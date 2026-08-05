"""The driver: one command, a stock Instagram APK in, a verified DFInsta build out.

Until now a human chained these stages by hand — decode, index, find the hosts,
write a resolution fixture, prepare a tree, apply, build, graft, verify. Every
piece was proven separately and nothing joined them. This is the join.

It is deliberately a *refusing* pipeline. Each stage either produces the exact
input the next one needs or stops with a stated reason, and no stage may weaken
a check to get past it. In particular the run stops at the evidence gate when any
hook lacks externally produced evidence — including the two settings hooks, which
have no mechanical fingerprint and need agent proposals supplied with
``--proposals``. Stopping there is the design working, not the pipeline failing.

``--discover-hosts`` is the one thing that can answer such a hook without a
human: it asks k independent agents *which class*, files their agreement and the
verifier's finding as evidence, and re-resolves. It is off by default because it
needs the network and spends the user's quota, and it cannot get a hook past the
gate — a disagreement leaves the hook unresolved and a refutation leaves it with
a failed claim the gate refuses. See `dfinsta_pipeline.discovery`.

Two things it works out for itself that used to be hand-edited per version, and
that silently produce a broken APK when wrong:

**Which DEX index is free.** Custom classes need an unused ``smali_classesN``.
430 ships 19 trees so ``smali_classes20`` was free; 439 ships 20 and needs
``smali_classes21``. Getting this wrong overwrites a stock DEX.

**Which host DEX files to graft.** The stock APK is preserved byte-for-byte apart
from the DEX files actually patched, so the graft list has to name exactly the
trees the resolved hosts live in. That list moved between 430 and 439 because the
hosts moved. The resolution knows each host's path, so the driver derives it
rather than trusting a default.

The build itself is not reimplemented here. ``tools/port_430/build.py`` already
does the apktool assembly and the stock DEX graft, and carries real-run evidence;
this shells out to it with computed arguments.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .agent_cost import record_run
from .agent_runner import AgentUnavailable
from .discovery import (
    DEFAULT_K,
    DEFAULT_MAX_AGENT_CALLS,
    DEFAULT_VERIFIERS,
    Discovery,
    discover_hosts,
)
from .evidence import (
    PRE_APPLY,
    EvidenceClaim,
    EvidenceKind,
    EvidenceLedger,
    Producer,
    Subject,
    Verdict,
    deterministic_claim,
)
from .hook_index import HookIndex, IndexUnusable
from .hook_manifest import Hook, ManifestError, load_manifest
from .proposals import (
    Assessment,
    ProposalError,
    Refutation,
    accepted_hosts,
    assess,
    load_proposals,
)
from .proposer import SandboxError
from .resolve import Outcome, ResolveReport, resolve_manifest
from .runtime_identity import (
    PROBE_DESCRIPTOR,
    expected_dex_symbols,
    probe_method,
    write_probe_class,
)

REPOSITORY = Path(__file__).resolve().parents[2]
INDEXER = REPOSITORY / "tools" / "indexer" / "build_index.py"
BUILDER = REPOSITORY / "tools" / "port_430" / "build.py"

STAGES = ("extract", "index", "assess", "resolve", "gate", "compose", "build")

#: What the `assess` stage needs beyond the index the driver already holds.
#:
#: Stage 4a's whole chain -- record, raise the gate, answer it through the
#: submission client, consume the rulings into the manifest -- was complete and
#: green while its FIRST link was a human remembering to type
#: `python -m dfinsta_pipeline.assessment_record record ...` after the driver
#: finished. `assessment_record.record` had zero callers outside its own `main`.
#: So every port would silently ship with no assessment, and the disconnection
#: would look exactly like a finished pipeline.
#:
#: Named as a group because the stage is all-or-nothing: a state root without an
#: actor cannot record, and an actor without a run id cannot be found again.
ASSESS_ARGUMENTS = ("--state-root", "--assessment-run-id", "--actor", "--owner-token")

#: Why discovery cannot run unlabelled. Stated once because both the CLI and
#: `port` refuse for it, and a second wording would let one of them drift into
#: sounding optional.
NEEDS_VERSION = (
    "--discover-hosts needs --version. Discovery is the expensive route — it spends "
    "the user's quota and takes minutes per proposer — and a run that spends without "
    "recording what it spent under a version label is exactly the state the cost "
    "ledger exists to end."
)


class DriverError(RuntimeError):
    """Raised when a stage cannot produce what the next one needs.

    ``report`` carries the resolution report when one had already been produced
    before the failure. What a port COST is settled at resolve time and does not
    depend on anything downstream succeeding: two Instagram 440 runs resolved all
    seven hooks mechanically, spent zero agent invocations, and recorded nothing
    at all, because the build failed afterwards for reasons that had nothing to
    do with cost. A metric that only records successful ports cannot show a port
    getting cheaper while it is still getting harder to build.
    """

    def __init__(self, *args: object, report: Mapping[str, Any] | None = None):
        super().__init__(*args)
        self.report = report


# --------------------------------------------------------------- dex topology


def smali_trees(decode: Path) -> list[str]:
    """Every smali tree in a decode, in DEX order."""
    trees = []
    for child in sorted(Path(decode).iterdir()):
        if child.is_dir() and (child.name == "smali" or child.name.startswith("smali_classes")):
            trees.append(child.name)
    return sorted(trees, key=tree_order)


def tree_order(name: str) -> int:
    if name == "smali":
        return 1
    return int(name[len("smali_classes") :])


def dex_name(tree: str) -> str:
    """`smali` -> `classes.dex`, `smali_classes4` -> `classes4.dex`."""
    return "classes.dex" if tree == "smali" else f"classes{tree_order(tree)}.dex"


def free_custom_tree(decode: Path) -> str:
    """The first unused `smali_classesN`, so custom code never lands on a stock DEX.

    430 ships 19 trees and 439 ships 20, which is why this cannot be a constant:
    the 439 port needed `classes21.dex` where 430 needed `classes20.dex`.
    """
    trees = smali_trees(decode)
    if not trees:
        raise DriverError(f"{decode} has no smali tree; it is not an apktool decode")
    return f"smali_classes{tree_order(trees[-1]) + 1}"


CALL_TARGET = re.compile(
    r"invoke-\w+(?:/range)?\s*\{[^}]*\},\s*(?P<descriptor>L[^;]+;)->(?P<method>[^(]+)\("
)
#: Instructions that only *reference* a type. `iput`/`iget` carry TWO register
#: operands before the owner, `sput`/`sget`/`new-instance` carry one, so the
#: second is optional — an earlier version used `[^,]+`, which cannot span a
#: comma and therefore never matched any `iput` at all.
FIELD_TARGET = re.compile(
    r"(?:iput|iget|sput|sget|new-instance)[\w-]*\s+[vp]\d+\s*(?:,\s*[vp]\d+\s*)?,\s*"
    r"(?P<descriptor>L[^;]+;)"
)


def host_hook_map(
    report: ResolveReport, index: HookIndex, hooks: Sequence[Hook]
) -> dict[str, list[list[str]]]:
    """Which DFInsta call each grafted DEX must be shown to contain.

    Read out of the payloads rather than hand-maintained, which is what it was
    before. A DEX stores a method reference as three separate indices, so only
    the type descriptor and the bare method name exist as literal strings — that
    pair is what the verifier can actually look for.

    Covers ALREADY_APPLIED hosts as well as RESOLVED ones, because
    :func:`host_dex_entries` grafts both. A DEX that is replaced in the output
    with nothing asserted about its contents is the vacuous pass the verifier
    refuses globally, reintroduced one DEX at a time.
    """
    by_id = {hook.hook_id: hook for hook in hooks}
    out: dict[str, set[tuple[str, str]]] = {}
    for item in report.resolutions:
        if item.outcome not in {Outcome.RESOLVED, Outcome.ALREADY_APPLIED}:
            continue
        assert item.descriptor is not None
        path = index.path_for(item.descriptor)
        if path is None:  # pragma: no cover - resolved hosts are always indexed
            continue
        dex = dex_name(path.split("/", 1)[0])
        # The manifest payload, not the rendered one: an already-applied hook has
        # no rendered payload, and the DFInsta descriptors and method names are
        # literal in the template either way — only registers and host types are
        # captures.
        payload = (
            item.resolution.payload
            if item.resolution is not None
            else list(by_id[item.hook_id].payload)
        )
        for line in payload:
            call = CALL_TARGET.search(line)
            if call and "dfinstagram" in call.group("descriptor"):
                out.setdefault(dex, set()).add(
                    (call.group("descriptor"), call.group("method"))
                )
                continue
            field = FIELD_TARGET.search(line)
            if field and "dfinstagram" in field.group("descriptor"):
                # A hook that only stores an object (the action-bar listener sets a
                # field rather than calling) still proves the type reference.
                out.setdefault(dex, set()).add((field.group("descriptor"), "<init>"))
    return {dex: sorted([list(pair) for pair in pairs]) for dex, pairs in out.items()}


def hook_symbol_map(
    report: ResolveReport, index: HookIndex, hooks: Sequence[Hook]
) -> dict[str, list[list[str]]]:
    """The same symbols as `host_hook_map`, kept per HOOK instead of per DEX.

    `host_hook_map` unions every hook's DFInsta calls into one set per DEX,
    because that is the shape the verifier checks. The union is exactly what
    makes its result unusable as evidence: `verify_build`'s report says
    `classes3.dex` carries `Lcom/dfinstagram/hooks; replaceReelsEndpoint`, and
    three Reels hooks contribute that same pair, so the report cannot say which
    of them was proven.

    This is what `EvidenceKind.STATIC_VERIFIED` needs and had never had. That
    kind is required of every hook by two requirement sets and **nothing in the
    tree produced one** -- so `EvidenceLedger.report("post_build")` escalated 7
    of 7 hooks on every version, and release readiness was unsatisfiable by
    construction rather than by any hook's fault.

    A pair one hook contributes is not always unique to it, and the claim says
    so rather than pretending otherwise: `attribution` is `sole` when every pair
    is this hook's alone and `shared` when any is not. Same distinction
    `runtime_probe` already draws for a signal two hooks emit. The probe symbol
    `Lcom/dfinstagram/probe; h_<hook_id>` is always sole, so a probe-instrumented
    build attributes cleanly and a bare one does not -- which is a fact about the
    build, not a defect in the reader.
    """

    by_id = {hook.hook_id: hook for hook in hooks}
    out: dict[str, set[tuple[str, str, str]]] = {}
    for item in report.resolutions:
        if item.outcome not in {Outcome.RESOLVED, Outcome.ALREADY_APPLIED}:
            continue
        assert item.descriptor is not None
        path = index.path_for(item.descriptor)
        if path is None:  # pragma: no cover - resolved hosts are always indexed
            continue
        dex = dex_name(path.split("/", 1)[0])
        # Seeded for EVERY resolved hook, before any symbol is looked for. A hook
        # whose payload references nothing of DFInsta's would otherwise be simply
        # ABSENT from this map -- and then `static_verified_claims` emits no claim
        # for it, the gate reports `not_exercised` ("nobody looked") where the
        # truth is `failed` ("there was nothing to look for"), and the driver's
        # own "N of M hook(s)" line takes its denominator from the claims it
        # produced rather than the hooks in the run. A two-hook port with one such
        # hook printed `1 of 1`. No hook in today's manifest does this, which is
        # exactly why it needed pinning rather than watching.
        out.setdefault(item.hook_id, set())
        payload = (
            item.resolution.payload
            if item.resolution is not None
            else list(by_id[item.hook_id].payload)
        )
        for line in payload:
            call = CALL_TARGET.search(line)
            if call and "dfinstagram" in call.group("descriptor"):
                out[item.hook_id].add(
                    (dex, call.group("descriptor"), call.group("method"))
                )
                continue
            field = FIELD_TARGET.search(line)
            if field and "dfinstagram" in field.group("descriptor"):
                out[item.hook_id].add((dex, field.group("descriptor"), "<init>"))
    return {
        hook_id: sorted([list(triple) for triple in triples])
        for hook_id, triples in out.items()
    }


def static_verified_claims(
    symbols: Mapping[str, list[list[str]]], verification: Mapping[str, Any]
) -> list[EvidenceClaim]:
    """One `static_verified` claim per hook, read out of the verifier's report.

    The verifier's `host_hooks` is `{dex: {"<descriptor> <method>": bool}}`. A
    hook passes when every symbol it contributed is present **and** the report
    passed overall -- the second half matters because a build can carry every
    DFInsta call and still have a mismatched preserved entry or an unexpected
    added file, and a per-hook pass beside a failed build would read as though
    the hook were fine.

    A hook with no symbols at all is `failed`, not skipped. That is the vacuous
    pass `verify_build` refuses globally, reintroduced one hook at a time: a hook
    that contributed nothing to prove has not been proven.
    """

    host_hooks = verification.get("host_hooks") or {}
    overall = verification.get("passed") is True
    counts: Counter[tuple[str, str, str]] = Counter()
    for triples in symbols.values():
        counts.update(tuple(triple) for triple in triples)

    claims: list[EvidenceClaim] = []
    for hook_id in sorted(symbols):
        triples = symbols[hook_id]
        checked: dict[str, bool] = {}
        for dex, descriptor, method in triples:
            checked[f"{dex} {descriptor} {method}"] = bool(
                host_hooks.get(dex, {}).get(f"{descriptor} {method}")
            )
        sole = all(counts[tuple(triple)] == 1 for triple in triples)
        passed = bool(triples) and all(checked.values()) and overall
        missing = sorted(name for name, found in checked.items() if not found)
        if not triples:
            summary = f"{hook_id} contributed no DFInsta symbol to assert"
        elif passed:
            summary = (
                f"{hook_id}: {len(checked)} DFInsta symbol(s) present in the built DEX"
            )
        elif not overall and not missing:
            summary = f"{hook_id}: symbols present, but the build verification failed"
        else:
            summary = f"{hook_id}: missing {', '.join(missing)}"
        claims.append(
            deterministic_claim(
                hook_id,
                EvidenceKind.STATIC_VERIFIED,
                passed,
                "tools/verify/verify_build.py",
                summary,
                {
                    "symbols": checked,
                    # Three states, not two. A hook that contributed nothing is
                    # neither solely nor jointly attributed -- calling it
                    # `shared` would read as "proven, alongside others" when the
                    # truth is that nothing was checked at all.
                    "attribution": ("sole" if sole else "shared") if triples else "none",
                    "build_verification_passed": overall,
                },
            )
        )
    return claims


def host_dex_entries(report: ResolveReport, index: HookIndex) -> list[str]:
    """The DEX files the resolved hosts live in, which is exactly what to graft.

    Everything else in the stock APK is preserved byte-for-byte, so this list
    must be neither short (a hook silently missing) nor long (a stock DEX
    needlessly replaced by an apktool round trip).
    """
    names: set[str] = set()
    for item in report.resolutions:
        if item.outcome not in {Outcome.RESOLVED, Outcome.ALREADY_APPLIED}:
            continue
        assert item.descriptor is not None
        path = index.path_for(item.descriptor)
        if path is None:
            raise DriverError(
                f"{item.hook_id} resolved to {item.descriptor}, which the index cannot place"
            )
        names.add(dex_name(path.split("/", 1)[0]))
    return sorted(names, key=lambda name: tree_order(
        "smali" if name == "classes.dex" else f"smali_classes{name[len('classes'):-len('.dex')]}"
    ))


# ------------------------------------------------------------------- stages


@dataclass(frozen=True)
class AssessmentRequest:
    """Where to record the stage 4a assessment, and who may answer its gate.

    A value rather than four loose parameters, because the four are meaningless
    apart: the run id is how the gate is found, the actor is who may answer it,
    and the owner token is what lets a later re-index reclaim a wedged operation.
    Passing three of the four is not a partial success.

    `manifest_path` defaults to the repository manifest, the same default
    `assessment_record.main` uses, and is overridable so a test never records an
    assessment of the shipped manifest by accident.
    """

    state_root: Path
    run_id: str
    allowed_actor: str
    owner_token: str
    manifest_path: Path | None = None
    rulings_path: Path | None = None

    @property
    def manifest(self) -> Path:
        return self.manifest_path or (REPOSITORY / "manifest" / "hooks.json")


def record_assessment(request: AssessmentRequest, index_dir: Path):
    """Record the assessment, translating its refusal into the driver's.

    `assessment_record` raises `RecordError`, which no driver caller catches, so
    without this a perfectly ordinary refusal -- a stale owner token, an index
    with no content hash -- would reach the operator as a traceback from a module
    they did not invoke.
    """
    from .assessment_record import RecordError, record  # noqa: PLC0415

    print(f"[assess] recording {request.run_id} from {index_dir}", flush=True)
    try:
        return record(
            request.state_root,
            run_id=request.run_id,
            index_dir=index_dir,
            manifest_path=request.manifest,
            allowed_actor=request.allowed_actor,
            owner_token=request.owner_token,
            rulings_path=request.rulings_path,
        )
    except RecordError as error:
        raise DriverError(f"could not record the assessment: {error}") from error


@dataclass
class RunPaths:
    out: Path
    #: An existing decode and index to analyse instead of producing new ones.
    #: Both are read-only inputs, so reusing them cannot affect the build, which
    #: always decodes the stock APK again for itself.
    reuse_decode: Path | None = None
    reuse_index: Path | None = None
    #: The two stage-10 files, which deliberately do NOT live in the run
    #: directory: what a port learned and what it cost are worth nothing unless
    #: they outlive the decode they were measured on, so both are committed next
    #: to the manifest. Overridable so a test never writes into the repository.
    memory_path: Path | None = None
    ledger_path: Path | None = None

    @property
    def decision_memory(self) -> Path:
        return self.memory_path or (REPOSITORY / "manifest" / "decisions.jsonl")

    @property
    def cost_ledger(self) -> Path:
        return self.ledger_path or (REPOSITORY / "manifest" / "agent_cost.jsonl")

    @property
    def discovery(self) -> Path:
        return self.out / "discovery.json"

    @property
    def analysis_decode(self) -> Path:
        # Separate from the build's own decode on purpose: `build.py` records
        # `stock_decode_mode: fresh_apktool_decode` as provenance and refuses a
        # pre-existing path, so its decode stays untouched by analysis.
        return self.reuse_decode or (self.out / "analysis-decode")

    @property
    def index_dir(self) -> Path:
        return self.reuse_index or (self.out / "index")

    @property
    def resolution(self) -> Path:
        return self.out / "resolution.json"

    @property
    def evidence(self) -> Path:
        return self.out / "evidence.jsonl"

    @property
    def readiness(self) -> Path:
        return self.out / "readiness.json"

    @property
    def patch_source(self) -> Path:
        return self.out / "patch-source"

    @property
    def build_decode(self) -> Path:
        return self.out / "build-decode"

    @property
    def work_tree(self) -> Path:
        return self.out / "work-tree"

    @property
    def output_apk(self) -> Path:
        return self.out / "dfinsta.apk"

    @property
    def host_hooks(self) -> Path:
        return self.out / "host-hooks.json"

    @property
    def required_strings(self) -> Path:
        return self.out / "required-strings.json"

    @property
    def assessments(self) -> Path:
        return self.out / "assessments.json"

    @property
    def framework_path(self) -> Path:
        return self.out / "framework"


@dataclass
class RunResult:
    stage_reached: str
    stopped_because: str = ""
    report: ResolveReport | None = None
    escalations: tuple[str, ...] = ()
    artifacts: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.stopped_because

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_reached": self.stage_reached,
            "ok": self.ok,
            "stopped_because": self.stopped_because,
            "escalations": list(self.escalations),
            "artifacts": dict(self.artifacts),
        }


def run_command(command: Sequence[str], label: str) -> None:
    print(f"[{label}] {' '.join(str(part) for part in command)}", flush=True)
    completed = subprocess.run([str(part) for part in command], check=False)
    if completed.returncode != 0:
        raise DriverError(f"{label} failed with exit code {completed.returncode}")


def extract(apk: Path, decode: Path, apktool: Path, framework_path: Path, framework_apk: Path | None) -> None:
    if decode.exists():
        raise DriverError(f"refusing to overwrite {decode}")
    if framework_apk is not None:
        run_command(
            ["java", "-jar", apktool, "if", framework_apk, "-p", framework_path],
            "framework",
        )
    run_command(
        ["java", "-jar", apktool, "decode", "-p", framework_path, "-o", decode, apk],
        "extract",
    )


def build_index(decode: Path, index_dir: Path) -> None:
    if index_dir.exists():
        raise DriverError(f"refusing to overwrite {index_dir}")
    run_command([sys.executable, INDEXER, decode, "--out", index_dir], "index")


def compose_patch_source(
    destination: Path,
    custom_code: Path,
    operations: Sequence[Mapping[str, Any]],
    hooks: Sequence[Hook] = (),
) -> None:
    """Write the exact patch source `build.py` consumes.

    The custom classes are version-independent DFInsta code and are copied as-is;
    only the resolved operations differ per version, which is the whole point of
    the manifest.
    """
    if destination.exists():
        raise DriverError(f"refusing to overwrite {destination}")
    if not (custom_code / "newCode").is_dir():
        raise DriverError(f"{custom_code} has no newCode/ directory")
    destination.mkdir(parents=True)
    shutil.copytree(custom_code / "newCode", destination / "newCode")
    if hooks:
        # Generated per run from the manifest, so the hook list and the probe
        # methods cannot drift apart.
        write_probe_class(hooks, destination / "newCode")
    (destination / "patches").mkdir()
    (destination / "patches" / "anchored_patches.json").write_text(
        json.dumps({"version": 1, "operations": list(operations)}, indent=1) + "\n",
        encoding="utf-8",
    )


def load_validator():
    """The mechanical candidate validator, imported lazily.

    It lives under `tools/` and reaches into `tools/reconstruction`, so importing
    it at module scope would make this package unusable wherever those are not on
    the path.
    """
    resolver = REPOSITORY / "tools" / "resolver"
    if str(resolver) not in sys.path:
        sys.path.insert(0, str(resolver))
    from validate_candidates import validate  # noqa: PLC0415

    return validate


def record_resolution_evidence(
    ledger: EvidenceLedger,
    report: ResolveReport,
    proposed: Mapping[str, Sequence[str]],
    decode: Path,
    hooks: Sequence[Hook],
    already_registered: Iterable[str] = (),
) -> None:
    """Turn what the pre-apply stages proved into ledger claims.

    Two kinds only, and both re-derived from the decode: the anchor resolved to
    exactly one site, and the payload does not clobber a live register. Nothing
    here can speak to static structure or runtime behaviour, because neither the
    APK nor the device exists yet — those claims arrive after the build, and
    their absence is what keeps a pre-apply pass from meaning "this works".
    """
    by_id = {hook.hook_id: hook for hook in hooks}
    registered = set(already_registered)
    validate = load_validator()
    for item in report.resolutions:
        if item.outcome is Outcome.ALREADY_APPLIED:
            # The marker is this pipeline's own idempotence stamp. Its presence at
            # exactly the expected count in exactly one class is deterministic
            # evidence that this exact payload is in this exact place — but
            # register liveness cannot be re-derived once the payload is applied,
            # because that check needs an unpatched anchor. Hence its own
            # provenance rather than a claim asserting something not re-derived.
            if item.hook_id not in registered:
                ledger.register(
                    Subject(item.hook_id, "already_applied", descriptor=item.descriptor)
                )
            ledger.record(
                deterministic_claim(
                    item.hook_id,
                    EvidenceKind.ANCHOR_UNIQUE,
                    True,
                    actor="dfinsta_pipeline.resolve",
                    summary=item.reason,
                    detail={"descriptor": item.descriptor, "outcome": item.outcome.value},
                )
            )
            continue
        if item.hook_id not in registered:
            # A hook assessed through `proposals.assess` is already registered
            # there, with the real proposer. Registering it again under a
            # synthetic name is refused by the ledger and used to kill the run.
            provenance = "agent" if item.hook_id in proposed else "mechanical"
            proposer = f"proposal:{item.hook_id}" if provenance == "agent" else ""
            ledger.register(
                Subject(
                    item.hook_id, provenance, descriptor=item.descriptor, proposed_by=proposer
                )
            )
        ledger.record(
            deterministic_claim(
                item.hook_id,
                EvidenceKind.ANCHOR_UNIQUE,
                item.outcome is Outcome.RESOLVED,
                actor="dfinsta_pipeline.resolve",
                summary=item.reason,
                detail={"descriptor": item.descriptor, "outcome": item.outcome.value},
            )
        )
        if item.outcome is not Outcome.RESOLVED:
            continue
        row = validate(decode, [item.as_operation(by_id[item.hook_id])])[0]
        safe = row.get("registers_safe")
        ledger.record(
            EvidenceClaim(
                hook_id=item.hook_id,
                kind=EvidenceKind.REGISTERS_SAFE,
                # `None` means the check never ran, which is recorded as
                # inconclusive rather than folded into a pass.
                verdict=(
                    Verdict.PASSED
                    if safe is True
                    else Verdict.FAILED
                    if safe is False
                    else Verdict.INCONCLUSIVE
                ),
                producer=Producer.DETERMINISTIC,
                actor="tools/resolver/validate_candidates.py",
                summary=str(row.get("registers_note") or "register liveness not evaluated"),
                detail={"payload_writes": list(row.get("payload_writes", ()))},
            )
        )


def port(
    apk: Path,
    paths: RunPaths,
    hooks: Sequence[Hook],
    apktool: Path,
    framework_apk: Path | None,
    custom_code: Path,
    proposals: Mapping[str, Sequence[str]] | None = None,
    full_proposals: Path | None = None,
    refutations: Path | None = None,
    stop_after: str = "build",
    require_evidence: bool = True,
    version: str = "",
    recorded_at: str = "",
    discovery: Discovery | None = None,
    assessment: AssessmentRequest | None = None,
) -> RunResult:
    """Run the pipeline as far as the evidence allows, then record what it cost.

    *version* is the label this port is for, such as ``"439"``. It is the key
    both stage-10 stores are written under, so it is required to record anything
    and refused when it looks like a path. *recorded_at* is the timestamp those
    records carry: it comes from the caller because nothing in that layer reads a
    clock, deliberately, so a Temporal replay rewrites the line already on disk.

    *discovery* turns on stage 5a. Without it the run is exactly what it was
    before that stage existed — deterministic, offline, and stopping at a hook
    whose host nothing points at.
    """
    if discovery is not None and not version.strip():
        raise DriverError(NEEDS_VERSION)
    if version.strip() and not recorded_at.strip():
        raise DriverError(
            "--version needs --recorded-at. A cost record stamped with nothing is one "
            "no trend can order, and this layer must never read the clock for itself."
        )
    try:
        result = _run_stages(
            apk=apk,
            paths=paths,
            hooks=hooks,
            apktool=apktool,
            framework_apk=framework_apk,
            custom_code=custom_code,
            proposals=proposals,
            full_proposals=full_proposals,
            refutations=refutations,
            stop_after=stop_after,
            require_evidence=require_evidence,
            discovery=discovery,
            assessment=assessment,
        )
    except DriverError as error:
        # A stage that threw still spent whatever it spent. Recording happens
        # before the error goes on its way, and the error still goes on its way:
        # this is not a rescue, it is a receipt.
        if error.report is not None and version.strip():
            try:
                record_run(
                    error.report,
                    version,
                    recorded_at,
                    memory_path=paths.decision_memory,
                    ledger_path=paths.cost_ledger,
                )
            except Exception as cost_error:  # noqa: BLE001 - the original must win
                # The receipt must never replace the reason. If writing the cost
                # record throws, its traceback would surface instead of "build
                # failed with exit code 1" and the run would report the wrong
                # problem — a bookkeeping failure masquerading as a port failure.
                print(f"[cost] could not record what this run cost: {cost_error}", flush=True)
            else:
                print(
                    f"[cost] recorded what this failed run cost to {paths.cost_ledger}",
                    flush=True,
                )
        raise
    # Stage 10, once per run and at the end of it, whatever the run concluded.
    # What a port *learned* is only written when something resolved; what it
    # *spent* is spent either way, and a blocked port that recorded nothing is
    # how the agent-invocation count stays at "unknown" forever.
    if result.report is None:
        return result
    if not version.strip():
        print(
            "[cost] nothing recorded: pass --version (and --recorded-at) to file what "
            "this port resolved and what it cost.",
            flush=True,
        )
        return result
    record_run(
        result.report,
        version,
        recorded_at,
        memory_path=paths.decision_memory,
        ledger_path=paths.cost_ledger,
    )
    result.artifacts["decision_memory"] = str(paths.decision_memory)
    result.artifacts["cost_ledger"] = str(paths.cost_ledger)
    return result


def _run_stages(
    apk: Path,
    paths: RunPaths,
    hooks: Sequence[Hook],
    apktool: Path,
    framework_apk: Path | None,
    custom_code: Path,
    proposals: Mapping[str, Sequence[str]] | None = None,
    full_proposals: Path | None = None,
    refutations: Path | None = None,
    stop_after: str = "build",
    require_evidence: bool = True,
    discovery: Discovery | None = None,
    assessment: AssessmentRequest | None = None,
) -> RunResult:
    """The stages themselves. Split out so stage 10 has exactly one call site."""
    if stop_after not in STAGES:
        raise DriverError(f"unknown stage {stop_after!r}; expected one of {', '.join(STAGES)}")
    proposals = dict(proposals or {})
    paths.out.mkdir(parents=True, exist_ok=True)
    artifacts: dict[str, str] = {}

    # 1. Extract
    if paths.reuse_decode is None:
        extract(apk, paths.analysis_decode, apktool, paths.framework_path, framework_apk)
    else:
        print(f"[extract] reusing {paths.analysis_decode}", flush=True)
    artifacts["analysis_decode"] = str(paths.analysis_decode)
    if stop_after == "extract":
        return RunResult("extract", artifacts=artifacts)

    # 2. Index
    if paths.reuse_index is None:
        build_index(paths.analysis_decode, paths.index_dir)
    else:
        print(f"[index] reusing {paths.index_dir}", flush=True)
    artifacts["index"] = str(paths.index_dir)
    if stop_after == "index":
        return RunResult("index", artifacts=artifacts)

    # 2a. Assess. Reads the same index stage 2 just wrote and records the stage 4a
    #     assessment under a run-keyed authority row, which is what makes the
    #     feature gate answerable from a run id alone. Skipped, loudly, when the
    #     run was not given somewhere to record it -- an offline port is a real
    #     mode and must not start requiring a ledger.
    if assessment is None:
        print(
            f"[assess] skipped: no state root. Pass {', '.join(ASSESS_ARGUMENTS)} to "
            "record an assessment this run's feature gate can be answered from.",
            flush=True,
        )
    else:
        recorded = record_assessment(assessment, paths.index_dir)
        artifacts["assessment"] = recorded.assessment.uri
        artifacts["assessment_run_id"] = recorded.run_id
    if stop_after == "assess":
        return RunResult("assess", artifacts=artifacts)

    # 3. Resolve. Agent proposals are assessed first, because an accepted one
    #    contributes both a host for the mechanical path and, for a hook whose
    #    manifest payload is only a shape, the operation itself.
    try:
        index = HookIndex.for_decode(paths.index_dir, paths.analysis_decode)
    except IndexUnusable as error:
        raise DriverError(str(error)) from error

    ledger = EvidenceLedger(paths.evidence)
    assessments: dict[str, Assessment] = {}
    if full_proposals is not None:
        try:
            assessments = assess_proposals(
                full_proposals, refutations, hooks, paths.analysis_decode, ledger
            )
        except ProposalError as error:
            raise DriverError(str(error)) from error
        accepted = accepted_hosts(assessments.values())
        for hook_id, descriptors in accepted.items():
            proposals.setdefault(hook_id, descriptors)
        paths.assessments.write_text(
            json.dumps(
                {key: value.to_dict() for key, value in assessments.items()},
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        artifacts["assessments"] = str(paths.assessments)

    report = resolve_manifest(hooks, index, paths.analysis_decode, proposals)

    # 3a. Discovery, only when asked for. A hook that escalated *because nothing
    #     points at its host* is the one question k agents can answer, and the
    #     only one they are asked: the manifest already owns the anchor pattern
    #     and the payload template, and asking for those back manufactures the
    #     variance that then reads as disagreement.
    discovered: set[str] = set()
    notice = ""
    if discovery is not None:
        try:
            found = discover_hosts(
                report,
                hooks,
                paths.analysis_decode,
                ledger,
                discovery,
                skip=set(assessments),
            )
        except SandboxError as error:
            # A sandbox that could not be made isolated is not a degraded run to
            # continue with: the answers to this exact question are on this
            # machine, and an agent that can reach them measures nothing.
            raise DriverError(str(error)) from error
        except AgentUnavailable as error:
            # "No agent ran" and "an agent answered badly" look identical in a
            # results file and mean opposite things about whether the run
            # measured anything. This one is the former, said as such.
            raise DriverError(str(error)) from error
        notice = found.notice
        discovered = {item.hook_id for item in found.hooks if item.attempted}
        if found.hooks:
            paths.discovery.write_text(
                json.dumps(found.to_dict(), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            artifacts["discovery"] = str(paths.discovery)
        for hook_id, descriptor in found.hosts.items():
            proposals[hook_id] = [*proposals.get(hook_id, ()), descriptor]
        if found.hosts:
            # Re-resolve with the agreed hosts in hand. Nothing else changes:
            # the host is checked against the index and then against the decode
            # exactly like a mechanically-found candidate, so an agreed class
            # whose anchor does not match still escalates.
            report = resolve_manifest(hooks, index, paths.analysis_decode, proposals)

    # A hook whose manifest payload is a shape cannot be resolved from the
    # template, so an accepted proposal is what stands in for it.
    by_proposal = {
        hook_id
        for hook_id, assessment in assessments.items()
        if assessment.resolved
    }
    paths.resolution.write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    artifacts["resolution"] = str(paths.resolution)
    outstanding = tuple(
        item.hook_id for item in report.escalations if item.hook_id not in by_proposal
    )
    escalations = tuple(item.hook_id for item in report.escalations)
    if stop_after == "resolve":
        return RunResult("resolve", report=report, escalations=escalations, artifacts=artifacts)

    if outstanding or not report.resolutions:
        detail = "; ".join(
            f"{item.hook_id}: {item.reason}"
            for item in report.escalations
            if item.hook_id in outstanding
        )
        return RunResult(
            "resolve",
            stopped_because=(
                (
                    f"{len(outstanding)} hook(s) did not resolve — {detail}"
                    if outstanding
                    else "no active hook resolved"
                )
                # A hook the budget never reached looks identical to a hook the
                # agents could not answer, and reads as the harder finding of the
                # two. Say which it was, in the sentence a caller prints.
                + (f" [{notice}]" if notice else "")
            ),
            report=report,
            escalations=outstanding,
            artifacts=artifacts,
        )

    # 4. Evidence gate — the PRE-APPLY half only. The static and runtime kinds
    #    need an APK that does not exist yet; requiring them here would make the
    #    gate unsatisfiable. They gate the release instead, and until they exist
    #    a passing gate here says only that nothing derivable from the decode
    #    objects.
    record_resolution_evidence(
        ledger,
        report,
        proposals,
        paths.analysis_decode,
        hooks,
        # Both routes registered their own subject, naming the agent that
        # actually proposed the host. Re-registering under a synthetic proposer
        # is refused by the ledger — and rightly: the two registrations would
        # require different evidence and disagree about who may produce it.
        already_registered={*assessments, *discovered},
    )
    readiness = ledger.report(PRE_APPLY)
    paths.readiness.write_text(
        json.dumps(readiness, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    artifacts["readiness"] = str(paths.readiness)
    artifacts["evidence"] = str(paths.evidence)
    if require_evidence and not readiness["complete"]:
        blocked = [entry["hook_id"] for entry in readiness["escalations"]]
        return RunResult(
            "gate",
            stopped_because=(
                f"{len(blocked)} hook(s) lack the evidence required before applying: "
                f"{', '.join(blocked)}. See {paths.readiness}."
            ),
            report=report,
            escalations=tuple(blocked),
            artifacts=artifacts,
        )
    if stop_after == "gate":
        return RunResult("gate", report=report, escalations=escalations, artifacts=artifacts)

    # 5. Compose the patch source. Mechanically resolved operations come from the
    #    report; a hook satisfied by an accepted proposal contributes the
    #    proposal's own anchor and payload, never the manifest shape.
    by_id = {hook.hook_id: hook for hook in hooks}
    operations = [
        item.as_operation(by_id[item.hook_id])
        for item in report.resolutions
        if item.outcome is Outcome.RESOLVED
    ]
    for hook_id in sorted(by_proposal):
        if any(operation["id"] == hook_id for operation in operations):
            continue
        accepted_proposal = assessments[hook_id].accepted
        assert accepted_proposal is not None
        operations.append(accepted_proposal.as_operation(by_id[hook_id]))
    compose_patch_source(paths.patch_source, custom_code, operations, hooks)
    artifacts["patch_source"] = str(paths.patch_source)
    if stop_after == "compose":
        return RunResult("compose", report=report, artifacts=artifacts)

    # 6. Build, with the DEX topology derived from this version rather than assumed
    custom_tree = free_custom_tree(paths.analysis_decode)
    replace_dex = host_dex_entries(report, index)
    if not replace_dex:
        raise DriverError("no host DEX entries to graft; refusing to build a stock APK")
    hooks_map = host_hook_map(report, index, hooks)
    if not hooks_map:
        raise DriverError("no host hook could be derived; the verifier would pass vacuously")
    paths.host_hooks.write_text(
        json.dumps(hooks_map, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    # What a ruling put in the manifest, for the verifier to find in the built
    # DEX. Derived here because the manifest is a per-run fact and `verify_build`
    # refuses to pin one for itself. Without this the decide machine could record
    # a block that the execute machine never proved shipped.
    from .rulings import RulingError, required_build_strings  # noqa: PLC0415

    try:
        required = required_build_strings(REPOSITORY / "manifest" / "hooks.json")
    except RulingError as error:
        raise DriverError(f"cannot derive the build's required strings: {error}") from error
    paths.required_strings.write_text(
        json.dumps(list(required), indent=2) + "\n", encoding="utf-8"
    )
    artifacts["custom_tree"] = custom_tree
    artifacts["replace_dex"] = ",".join(replace_dex)
    artifacts["host_hooks"] = str(paths.host_hooks)
    artifacts["required_strings"] = str(paths.required_strings)
    print(f"[build] custom tree {custom_tree}, grafting {', '.join(replace_dex)}", flush=True)
    if framework_apk is None:
        raise DriverError("--framework-apk is required to build", report=report)
    # `report` rides along on any failure from here down, so a port that resolved
    # cheaply and then failed to assemble still records what it cost. See
    # `DriverError`.
    try:
        run_command(
            [
                sys.executable,
                BUILDER,
                paths.build_decode,
                apk,
                paths.patch_source,
                apktool,
                framework_apk,
                "--framework-path",
                paths.framework_path,
                "--work-tree",
                paths.work_tree,
                "--output-apk",
                paths.output_apk,
                "--custom-tree",
                custom_tree,
                "--replace-dex",
                ",".join(replace_dex),
                "--verifier",
                "generic",
                "--host-hooks",
                paths.host_hooks,
                "--required-strings",
                paths.required_strings,
            ],
            "build",
        )
    except DriverError as error:
        raise DriverError(*error.args, report=report) from error
    artifacts["apk"] = str(paths.output_apk)

    # The build's own verifier report, turned into evidence. Until 2026-08-05
    # `build.py` wrote this file, `verify_build.py` computed every assertion in
    # it, and nothing joined the two to the ledger -- so `static_verified` was
    # required of every hook and produced for none, and the post-build report
    # below escalated all seven on every version for a reason no hook could fix.
    verification_path = paths.output_apk.with_suffix(".verification.json")
    try:
        verification = json.loads(verification_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        # Not fatal, and deliberately so: the APK is built and verified by the
        # time this runs -- `build.py` exits non-zero on a failed verification,
        # so reaching here at all means it passed. Losing the claim costs
        # evidence, not correctness, and turning a readable-report problem into a
        # failed port would be the tail wagging the dog.
        print(f"[build] no static_verified evidence: {error}", flush=True)
    else:
        static_claims = static_verified_claims(
            hook_symbol_map(report, index, hooks), verification
        )
        for claim in static_claims:
            ledger.record(claim)
        passed = sum(1 for claim in static_claims if claim.verdict is Verdict.PASSED)
        artifacts["static_verified"] = f"{passed}/{len(static_claims)}"
        print(
            f"[build] static_verified: {passed} of {len(static_claims)} hook(s)",
            flush=True,
        )

    outstanding = ledger.report("post_build")["escalations"]
    if outstanding:
        print(
            f"\n{len(outstanding)} hook(s) still lack post-build evidence "
            "(static assertions, a two-directional runtime probe, and a differential "
            "against the last known-good build). This APK is not release-ready: every "
            "inert patch this project has shipped passed everything up to here.",
            flush=True,
        )
    return RunResult("build", report=report, artifacts=artifacts)


# ---------------------------------------------------------------------- cli


def load_host_proposals(path: Path | None) -> dict[str, list[str]]:
    """Accepted hosts for `by_agent` hooks, as `{hook_id: [descriptor]}`.

    This is the *weak* form: a bare host, which lets the manifest template be
    checked against a class but supplies no agreement and no adversarial review.
    A hook whose template is a shape (``requires_proposal``) cannot be satisfied
    this way at all — use ``--full-proposals``.
    """
    if path is None:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise DriverError(
            f"{path} must map hook_id to a list of descriptors, got {type(data).__name__}"
        )
    for key, value in data.items():
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise DriverError(
                f"{path}: {key} must map to a list of descriptor strings. Full proposals "
                "carrying an anchor and payload belong in --full-proposals, where they are "
                "run through agreement and adversarial review."
            )
    return {key: list(value) for key, value in data.items()}


def assess_proposals(
    path: Path,
    refutations_path: Path | None,
    hooks: Sequence[Hook],
    decode: Path,
    ledger: EvidenceLedger,
) -> dict[str, Assessment]:
    """Run agent proposals through agreement, validation and refutation.

    This is the only route by which a `requires_proposal` hook can be satisfied,
    because it is the only one that produces the evidence such a hook needs: that
    independent proposers reached the same answer, and that a verifier told to
    refute it could not.
    """
    by_id = {hook.hook_id: hook for hook in hooks}
    grouped = load_proposals(path)
    refutations: dict[str, list[Refutation]] = {}
    if refutations_path is not None:
        for entry in json.loads(refutations_path.read_text(encoding="utf-8")):
            refutations.setdefault(entry["hook_id"], []).append(
                Refutation(
                    hook_id=entry["hook_id"],
                    verifier=entry["verifier"],
                    refuted=bool(entry["refuted"]),
                    finding=entry["finding"],
                    checked=tuple(entry.get("checked", ())),
                )
            )
    validate = load_validator()
    out: dict[str, Assessment] = {}
    for hook_id, items in grouped.items():
        hook = by_id.get(hook_id)
        if hook is None:
            raise DriverError(f"proposals name unknown hook {hook_id!r}")
        out[hook_id] = assess(
            hook,
            items,
            decode,
            validate,
            refutations.get(hook_id, ()),
            ledger=ledger,
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("apk", type=Path, help="stock Instagram APK")
    parser.add_argument("--out", type=Path, required=True, help="run directory")
    parser.add_argument("--manifest", type=Path, default=REPOSITORY / "manifest" / "hooks.json")
    parser.add_argument(
        "--custom-code",
        type=Path,
        default=REPOSITORY / "dfinsta_source_439",
        help="directory holding newCode/ with the DFInsta classes",
    )
    parser.add_argument("--apktool", type=Path, default=REPOSITORY / "apktool_2.9.3.jar")
    parser.add_argument(
        "--framework-apk",
        type=Path,
        help="API 36 framework-res.apk; required to decode and build Instagram 430+",
    )
    parser.add_argument(
        "--proposals",
        type=Path,
        help='JSON {"hook_id": ["LX/0Di2;"]} of hosts for by_agent hooks. Supplies a class '
        "to check the manifest template against; supplies no agreement or adversarial "
        "evidence, so such a hook still stops at the gate.",
    )
    parser.add_argument(
        "--full-proposals",
        type=Path,
        help="JSON proposals carrying proposer, descriptor, anchor and payload. Run through "
        "agreement, the mechanical validator and any refutations, and the only route by "
        "which a requires_proposal hook can be satisfied.",
    )
    parser.add_argument(
        "--refutations",
        type=Path,
        help='JSON [{"hook_id":…,"verifier":…,"refuted":bool,"finding":…}] from verifiers '
        "instructed to break the proposals",
    )
    parser.add_argument(
        "--reuse-decode",
        type=Path,
        help="analyse this existing decode instead of extracting one; the build "
        "still decodes the stock APK for itself",
    )
    parser.add_argument(
        "--reuse-index", type=Path, help="use this existing index instead of building one"
    )
    parser.add_argument("--stop-after", choices=STAGES, default="build")
    assess_group = parser.add_argument_group(
        "assess",
        "Record the stage 4a assessment so this run's feature gate can be answered "
        "from its run id. All four are needed together; supplying none skips the "
        "stage and leaves the run entirely offline.",
    )
    assess_group.add_argument(
        "--state-root", type=Path, default=None, help="ledger and content store"
    )
    assess_group.add_argument(
        "--assessment-run-id",
        default=None,
        help="how the recorded assessment is found again; the gate client takes "
        "nothing but this",
    )
    assess_group.add_argument("--actor", default=None, help="who may answer the gate")
    assess_group.add_argument(
        "--owner-token",
        default=None,
        help="what lets a later re-index reclaim this operation if it wedges",
    )
    assess_group.add_argument(
        "--rulings", type=Path, default=None, help="ruling store; default manifest/rulings.jsonl"
    )
    parser.add_argument(
        "--skip-evidence-gate",
        action="store_true",
        help=(
            "proceed to build without the evidence a hook needs. For bring-up on a "
            "target whose probes do not exist yet; never for a build anyone installs."
        ),
    )
    parser.add_argument(
        "--version",
        default="",
        help="the label this port is for, e.g. 439. Both stage-10 stores are keyed by "
        "it, so it is required to record what the run resolved and what it cost, and "
        "required by --discover-hosts.",
    )
    parser.add_argument(
        "--recorded-at",
        default=datetime.now(timezone.utc).isoformat(),
        help="the timestamp the durable records carry. Defaults to now; pass it "
        "explicitly to re-run a port and write byte-identical records.",
    )
    parser.add_argument(
        "--decision-memory",
        type=Path,
        help="where to append what this port learned (default: the committed "
        "manifest/decisions.jsonl)",
    )
    parser.add_argument(
        "--cost-ledger",
        type=Path,
        help="where to append what this port cost (default: the committed "
        "manifest/agent_cost.jsonl)",
    )
    discovery_group = parser.add_argument_group(
        "host discovery (stage 5a)",
        "Off by default. It needs the network, spends quota and takes minutes per "
        "proposer, so it runs only for a hook that escalated because nothing "
        "mechanical points at its host — and never weakens the evidence gate.",
    )
    discovery_group.add_argument(
        "--discover-hosts",
        action="store_true",
        help="ask k agents which class each host-less hook belongs in",
    )
    discovery_group.add_argument(
        "--discover-k", type=int, default=DEFAULT_K, help="independent proposers per hook"
    )
    discovery_group.add_argument(
        "--discover-verifiers",
        type=int,
        default=DEFAULT_VERIFIERS,
        help="adversarial verifiers per hook, each shown the claim and never the rationale",
    )
    discovery_group.add_argument("--discover-model", help="model id for the agent runtime")
    discovery_group.add_argument(
        "--max-agent-calls",
        type=int,
        default=DEFAULT_MAX_AGENT_CALLS,
        help="total agent invocations this run may make. A hook the cap cannot cover "
        "is reported as skipped rather than as unresolved.",
    )
    discovery_group.add_argument(
        "--sandbox-root",
        type=Path,
        help="where to hardlink the decode for the agents. Must not exist and must be "
        "outside this repository, which holds the resolved anchors for every version "
        "ported so far.",
    )
    discovery_group.add_argument(
        "--keep-sandbox", action="store_true", help="do not remove the sandbox afterwards"
    )
    args = parser.parse_args(argv)

    hooks = load_manifest(args.manifest)
    discovery: Discovery | None = None
    assessment: AssessmentRequest | None = None
    try:
        supplied = {
            "--state-root": args.state_root,
            "--assessment-run-id": args.assessment_run_id,
            "--actor": args.actor,
            "--owner-token": args.owner_token,
        }
        given = {name for name, value in supplied.items() if value is not None}
        # All four or none. Three of the four is not a partial success: it is a run
        # that looks like it recorded an assessment and did not.
        if given and given != set(ASSESS_ARGUMENTS):
            missing = ", ".join(sorted(set(ASSESS_ARGUMENTS) - given))
            raise DriverError(
                f"recording an assessment needs all of {', '.join(ASSESS_ARGUMENTS)}; "
                f"missing {missing}. Pass none of them to skip the stage."
            )
        if given:
            assessment = AssessmentRequest(
                state_root=args.state_root,
                run_id=args.assessment_run_id,
                allowed_actor=args.actor,
                owner_token=args.owner_token,
                manifest_path=args.manifest,
                rulings_path=args.rulings,
            )
        if args.discover_hosts:
            if not args.version.strip():
                # Refused here rather than by `Discovery`, so the message names
                # the flag the user is missing rather than the field it fills.
                raise DriverError(NEEDS_VERSION)
            discovery = Discovery(
                version=args.version,
                k=args.discover_k,
                verifiers=args.discover_verifiers,
                model=args.discover_model,
                max_agent_calls=args.max_agent_calls,
                sandbox_root=args.sandbox_root,
                keep_sandbox=args.keep_sandbox,
            )
    except (DriverError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    try:
        result = port(
            apk=args.apk,
            paths=RunPaths(
                args.out,
                args.reuse_decode,
                args.reuse_index,
                memory_path=args.decision_memory,
                ledger_path=args.cost_ledger,
            ),
            hooks=hooks,
            apktool=args.apktool,
            framework_apk=args.framework_apk,
            custom_code=args.custom_code,
            proposals=load_host_proposals(args.proposals),
            full_proposals=args.full_proposals,
            refutations=args.refutations,
            stop_after=args.stop_after,
            require_evidence=not args.skip_evidence_gate,
            version=args.version,
            recorded_at=args.recorded_at,
            discovery=discovery,
            assessment=assessment,
        )
    except (DriverError, ManifestError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    print()
    if result.report is not None:
        for item in result.report.resolutions:
            mark = " " if not item.escalates else "!"
            print(f"{mark} {item.outcome.value:16s} {item.hook_id:38s} {item.descriptor or '-'}")
    print()
    for key, value in sorted(result.artifacts.items()):
        print(f"  {key:18s} {value}")
    if result.ok:
        print(f"\nreached stage: {result.stage_reached}")
        return 0
    print(f"\nSTOPPED at {result.stage_reached}: {result.stopped_because}", file=sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

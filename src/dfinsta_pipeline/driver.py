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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

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

STAGES = ("extract", "index", "resolve", "gate", "compose", "build")


class DriverError(RuntimeError):
    """Raised when a stage cannot produce what the next one needs."""


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


@dataclass
class RunPaths:
    out: Path
    #: An existing decode and index to analyse instead of producing new ones.
    #: Both are read-only inputs, so reusing them cannot affect the build, which
    #: always decodes the stock APK again for itself.
    reuse_decode: Path | None = None
    reuse_index: Path | None = None

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
) -> RunResult:
    """Run the pipeline as far as the evidence allows."""
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
                f"{len(outstanding)} hook(s) did not resolve — {detail}"
                if outstanding
                else "no active hook resolved"
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
        already_registered=assessments.keys(),
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
    artifacts["custom_tree"] = custom_tree
    artifacts["replace_dex"] = ",".join(replace_dex)
    artifacts["host_hooks"] = str(paths.host_hooks)
    print(f"[build] custom tree {custom_tree}, grafting {', '.join(replace_dex)}", flush=True)
    if framework_apk is None:
        raise DriverError("--framework-apk is required to build")
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
        ],
        "build",
    )
    artifacts["apk"] = str(paths.output_apk)
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
    parser.add_argument(
        "--skip-evidence-gate",
        action="store_true",
        help=(
            "proceed to build without the evidence a hook needs. For bring-up on a "
            "target whose probes do not exist yet; never for a build anyone installs."
        ),
    )
    args = parser.parse_args(argv)

    hooks = load_manifest(args.manifest)
    try:
        result = port(
            apk=args.apk,
            paths=RunPaths(args.out, args.reuse_decode, args.reuse_index),
            hooks=hooks,
            apktool=args.apktool,
            framework_apk=args.framework_apk,
            custom_code=args.custom_code,
            proposals=load_host_proposals(args.proposals),
            full_proposals=args.full_proposals,
            refutations=args.refutations,
            stop_after=args.stop_after,
            require_evidence=not args.skip_evidence_gate,
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

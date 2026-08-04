"""What a human's ruling actually changes. The gate's missing consumer.

Stage 4 asks a human for a verdict on every candidate the assessment surfaced,
the Workflow admits the document, the ledger records the decision — and until
this module existed **nothing read the verdict**. `grep -rn "offer_toggle"` found
it in the contract, the assessment and the client, and nowhere that acts.

That is the same shape as the gap this project spent the previous day closing on
the *other* side of the gate (`feature_gate.py` "imported by nothing but its own
tests"), one link further along the chain, and it was invisible for the same
reason: every piece is complete, tested and green, so the chain *looks* finished.

===============================================================================
  A RULING HAS THREE DESTINATIONS, AND ONLY TWO OF THEM ARE DATA
===============================================================================

**The manifest.** `assessment.blocked_endpoints` reads `semantic_deps` off the
hook manifest, so an endpoint a human ruled `block` or `offer_toggle` has to land
there or **stage 4a proposes it again on the next port, forever**. Measured
before relying on it: `semantic_deps` is read by `assessment.py` and
`surface_diff.py` and by nothing in the resolution path, so adding an entry
cannot change how a hook resolves.

**The ruling store.** `ignore` means "we looked and decided not to block this".
It changes nothing in the app, so nothing in the app records it, so without a
store the candidate returns every port and a human re-decides it every port. This
is its own append-only file rather than a fourth `decisions.RecordKind`, for
exactly the reason `agent_cost` is: that enum says "the three tables. Nothing
else may be stored here", and a ruling is a different fact about a different
subject — not a resolution, and emphatically not a *miss*, which means the
pipeline was confidently wrong.

`defer` is recorded and **does not suppress**. It is an explicit "not decided
yet", so the candidate coming back is the point.

**The app.** A block only happens because `throwIfBlocked` carries the endpoint
literal and a preference key. That is hand-written smali in DFInsta's own source,
and this module **does not write it**. It emits exactly what must be added and
refuses to call a ruling applied until the built APK can be shown to carry it.
The manifest half saying "blocked" while the app does not block is the precise
shape of every inert patch this project has shipped, so the two are kept apart:
`semantic_deps` records the *decision*, and only a static check against the built
DEX records the *fact*.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .contracts import canonical_json
from .feature_gate import SILENT_VERDICT, VERDICTS, FeatureDispositionsV1
from .hook_manifest import ManifestError, load_manifest
from .manifest_patch import (
    DEFAULT_MANIFEST_PATH,
    Refusal,
    serialise,
    write_manifest_atomically,
)

__all__ = [
    "RulingError",
    "Ruling",
    "RulingPlan",
    "DEFAULT_STORE_PATH",
    "BLOCKING_VERDICTS",
    "SUPPRESSING_VERDICTS",
    "read_store",
    "suppressed_candidates",
    "endpoint_of",
    "admitted_dispositions",
    "plan",
    "apply",
    "main",
]


class RulingError(ValueError):
    """Raised when a ruling cannot be read, planned or applied."""


SCHEMA_VERSION = 1

#: Beside `manifest/decisions.jsonl` and `manifest/agent_cost.jsonl`, for the
#: same reason: a decision a human made must survive a clean checkout, and
#: `work/` is gitignored.
DEFAULT_STORE_PATH = Path("manifest/rulings.jsonl")

#: Verdicts that mean the endpoint should stop reaching the network. Both, not
#: just `block`: the project's feature policy makes `offer_toggle` the default
#: shape for anything judged addictive — a switch rather than a silent removal —
#: so the endpoint is guarded either way and the difference is whether the
#: preference defaults on.
BLOCKING_VERDICTS = frozenset({"block", "offer_toggle"})

#: Verdicts that stop a candidate being surfaced again. `defer` is deliberately
#: absent: it is an explicit "not decided yet", so it coming back is the point,
#: and suppressing it would turn indecision into a silent no.
SUPPRESSING_VERDICTS = frozenset({SILENT_VERDICT})

#: `assessment.assess_gap` mints `gap:{literal}`. Split rather than stripped by
#: length, so a candidate id in some other namespace is refused instead of
#: silently yielding a wrong endpoint.
CANDIDATE_NAMESPACE = "gap:"

REFUSE_UNKNOWN_VERDICT = "unknown_verdict"
REFUSE_NOT_A_GAP = "candidate_is_not_an_endpoint_gap"
REFUSE_NO_HOOK = "no_hook_declares_this_family"
REFUSE_ALREADY_BLOCKED = "already_in_semantic_deps"
REFUSE_REFORMATS = "would_reformat_the_manifest"
REFUSE_UNCONFIRMED = "unconfirmed"
REFUSE_PLAN_REFUSED = "plan_refused"
REFUSE_STALE = "manifest_changed_since_plan"
REFUSE_DOES_NOT_LOAD = "patched_manifest_does_not_load"


def endpoint_of(candidate_id: str) -> str:
    """The endpoint literal a `gap:` candidate names.

    Refuses anything else rather than guessing. A candidate id in another
    namespace is a candidate this module does not know how to act on, and acting
    on it wrongly would put a made-up literal into the manifest.
    """
    if not isinstance(candidate_id, str) or not candidate_id.startswith(CANDIDATE_NAMESPACE):
        raise RulingError(
            f"{candidate_id!r} is not a {CANDIDATE_NAMESPACE!r} candidate, so this module "
            "cannot say which endpoint it means"
        )
    literal = candidate_id[len(CANDIDATE_NAMESPACE) :]
    if not literal.strip():
        raise RulingError(f"{candidate_id!r} names no endpoint")
    return literal


@dataclass(frozen=True)
class Ruling:
    """One human verdict on one candidate, and the evidence it was made against."""

    candidate_id: str
    verdict: str
    rationale: str
    run_id: str
    decision_id: str
    assessment_sha256: str
    policy_revision: str
    recorded_at: str

    def __post_init__(self) -> None:
        if self.verdict not in VERDICTS:
            raise RulingError(
                f"{self.candidate_id}: {self.verdict!r} is not one of {', '.join(VERDICTS)}"
            )
        for value, label in (
            (self.candidate_id, "candidate id"),
            (self.run_id, "run id"),
            (self.decision_id, "decision id"),
            (self.assessment_sha256, "assessment digest"),
            (self.policy_revision, "policy revision"),
            (self.recorded_at, "timestamp"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise RulingError(f"a ruling needs a {label}")

    @property
    def blocking(self) -> bool:
        return self.verdict in BLOCKING_VERDICTS

    @property
    def suppresses(self) -> bool:
        return self.verdict in SUPPRESSING_VERDICTS

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "verdict": self.verdict,
            "rationale": self.rationale,
            "run_id": self.run_id,
            "decision_id": self.decision_id,
            "assessment_sha256": self.assessment_sha256,
            "policy_revision": self.policy_revision,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Ruling:
        missing = sorted(
            {
                "candidate_id",
                "verdict",
                "rationale",
                "run_id",
                "decision_id",
                "assessment_sha256",
                "policy_revision",
                "recorded_at",
            }
            - set(data)
        )
        if missing:
            raise RulingError(f"ruling record is missing {', '.join(missing)}")
        return cls(**{key: data[key] for key in data})


def read_store(path: Path | str = DEFAULT_STORE_PATH) -> list[Ruling]:
    """Every recorded ruling, oldest first. A missing store is an empty one."""
    path = Path(path)
    if not path.exists():
        return []
    out: list[Ruling] = []
    with open(path, "r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as error:
                raise RulingError(f"{path}:{number}: unreadable ruling: {error}") from error
            if entry.get("schema_version") != SCHEMA_VERSION:
                raise RulingError(
                    f"{path}:{number}: unsupported ruling schema {entry.get('schema_version')!r}"
                )
            out.append(Ruling.from_dict(entry["record"]))
    return out


def suppressed_candidates(
    policy_revision: str, path: Path | str = DEFAULT_STORE_PATH
) -> dict[str, Ruling]:
    """Candidates a human ruled on that should not be surfaced again.

    **Scoped to the policy revision**, so changing the policy brings every
    previously-ignored candidate back for a fresh decision rather than carrying a
    judgement made under rules that no longer apply. That is the same dimension
    `decisions.reusable` makes a resolution's reuse depend on.

    Latest ruling per candidate wins, because the store is append-only and a
    human is allowed to change their mind.
    """
    out: dict[str, Ruling] = {}
    for ruling in read_store(path):
        if ruling.policy_revision != policy_revision:
            continue
        if ruling.suppresses:
            out[ruling.candidate_id] = ruling
        else:
            # A later non-suppressing ruling un-suppresses: `defer` after
            # `ignore` means the human reopened it.
            out.pop(ruling.candidate_id, None)
    return out


def append_rulings(path: Path | str, rulings: Sequence[Ruling]) -> None:
    """Append to the store. Never rewrites, never deduplicates.

    A human changing their mind is a second record, not an edit — the history of
    what was decided and when is the whole value of keeping this.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        for ruling in rulings:
            handle.write(
                canonical_json({"schema_version": SCHEMA_VERSION, "record": ruling.to_dict()})
                + "\n"
            )


# ------------------------------------------------------------------- the plan


@dataclass(frozen=True)
class RulingPlan:
    """What a set of rulings would change, before anything changes.

    Three destinations kept apart on purpose. :attr:`document_after` is the whole
    manifest as it would be written, so what a reader reviews is provably the
    bytes :func:`apply` writes. :attr:`store` is what would be recorded.
    :attr:`custom_code` is what a human must add to DFInsta's own source, which
    this module states and does not write.
    """

    manifest_path: Path
    rulings: tuple[Ruling, ...]
    document_before: str
    document_after: str | None
    #: `(endpoint, hook_id)` pairs the manifest would gain.
    manifest_additions: tuple[tuple[str, str], ...] = ()
    #: Rulings that would be recorded, whether or not they change the manifest.
    store: tuple[Ruling, ...] = ()
    #: `(endpoint, preference_key)` the app must gain before the ruling is real.
    custom_code: tuple[tuple[str, str], ...] = ()
    refusals: tuple[Refusal, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.document_after is None and not self.refusals:
            raise RulingError(
                "a plan that changes nothing must say why; silence is the one state "
                "that cannot be reviewed"
            )

    @property
    def refused(self) -> bool:
        return bool(self.refusals)

    @property
    def changes_manifest(self) -> bool:
        return bool(self.manifest_additions)

    def describe(self) -> str:
        lines: list[str] = []
        for ruling in self.rulings:
            lines.append(f"  {ruling.candidate_id:38s} {ruling.verdict}")
        if self.manifest_additions:
            lines.append("\nmanifest additions (semantic_deps):")
            for endpoint, hook_id in self.manifest_additions:
                lines.append(f"  {endpoint:38s} -> {hook_id}")
        if self.custom_code:
            lines.append(
                "\nthe app does NOT block these yet. Until `throwIfBlocked` carries them "
                "and the built DEX is shown to, the manifest records a decision and not a fact:"
            )
            for endpoint, preference in self.custom_code:
                lines.append(f"  {endpoint:38s} preference {preference}")
        if self.store:
            lines.append(f"\nwould record {len(self.store)} ruling(s)")
        for refusal in self.refusals:
            lines.append(f"\nREFUSED [{refusal.code}] {refusal.reason}")
        for note in self.notes:
            lines.append(f"\nnote: {note}")
        return "\n".join(lines)


def preference_key_for(endpoint: str) -> str:
    """The preference a toggle for this endpoint would use.

    Proposed, never invented into the manifest: a key that does not match what
    `throwIfBlocked` and the settings dialog actually use is a toggle that reads
    a preference nobody sets, which is a switch that does nothing. It is emitted
    for a human to reconcile against the existing keys.
    """
    stem = endpoint.strip("/").replace("/", "_").replace("-", "_")
    return f"disable_{stem}" if stem else "disable_unknown"


def _hook_for(data: Mapping[str, Any], endpoint: str) -> str | None:
    """Which hook's `semantic_deps` should gain this endpoint.

    The hook that already guards the same *family* of endpoints, identified by
    it declaring URI-path rules at all. `tigon_url_block` is the one that blocks
    by URL today; a future second one would be found the same way rather than by
    name, because naming it here would make this module wrong the day the
    manifest gains another.
    """
    from .assessment import looks_like_uri_rule  # noqa: PLC0415

    for entry in data.get("hooks", ()):
        deps = entry.get("semantic_deps") or ()
        if any(looks_like_uri_rule(dep) for dep in deps):
            return entry.get("hook_id")
    return None


def plan(
    dispositions: FeatureDispositionsV1,
    *,
    run_id: str,
    decision_id: str,
    recorded_at: str,
    manifest_path: Path | str = DEFAULT_MANIFEST_PATH,
) -> RulingPlan:
    """Turn an admitted dispositions document into a reviewable change.

    Reads the *document*, not a caller's summary of it, so what is planned is
    what the human signed.
    """
    manifest_path = Path(manifest_path)
    try:
        before = manifest_path.read_text(encoding="utf-8")
        data = json.loads(before)
    except (OSError, json.JSONDecodeError) as error:
        raise RulingError(f"{manifest_path}: cannot read the manifest: {error}") from error

    refusals: list[Refusal] = []
    notes: list[str] = []
    rulings: list[Ruling] = []
    for item in dispositions.dispositions:
        try:
            rulings.append(
                Ruling(
                    candidate_id=item.candidate_id,
                    verdict=item.verdict,
                    rationale=item.rationale,
                    run_id=run_id,
                    decision_id=decision_id,
                    assessment_sha256=dispositions.assessment_sha256,
                    policy_revision=dispositions.policy_revision,
                    recorded_at=recorded_at,
                )
            )
        except RulingError as error:
            refusals.append(Refusal(REFUSE_UNKNOWN_VERDICT, str(error)))

    if serialise(data) != before:
        # The same guard `manifest_patch` carries: if the file does not round-trip
        # through this serialiser, applying would reformat everything and make the
        # review of a two-line change worthless.
        refusals.append(
            Refusal(
                REFUSE_REFORMATS,
                f"{manifest_path} does not round-trip through this module's serialiser, so "
                "writing it would reformat parts nobody reviewed",
            )
        )

    additions: list[tuple[str, str]] = []
    custom: list[tuple[str, str]] = []
    if not refusals:
        for ruling in rulings:
            if not ruling.blocking:
                continue
            try:
                endpoint = endpoint_of(ruling.candidate_id)
            except RulingError as error:
                refusals.append(Refusal(REFUSE_NOT_A_GAP, str(error)))
                continue
            hook_id = _hook_for(data, endpoint)
            if hook_id is None:
                refusals.append(
                    Refusal(
                        REFUSE_NO_HOOK,
                        f"no hook declares URI-path rules, so there is nowhere to record "
                        f"that {endpoint} is blocked",
                    )
                )
                continue
            entry = next(h for h in data["hooks"] if h.get("hook_id") == hook_id)
            if endpoint in (entry.get("semantic_deps") or ()):
                notes.append(
                    f"{endpoint} is already in {hook_id}'s semantic_deps; the ruling is "
                    "recorded and the manifest is unchanged"
                )
                continue
            additions.append((endpoint, hook_id))
            custom.append((endpoint, preference_key_for(endpoint)))

    if refusals:
        return RulingPlan(
            manifest_path=manifest_path,
            rulings=tuple(rulings),
            document_before=before,
            document_after=None,
            refusals=tuple(refusals),
            notes=tuple(notes),
        )

    after = json.loads(before)
    for endpoint, hook_id in additions:
        entry = next(h for h in after["hooks"] if h.get("hook_id") == hook_id)
        entry.setdefault("semantic_deps", [])
        entry["semantic_deps"] = [*entry["semantic_deps"], endpoint]
    return RulingPlan(
        manifest_path=manifest_path,
        rulings=tuple(rulings),
        document_before=before,
        document_after=serialise(after),
        manifest_additions=tuple(additions),
        store=tuple(rulings),
        custom_code=tuple(custom),
        notes=tuple(notes),
    )


def apply(
    plan_: RulingPlan,
    *,
    confirm: bool,
    store_path: Path | str = DEFAULT_STORE_PATH,
) -> tuple[bool, tuple[Refusal, ...]]:
    """Record the rulings and, if any block, write the manifest. Refuses otherwise.

    Returns `(written, refusals)`. **Never writes the custom code**: what the app
    must gain is stated by the plan and added by a human, and until the built DEX
    carries it the manifest records a decision rather than a fact.
    """
    refusals: list[Refusal] = []
    if plan_.refused:
        refusals.append(
            Refusal(REFUSE_PLAN_REFUSED, "the plan was refused; nothing may be applied from it")
        )
    if not confirm:
        refusals.append(
            Refusal(
                REFUSE_UNCONFIRMED,
                "applying a human's rulings changes what the app blocks; pass confirm",
            )
        )
    current = plan_.manifest_path.read_text(encoding="utf-8")
    if current != plan_.document_before:
        refusals.append(
            Refusal(
                REFUSE_STALE,
                f"{plan_.manifest_path} changed since this plan was made; re-plan rather "
                "than overwrite an edit nobody reviewed",
            )
        )
    if refusals:
        return False, tuple(refusals)

    # The store first. If the manifest write then fails, a human's decision is
    # still recorded — the opposite order could block an endpoint with no record
    # of who decided to.
    append_rulings(store_path, plan_.store)
    if not plan_.changes_manifest:
        return False, ()
    assert plan_.document_after is not None
    try:
        write_manifest_atomically(plan_.manifest_path, plan_.document_after)
    except (ManifestError, OSError) as error:
        return False, (Refusal(REFUSE_DOES_NOT_LOAD, str(error)),)
    load_manifest(plan_.manifest_path)
    return True, ()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE_PATH)
    parser.add_argument("--recorded-at", required=True, help="this layer never reads a clock")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="record the rulings and write the manifest. Without it this is a dry run.",
    )
    args = parser.parse_args(argv)

    from . import activities  # noqa: PLC0415

    try:
        activities.configure_runtime(args.state_root, read_only=True)
    except FileNotFoundError as error:
        print(f"error: no ledger under {args.state_root}: {error}", file=sys.stderr)
        return 1

    try:
        configured = activities.runtime()
        document, decision_id = admitted_dispositions(
            configured.ledger, configured.store, args.run_id
        )
        plan_ = plan(
            document,
            run_id=args.run_id,
            decision_id=decision_id,
            recorded_at=args.recorded_at,
            manifest_path=args.manifest,
        )
    except (RulingError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(plan_.describe())
    if not args.apply:
        print("\n(dry run; pass --apply to record and write)")
        return 1 if plan_.refused else 0

    written, refusals = apply(plan_, confirm=True, store_path=args.store)
    for refusal in refusals:
        print(f"REFUSED [{refusal.code}] {refusal.reason}", file=sys.stderr)
    if refusals:
        return 1
    print(f"\nrecorded {len(plan_.store)} ruling(s) to {args.store}")
    print(f"manifest written: {written}")
    return 0


def admitted_dispositions(ledger: Any, store: Any, run_id: str) -> tuple[FeatureDispositionsV1, str]:
    """The rulings this run's gate admitted, fetched by the reference it recorded.

    By reference and never by a path a caller names: reading a document the human
    did not sign would apply rulings nobody made. `read_blob` re-verifies the
    digest and the size it was asked for, so the bytes acted on are the bytes
    whose hash was admitted.

    Ledger and store are passed in, so the caller's one statement about what it
    may do — `configure_runtime(..., read_only=True)` — is not stepped around by
    a second connection opened here.
    """
    row = ledger.admitted_dispositions_for_run(run_id)
    body = store.read_blob(row["dispositions_sha256"], int(row["dispositions_size"]))
    try:
        document = FeatureDispositionsV1.from_dict(json.loads(body.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise RulingError(f"admitted dispositions are not a readable document: {error}") from error
    if document.assessment_sha256 != row["assessment_sha256"]:
        raise RulingError(
            "the admitted dispositions name a different assessment than the row records"
        )
    return document, str(row["decision_id"])

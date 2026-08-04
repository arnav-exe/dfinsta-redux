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
and this module **does not write it** — see the refusals at the bottom of this
docstring for why.

So `semantic_deps` records the *decision* and the source records the *fact*, and
this module reads both. :func:`unenforced_endpoints` is the check: every URI-path
entry on the url-block hook that `throwIfBlocked` does not test. A plan reports
which of its own rulings the app does not yet enforce, and the same function run
over the whole manifest catches the general case — "the manifest says blocked and
the app does not block it", which is the precise shape of every inert patch this
project has shipped.

An earlier version of this docstring claimed the module "refuses to call a ruling
applied until the built APK can be shown to carry it", and nothing in it looked
at a DEX or at the source. That is recorded here rather than quietly corrected,
because a docstring asserting a check nobody wrote is the same failure as a
manifest asserting a block nobody implemented.

===============================================================================
  WHAT THIS MODULE WILL NOT WRITE, AND WHY
===============================================================================

The guard block is ten instructions and looks generable. Three measured reasons
it is emitted for review instead:

* **The match method is not derivable.** Every literal ending `/` uses
  `endsWith` and every one that does not uses `contains` — 13 of 13 across both
  source trees — but that is a record of a per-endpoint judgement about whether
  the live request path carries a suffix, not a rule. Guess `endsWith` wrongly
  and the rule never fires: the patch assembles, static verification passes, and
  the toggle silently does nothing.
* **The preference key is not derivable either**, and for the candidates
  actually on the table a *new* key is probably wrong. `/feed/reels_tray/` is
  `disable_stories`; `/profile_ads/get_profile_ads/` is `disable_adds`.
  :func:`existing_preference_keys` offers what the app already reads.
* **A new toggle is five coordinated edits, one of which fails silently.** The
  settings dialog's index-to-key dispatch is the only per-key code in it, and its
  default branch is a no-op — so a row that renders and animates and writes
  nothing is what a mistake there produces.
"""

from __future__ import annotations

import argparse
import json
import re
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


#: Where the pipeline's custom code lives. `driver.py` defaults `--custom-code`
#: here, and `dfinsta_source_430` is byte-identical, so one path covers both.
DEFAULT_SOURCE_PATH = Path("dfinsta_source_439/newCode/com/dfinstagram/hooks.smali")


def unenforced_endpoints(
    manifest_path: Path | str = DEFAULT_MANIFEST_PATH,
    source_path: Path | str = DEFAULT_SOURCE_PATH,
) -> tuple[str, ...]:
    """URI-path rules the manifest declares blocked that the app does not test.

    The whole point of keeping the decision and the fact apart. Run over the
    shipped manifest today this returns nothing, and it returns something the
    moment a ruling is recorded without the guard being written — which is
    exactly the window this module opens and had no way to close.

    Only the url-block hook's deps are checked. A rewriting hook's literals name
    an endpoint it *replaces*, not one it blocks, and they live at a host call
    site rather than in `throwIfBlocked`.
    """
    from .assessment import looks_like_uri_rule  # noqa: PLC0415

    data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    hook_id = _hook_for(data, "")
    if hook_id is None:
        return ()
    entry = next(h for h in data["hooks"] if h.get("hook_id") == hook_id)
    guarded = guarded_endpoints(source_path)
    return tuple(
        dep
        for dep in entry.get("semantic_deps") or ()
        if looks_like_uri_rule(dep)
        # Containment in both directions: the manifest writes `/feed/timeline/`
        # where a candidate id yields `feed/timeline_stream/`, so exact equality
        # would report a false gap on the very entries that do work.
        and not any(dep in literal or literal in dep for literal in guarded)
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
    #: Endpoints the app does not test yet. Until `throwIfBlocked` gains them the
    #: manifest records a decision and not a fact, and this is the field that
    #: says which.
    custom_code: tuple[str, ...] = ()
    #: The preference keys the app already reads, offered rather than invented.
    preference_keys: tuple[str, ...] = ()
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
                "\nTHE APP DOES NOT BLOCK THESE YET. `throwIfBlocked` does not test them, so "
                "the manifest records a decision and not a fact until it does:"
            )
            for endpoint in self.custom_code:
                lines.append(f"  {endpoint}")
            lines.append(
                "  preference keys the app already reads: "
                + (", ".join(self.preference_keys) or "(none found)")
            )
            lines.append(
                "  a NEW key is probably wrong for these — check whether an existing one "
                "covers the family before adding a toggle, which is five coordinated edits."
            )
        if self.store:
            lines.append(f"\nwould record {len(self.store)} ruling(s)")
        for refusal in self.refusals:
            lines.append(f"\nREFUSED [{refusal.code}] {refusal.reason}")
        for note in self.notes:
            lines.append(f"\nnote: {note}")
        return "\n".join(lines)


PREFERENCE_KEY = re.compile(r'const-string v\d+, "(disable_[a-z_]+)"')
GUARD_LITERAL = re.compile(r'const-string v\d+, "(/?[a-z0-9][a-z0-9/_.-]*)"')


def existing_preference_keys(source: Path | str) -> tuple[str, ...]:
    """Every preference key `throwIfBlocked` already reads, in source order.

    Emitted for a human to choose from, and deliberately *not* narrowed to one:
    for the four candidates actually on the table the right key is almost
    certainly an existing one — `feed/timeline_stream/` belongs under
    `disable_feed`, the reels endpoints under `disable_reels` — so a module that
    minted `disable_feed_timeline_stream` would create a toggle nobody wanted and
    then owe it a declaration in four more places. Deriving a key from an
    endpoint is not possible anyway: `/feed/reels_tray/` is `disable_stories` and
    `/profile_ads/get_profile_ads/` is `disable_adds`.
    """
    text = Path(source).read_text(encoding="utf-8", errors="replace")
    seen: list[str] = []
    for match in PREFERENCE_KEY.finditer(text):
        if match.group(1) not in seen:
            seen.append(match.group(1))
    return tuple(seen)


def guarded_endpoints(source: Path | str) -> frozenset[str]:
    """Every endpoint literal `throwIfBlocked` actually tests.

    This is the app's side of the claim. `semantic_deps` records what a human
    *decided*; this records what the code *does*, and the two disagreeing is the
    shape of every inert patch this project has shipped.
    """
    text = Path(source).read_text(encoding="utf-8", errors="replace")
    body = text.split("throwIfBlocked", 1)[-1].split(".end method", 1)[0]
    return frozenset(
        match.group(1)
        for match in GUARD_LITERAL.finditer(body)
        if not match.group(1).startswith("disable_")
    )


#: The manifest's own word for "this hook blocks by URL". Keyed on rather than
#: matched by shape: the first version of this looked for any hook declaring a
#: URI-path rule and took the first in *file order*, which is `tigon_url_block`
#: today and would silently become `replace_reels_discover_endpoint` — an
#: endpoint-*rewriting* hook — if the manifest were reordered. A blocked endpoint
#: filed under a rewriting hook is a decision recorded in a place nothing reads.
URL_BLOCK_STRATEGY = "url_block"


def _hook_for(data: Mapping[str, Any], endpoint: str) -> str | None:
    """Which hook's `semantic_deps` should gain this endpoint.

    The hook that declares itself a URL blocker. Refuses rather than guesses when
    there is not exactly one: two would make the choice arbitrary, and none means
    there is nowhere to record that an endpoint is blocked.
    """
    blockers = [
        entry.get("hook_id")
        for entry in data.get("hooks", ())
        if entry.get("strategy") == URL_BLOCK_STRATEGY
    ]
    return blockers[0] if len(blockers) == 1 else None


def plan(
    dispositions: FeatureDispositionsV1,
    *,
    run_id: str,
    decision_id: str,
    recorded_at: str,
    manifest_path: Path | str = DEFAULT_MANIFEST_PATH,
    source_path: Path | str = DEFAULT_SOURCE_PATH,
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
    custom: list[str] = []
    # Read once, from the app's own source: what it *actually* tests, and which
    # preference keys it *actually* reads. Both are facts about the built thing,
    # not values this module may invent.
    try:
        guarded = guarded_endpoints(source_path)
        keys = existing_preference_keys(source_path)
    except OSError:
        guarded, keys = frozenset(), ()
        notes.append(
            f"{source_path} could not be read, so this plan cannot say whether the app "
            "already blocks these. An unchecked source is not a checked one."
        )
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
            if not any(endpoint in guard or guard in endpoint for guard in guarded):
                custom.append(endpoint)

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
        preference_keys=keys,
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

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

:func:`undeclared_endpoints` is the same question the other way round — what the
app tests and no hook declares — because either direction alone reads as clean
while the other is not. :func:`audit` runs both, and
``python -m dfinsta_pipeline.rulings --audit`` is where an operator gets the
answer.

**And the artifact.** :func:`required_build_strings` names the literals a build
must carry, for `tools/verify/verify_build.py --required-strings` to find as
bytes in the custom DEX. Both checks above read *text* — a block can be recorded,
guarded in smali, and still not reach the DEX — so this is the only one that
reaches the thing that ships. The driver derives it per run rather than the
verifier pinning it, because a manifest entry is a per-version fact.

An earlier version of this docstring claimed the module "refuses to call a ruling
applied until the built APK can be shown to carry it", and nothing in it looked
at a DEX or at the source. That is recorded here rather than quietly corrected,
because a docstring asserting a check nobody wrote is the same failure as a
manifest asserting a block nobody implemented. **The claim is now true**, but by
three functions written afterwards rather than by the sentence — and it is still
the *verifier* that refuses, not this module.

===============================================================================
  WHAT THIS MODULE WILL NOT WRITE, AND WHY
===============================================================================

The guard block is ten instructions and looks generable. Three measured reasons
it is emitted for review instead:

* **The match method is not derivable.** When this was written, every literal
  ending `/` used `endsWith` and every one that did not used `contains` — 13 of
  13 across both source trees — but that is a record of a per-endpoint judgement
  about whether the live request path carries a suffix, not a rule. **As of
  2026-08-14 five of eleven break it**, all the same way: `/feed/timeline_stream/`,
  `/feed/injected_reels_media/`, `/feed/reels_media/`, `/feed/reels_media_stream/`
  and `/feed/text_post_app_timeline/` end in `/` and are `contains`. A generator
  built on the old regularity would today render five of eleven rules into silent
  no-ops. It did not merely fail to generalise — it inverted, while the note
  recording it still said 13 of 13. Guess `endsWith` wrongly
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
from .feature_gate import (
    CONSENT_ANSWERS,
    SILENT_VERDICT,
    VERDICTS,
    FeatureDispositionsV1,
)
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
#: so the endpoint is guarded either way.
#:
#: This once said the difference was "whether the preference defaults on".
#: **There is nowhere for that to be true**: `getBoolTrueEz` is a single
#: hardcoded `getBoolean(key, true)` for every key, and no per-key default exists
#: anywhere in the shipped tree. A key with no settings row is not off by
#: default — it is blocked permanently, with no switch to change it.
#: `dfinsta_pipeline.settings_ui` refuses a build in that state; until
#: 2026-08-14 nothing did. What separates the two verdicts today is nothing:
#: both append the endpoint to `semantic_deps` and a human writes the rule.
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
    #: Which side of the consent test the human answered, or `None` for a ruling
    #: made before the question was asked. **Optional on purpose**, and the only
    #: optional field on this record. Six rulings were recorded on 2026-08-08
    #: carrying prose alone, and re-reading that prose cannot recover which side
    #: of the test each was on. Backfilling them would mean this code answering a
    #: question a human was never asked, into a committed store — which is the
    #: precise failure of the 36 fabricated rows this project has already shipped
    #: once. `None` says nobody was asked, and that is true.
    consent: str | None = None

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
        if self.consent is not None and self.consent not in CONSENT_ANSWERS:
            raise RulingError(
                f"{self.candidate_id}: {self.consent!r} is not one of "
                f"{', '.join(CONSENT_ANSWERS)}"
            )

    @property
    def blocking(self) -> bool:
        return self.verdict in BLOCKING_VERDICTS

    @property
    def suppresses(self) -> bool:
        return self.verdict in SUPPRESSING_VERDICTS

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "candidate_id": self.candidate_id,
            "verdict": self.verdict,
            "rationale": self.rationale,
            "run_id": self.run_id,
            "decision_id": self.decision_id,
            "assessment_sha256": self.assessment_sha256,
            "policy_revision": self.policy_revision,
            "recorded_at": self.recorded_at,
        }
        # Omitted rather than written as null, so a record made before the
        # question existed round-trips to the bytes already committed. A stored
        # `"consent": null` would be a new fact about six old rulings.
        if self.consent is not None:
            out["consent"] = self.consent
        return out

    #: Required on every record. `consent` is deliberately absent — see
    #: :data:`OPTIONAL_FIELDS`.
    FIELDS = (
        "candidate_id",
        "verdict",
        "rationale",
        "run_id",
        "decision_id",
        "assessment_sha256",
        "policy_revision",
        "recorded_at",
    )

    #: Keys a record MAY carry. The strict-unknown check still applies to
    #: everything outside `FIELDS | OPTIONAL_FIELDS`, so this widens the store by
    #: exactly one name and not by "anything new is fine".
    OPTIONAL_FIELDS = ("consent",)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Ruling:
        """Strict both ways. A missing field and an extra one are both refusals.

        The first version checked only for missing keys and then splatted the
        whole record, so a hand-added `"note"` escaped as a bare `TypeError` —
        past `main`'s handler, and past every caller of `suppressed_candidates`,
        which catch `RulingError` and name none of this. Everything that can
        refuse has to go through the refusal channel or the channel is decorative.
        """
        if not isinstance(data, Mapping):
            raise RulingError("a ruling record must be an object")
        missing = sorted(set(cls.FIELDS) - set(data))
        unknown = sorted(set(data) - set(cls.FIELDS) - set(cls.OPTIONAL_FIELDS))
        if missing:
            raise RulingError(f"ruling record is missing {', '.join(missing)}")
        if unknown:
            raise RulingError(f"ruling record has unknown field(s): {', '.join(unknown)}")
        present = [key for key in (*cls.FIELDS, *cls.OPTIONAL_FIELDS) if key in data]
        return cls(**{key: data[key] for key in present})


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
            if not isinstance(entry, Mapping):
                raise RulingError(f"{path}:{number}: a ruling line must be an object")
            if entry.get("schema_version") != SCHEMA_VERSION:
                raise RulingError(
                    f"{path}:{number}: unsupported ruling schema {entry.get('schema_version')!r}"
                )
            if "record" not in entry:
                raise RulingError(f"{path}:{number}: a ruling line carries no record")
            try:
                out.append(Ruling.from_dict(entry["record"]))
            except RulingError as error:
                raise RulingError(f"{path}:{number}: {error}") from error
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
        # NOT `return ()`. This function's entire job is to notice that the
        # manifest claims a block the app does not implement, and an empty tuple
        # is the same answer it gives when everything agrees — so a manifest with
        # no url-block hook, or two, would read as clean. That is absence
        # reported as a pass, which is the one failure this project refuses
        # everywhere else.
        # `str(...)` because this runs on a raw JSON read, not on a loaded
        # manifest: a hook with no `hook_id` would otherwise make the *error
        # path* raise a TypeError, replacing a legible refusal with a traceback
        # about the refusal.
        blockers = [
            str(entry.get("hook_id"))
            for entry in data.get("hooks", ())
            if entry.get("strategy") == URL_BLOCK_STRATEGY
        ]
        raise RulingError(
            f"{manifest_path} declares {len(blockers)} hooks with strategy "
            f"{URL_BLOCK_STRATEGY!r} ({', '.join(blockers) or 'none'}), so there is no one "
            "hook whose declared blocks can be checked against the app. This is not the "
            "same as nothing being unenforced."
        )
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


def undeclared_endpoints(
    manifest_path: Path | str = DEFAULT_MANIFEST_PATH,
    source_path: Path | str = DEFAULT_SOURCE_PATH,
) -> tuple[str, ...]:
    """Endpoints the app blocks that no hook in the manifest declares.

    The other direction, and the one that was missing. :func:`unenforced_endpoints`
    asks "does the code do what the manifest says?"; this asks "does the manifest
    say what the code does?". A block the manifest omits is invisible to
    `assessment.blocked_endpoints`, so stage 4a proposes a candidate the app
    already blocks and a human is asked to rule on a decision that was taken long
    ago.

    Checked against **every** hook's deps, not just the url-block hook's, because
    a declaration filed under a rewriting hook is still a declaration and
    reporting it would be a false positive.

    Run over the shipped tree this returns `()`. It did not always: when it was
    written it returned `('/clips/discover',)`, a real defect and not a rounding
    error — `throwIfBlocked` tested six endpoints and
    `tigon_url_block.semantic_deps` listed five. The nearest thing anywhere in
    the manifest was `replace_reels_discover_endpoint`'s `clips/discover/`, and
    containment fails in both directions — `/clips/discover` is not inside
    `clips/discover/` because of the trailing slash, and `clips/discover/` is not
    inside `/clips/discover` because of the leading one. `assessment.is_blocked`
    uses the same containment, so the endpoint is covered by neither hook and
    stage 4a would happily propose blocking something the app already blocks.

    Containment in both directions, for the same reason as above. `disable_`
    literals are already filtered by :func:`guarded_endpoints`; they are
    preference keys, not endpoints.
    """
    data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    declared = tuple(
        dep
        for hook in data.get("hooks", ())
        for dep in hook.get("semantic_deps") or ()
        if isinstance(dep, str)
    )
    return tuple(
        sorted(
            literal
            for literal in guarded_endpoints(source_path)
            if not any(literal in dep or dep in literal for dep in declared)
        )
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


def _throw_if_blocked(source: Path | str) -> str:
    """The body of `throwIfBlocked`, and nothing else in the file."""
    text = Path(source).read_text(encoding="utf-8", errors="replace")
    if "throwIfBlocked" not in text:
        raise RulingError(f"{source} declares no throwIfBlocked to read")
    return text.split("throwIfBlocked", 1)[-1].split(".end method", 1)[0]


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
    # Scoped to the method, because that is what the docstring claims. Scanning
    # the whole file offered `disable_reels` first, from `replaceReelsEndpoint`
    # above it — harmless today because the guard reads it too, and wrong the day
    # some other method reads a key this one never does.
    seen: list[str] = []
    for match in PREFERENCE_KEY.finditer(_throw_if_blocked(source)):
        if match.group(1) not in seen:
            seen.append(match.group(1))
    return tuple(seen)


def guarded_endpoints(source: Path | str) -> frozenset[str]:
    """Every endpoint literal `throwIfBlocked` actually tests.

    This is the app's side of the claim. `semantic_deps` records what a human
    *decided*; this records what the code *does*, and the two disagreeing is the
    shape of every inert patch this project has shipped.
    """
    return frozenset(
        match.group(1)
        for match in GUARD_LITERAL.finditer(_throw_if_blocked(source))
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
                    # `""` is the document's "not asked"; `None` is the store's.
                    # They stay distinct types on purpose — the document requires
                    # an answer for the acting verdicts and can therefore carry a
                    # meaningful blank, and the store must be able to say that a
                    # record predates the question entirely.
                    consent=item.consent or None,
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
    except (OSError, RulingError) as error:
        # Both cases, deliberately: a source that is missing and a source that is
        # the wrong file are the same fact — this plan cannot say whether the app
        # blocks these. Raising for one and noting the other would lose the
        # human's rulings entirely over an operator's wrong `--source` path, and
        # this module already settled that priority in `apply`: a decision
        # recorded with no block is recoverable, a block with no record of who
        # decided it is not.
        guarded, keys = frozenset(), ()
        notes.append(
            f"{source_path} could not be read ({error}), so this plan cannot say whether "
            "the app already blocks these. An unchecked source is not a checked one."
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
            covered = [
                dep
                for dep in entry.get("semantic_deps") or ()
                # Containment, not equality — the same comparison
                # `assessment.is_blocked` uses to decide coverage. With equality,
                # a `block` on `feed/timeline/` appended a second rule beside the
                # `/feed/timeline/` that already covers it, and the note that
                # exists to say "nothing to change" never fired.
                if dep in endpoint or endpoint in dep
            ]
            if covered:
                notes.append(
                    f"{endpoint} is already covered by {hook_id}'s {covered[0]!r}; the "
                    "ruling is recorded and the manifest is unchanged"
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


def required_build_strings(
    manifest_path: Path | str = DEFAULT_MANIFEST_PATH,
    source_path: Path | str = DEFAULT_SOURCE_PATH,
) -> tuple[str, ...]:
    """The endpoint literals a build must carry for its rulings to have landed.

    The third direction, and the one that reaches the artifact.
    :func:`unenforced_endpoints` compares the manifest to the app's source and
    :func:`undeclared_endpoints` compares it back; both read text. This is what a
    verifier can check against the *built APK*, where the same literals appear as
    bytes inside DFInsta's own DEX — measured on the shipped 440 release, all six
    present in `classes21.dex`.

    **Declared MINUS unenforced, and that subtraction is load-bearing.** A ruling
    puts an endpoint in `semantic_deps` the moment a human blocks it, which is
    before anybody writes the guard; the literal is then in the manifest and in
    no source file, so it cannot be in the DEX and requiring it fails a build for
    something that is not a build defect. Measured: the six endpoints ruled on
    2026-08-08 made the next 441 port fail its post-build verification on five
    strings, with a correct APK on disk — the first build after that ruling, and
    nothing between the two could have caught it.

    The two questions stay separate rather than merged. "Does the app enforce
    what the manifest declares?" is source-level and belongs to
    :func:`unenforced_endpoints`, which `rulings --audit` exits 1 for. "Did the
    build carry what the app enforces?" is this, and it is the only one an
    artifact can answer. Subtracting here does not hide the first question — it
    stops the second from reporting the first's answer as a build failure.

    Only the url-block hook's deps, for the same reason `unenforced_endpoints`
    restricts itself: a rewriting hook's literals name an endpoint it replaces,
    and the replacement is what reaches the DEX, not the original.

    Refuses rather than returning `()` when there is no single url-block hook,
    because a caller that gets an empty tuple would pass it to a verifier which
    would then prove nothing — the same absence-as-a-pass this module refuses
    everywhere else. It refuses on an empty *result* too: a manifest whose every
    declared block is unenforced is a manifest that can require nothing, and a
    verifier handed nothing proves nothing.
    """
    from .assessment import looks_like_uri_rule  # noqa: PLC0415

    data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    hook_id = _hook_for(data, "")
    if hook_id is None:
        raise RulingError(
            f"{manifest_path} has no single hook with strategy {URL_BLOCK_STRATEGY!r}, so "
            "there is no set of literals a build can be required to carry. An empty "
            "requirement would make the verifier's check pass vacuously."
        )
    entry = next(h for h in data["hooks"] if h.get("hook_id") == hook_id)
    declared = tuple(
        dep for dep in entry.get("semantic_deps") or () if looks_like_uri_rule(dep)
    )
    # Through `unenforced_endpoints` rather than re-deriving the containment
    # comparison, so the set this requires and the set the audit reports can
    # never drift apart: they are one subtraction of one function's output.
    unenforced = set(unenforced_endpoints(manifest_path, source_path))
    required = tuple(dep for dep in declared if dep not in unenforced)
    if not required:
        raise RulingError(
            f"{manifest_path} declares {len(declared)} URI-path blocks and "
            f"{source_path} enforces none of them, so there is no literal a build can "
            "be required to carry. This is not the same as a build having nothing to "
            "prove — it is a manifest whose every block is still unwritten."
        )
    return required


def audit(
    manifest_path: Path | str = DEFAULT_MANIFEST_PATH,
    source_path: Path | str = DEFAULT_SOURCE_PATH,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Both directions of manifest-versus-app agreement, in one call.

    Kept together because either one alone reads as clean while the other is not.
    Returns `(unenforced, undeclared)`: what the manifest claims and the app does
    not do, and what the app does and the manifest does not record.
    """
    return (
        unenforced_endpoints(manifest_path, source_path),
        undeclared_endpoints(manifest_path, source_path),
    )


def describe_audit(unenforced: Sequence[str], undeclared: Sequence[str]) -> str:
    lines = ["manifest and app agreement", ""]
    if not unenforced and not undeclared:
        lines.append("  the manifest's blocks and the app's guards agree in both directions")
        return "\n".join(lines)
    if unenforced:
        lines.append("  declared blocked, NOT tested by throwIfBlocked:")
        lines.extend(f"    {endpoint}" for endpoint in unenforced)
        lines.append("    -> the manifest records a decision the app does not implement")
    if undeclared:
        if unenforced:
            lines.append("")
        lines.append("  tested by throwIfBlocked, NOT declared in any hook:")
        lines.extend(f"    {endpoint}" for endpoint in undeclared)
        lines.append(
            "    -> assessment.blocked_endpoints cannot see these, so stage 4a will"
        )
        lines.append("       propose blocking what the app already blocks")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # Not `required=True`: `--audit` needs none of them, and argparse cannot say
    # "required unless". Checked below so a missing argument is a refusal rather
    # than a usage error about flags the chosen mode does not use.
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE_PATH)
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE_PATH)
    parser.add_argument("--recorded-at", help="this layer never reads a clock")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="record the rulings and write the manifest. Without it this is a dry run.",
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="check the manifest against the app in BOTH directions and exit. "
        "Needs no ledger, no run and no clock. Exit 1 if either direction "
        "disagrees.",
    )
    args = parser.parse_args(argv)

    if args.audit:
        try:
            unenforced, undeclared = audit(args.manifest, args.source)
        except (RulingError, OSError, ValueError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        print(describe_audit(unenforced, undeclared))
        return 1 if (unenforced or undeclared) else 0

    missing = [
        name
        for name, value in (
            ("--state-root", args.state_root),
            ("--run-id", args.run_id),
            ("--recorded-at", args.recorded_at),
        )
        if value is None
    ]
    if missing:
        print(
            f"error: applying rulings needs {', '.join(missing)}. To check the "
            "manifest against the app without a run, pass --audit.",
            file=sys.stderr,
        )
        return 1

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
    try:
        body = store.read_blob(row["dispositions_sha256"], int(row["dispositions_size"]))
    except OSError as error:
        # The ledger row and the blob can be restored apart. A raw errno naming a
        # two-character shard directory is a correct refusal nobody can read.
        raise RulingError(
            f"the rulings admitted for {run_id} name {row['dispositions_sha256']}, which is "
            f"not in this content store: {error}"
        ) from error
    try:
        document = FeatureDispositionsV1.from_dict(json.loads(body.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise RulingError(f"admitted dispositions are not a readable document: {error}") from error
    if document.assessment_sha256 != row["assessment_sha256"]:
        raise RulingError(
            "the admitted dispositions name a different assessment than the row records"
        )
    return document, str(row["decision_id"])


# `main` had no caller: no `__main__` guard here and no console script in
# `pyproject.toml`, so `python -m dfinsta_pipeline.rulings` imported the module,
# ran nothing and exited 0. Every other CLI in this package ends this way.
if __name__ == "__main__":
    raise SystemExit(main())

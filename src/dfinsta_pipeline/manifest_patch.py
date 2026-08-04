"""Stage 10, the committing half: turn a verified proposal into a manifest edit — or refuse.

`generalise` proposes and a human commits. That default is right — promoting a
host on one run's strength is the confident-and-wrong failure this project keeps
paying for — but until now the step *between* the proposal and
`manifest/hooks.json` was a person reading a JSON file and retyping part of it.
It worked once: the agent count fell 2 -> 0 between 439 and 440 because a human
read what 439's proposers cited and hand-wrote two `by_anchor` entries. Nothing
about that step was written down, so the next fingerprint would be derived the
same ad-hoc way, and the one thing the hand-edit never did was *prove* the new
fingerprint still selects the host the old one selected.

This module is that step, and it is written as a wall of refusals rather than as
an editor. The edit itself is four lines of `json`; everything else here exists
because a manifest edit is the one artifact in this pipeline that outlives the
run that produced it.

**A refusal is a returned value with a reason; a failure is an exception.**
:func:`plan` always returns a :class:`Patch`. A refused patch carries
:attr:`Patch.refusals`, each naming a code and the measurement behind it, and
:func:`apply` will not write one. A `Patch` that neither refuses nor carries a
document to write is itself refused at construction, because silence reads as
success.

**What is refused, and why each one is not decoration:**

  * **A proposal checked against one version.** The systrace literal
    ``ProfileActionBarViewBinder.bindUsernameTitle…`` selects exactly one class on
    439 and it is the right one; exactly one class on 430 and it is the *wrong*
    one; and exactly one class on 440 and it is the right one again. Every
    single-version check passes it. The refusal keys on
    :attr:`generalise.Proposal.selections` — the versions the proposal recorded a
    measurement for — never on a caller's assurance, because the caller is the
    thing being checked.

  * **A fingerprint that would not select the recorded host.** Checked twice, on
    purpose. :func:`plan` re-reads the verdicts the proposal carries, which
    `generalise.Proposal.__post_init__` already refuses to construct around;
    removing either check alone changes nothing, and that is the point — the
    builder fails fast and this stage refuses a hand-built proposal that never
    went through the builder. :func:`verify` then runs the real resolver against
    real decodes, which is the check that can actually be wrong.

  * **A forbidden value.** An obfuscated descriptor, a resource id, an absolute
    path. `LX/05t2;` names a different class in each version, 103 of 11,737
    drawable ids survived 430->439, and an absolute path names one machine's
    workspace. A patch carrying one is refused *and never materialised*: there is
    no document on the returned Patch for a careless caller to write.

    The ``note`` is held to a weaker rule — resource ids and absolute paths only —
    because the shipped manifest's own host notes cite version-stamped
    descriptors (``439 -> LX/0DnT; (1 of 181,421 classes)``) as measurements, and
    a rule that refused those would refuse every real proposal for a reason the
    manifest itself disproves. A descriptor in a *value* is a join key; a
    descriptor in a note, next to the version it was measured on, is a
    measurement. See :func:`forbidden_in_value` and :func:`forbidden_in_note`.

    **Which fields carry a value is a table, not an inspection**
    (:data:`VALUE_FIELDS`), and a kind absent from it is refused rather than
    treated as carrying none. A ``by_anchor`` promotion genuinely has nothing to
    scrub — the fingerprint is the hook's own anchor, already in the manifest and
    already the text the patch is spliced into — so the patch records that the
    rules ran against nothing and :attr:`Patch.value_rule_note` states why.
    "The rules found nothing wrong" and "there was nothing for the rules to look
    at" are otherwise the same silence.

  * **A fingerprint no stronger than the one already there.** See
    :data:`STRENGTH`. A `named` host that resolves today costs nothing and depends
    on nothing; swapping it for a `by_literal` buys no agent invocation back and
    adds a dependency on the API-surface index.

  * **A proposal that would not actually retire the agent.**
    :attr:`generalise.Proposal.blocks` records what would refuse the commit
    elsewhere, and the real 439 `by_literal` proposal carries three — including
    `resolve.search_hosts`, which would make the hook resolve to ZERO candidates
    and escalate anyway. Committing that would move nothing except the diff.

**Absence is never a pass.** :func:`verify` reports one result per version, and a
version it was given no decode for comes back ``unchecked`` — a distinct status
that is not ``ok``, and :func:`apply` refuses on it. "I could not check 430" must
never read as "430 is fine", which is the shape of every absence-assertion bug
this project has shipped.

**The formatting is part of the review.** The manifest is written by
``json.dumps(data, indent=2, ensure_ascii=False)`` and round-trips through it
byte for byte. :func:`plan` asserts that round-trip before building anything, and
refuses if it fails, because a write-back that reformats the whole file makes the
human review of the one changed entry worthless. Every unrelated byte is
identical after a patch by construction rather than by care.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .agent_cost import ABSOLUTE_PATH, DESCRIPTOR
from .generalise import KINDS, Proposal, Rejection, Selection, forbidden_reason
from .hook_index import HookIndex, IndexUnusable
from .hook_manifest import ManifestError, load_manifest
from .manifest_update import RESOURCE_ID, is_stable_named_type, require_version
from .resolve import resolve_hook

SCHEMA_VERSION = 1

#: The manifest this stage exists to edit. Named so the CLI's default and the
#: tests' refusal to touch it are the same string.
DEFAULT_MANIFEST_PATH = Path("manifest/hooks.json")


class PatchError(ValueError):
    """Raised when a caller hands this module something it must not act on.

    A *failure*, never a finding. Everything this stage learns about a proposal
    or a manifest comes back as a :class:`Refusal` instead.
    """


# ---------------------------------------------------------------- serialisation


def serialise(data: Any) -> str:
    """The manifest's own on-disk form. One definition, used to read and to write.

    `manifest/hooks.json` round-trips through exactly this call. Keeping it in
    one place is what lets :func:`plan` *assert* the round-trip rather than hope
    for it, and what makes every byte outside the patched entry identical after a
    write instead of merely intended to be.
    """
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


# --------------------------------------------------------------- the strength law

#: How much a host fingerprint is worth, weakest first. A patch must move a hook
#: strictly UP this ladder; :data:`REFUSE_NOT_STRONGER` is what refuses the rest.
#:
#: The order is argued from this project's own measurements, not from taste:
#:
#:   0. ``named`` at an obfuscated descriptor. `manifest_update._technique` says
#:      it outright: "that is not a fingerprint". Every 430 host name still exists
#:      in 439 naming a different class, so this route resolves *confidently* to
#:      the wrong class — worse than not resolving, and the only entry below
#:      `by_agent`.
#:   1. ``by_agent``. Honest and useless: it costs one agent invocation per port,
#:      and it is the number stage 10 exists to drive down.
#:   2. ``by_literal``. API-path literals survived 430->439 at 93.9%, the highest
#:      measured of any signal that identifies a class. It resolves through
#:      `resolve.search_hosts` -> `HookIndex.descriptors_with_literal`, so it
#:      carries a second condition the others do not: the literal must also be
#:      *indexed*, and the index holds only API-path-shaped strings.
#:   3. ``named`` at a stable named type. 89.3% measured survival — lower than a
#:      literal — but ranked above it because that number measures the SIGNAL and
#:      this ladder ranks the ROUTE. A stable name resolves through one index
#:      lookup with no second condition attached, which is why a working `named`
#:      host is never churned into a `by_literal` one.
#:   4. ``by_anchor``. Nothing is carried across versions at all: the pattern is
#:      re-matched against the target decode every port, and what it matches is
#:      the site the patch is spliced into — so a version where it stops
#:      identifying the host is a version where it stops identifying the site, and
#:      the hook escalates rather than resolving somewhere wrong. It is the kind
#:      that took the 439->440 agent count to zero.
STRENGTH: dict[str, int] = {
    "named_obfuscated": 0,
    "by_agent": 1,
    "by_literal": 2,
    "named": 3,
    "by_anchor": 4,
}


def strength_key(entry: Mapping[str, Any]) -> str:
    """Which rung of :data:`STRENGTH` one `hosts` entry sits on.

    ``named`` splits in two, because the same kind means opposite things
    depending on the descriptor: `Lcom/instagram/api/tigon/TigonServiceLayer;`
    survived 430->439->440 unchanged, and `LX/05t2;` names a different class in
    every version.
    """
    kind = entry.get("kind")
    if kind == "named" and not is_stable_named_type(str(entry.get("descriptor") or "")):
        return "named_obfuscated"
    if kind not in STRENGTH:
        raise PatchError(
            f"host fingerprint kind {kind!r} has no place in the strength ladder. A kind "
            "this stage cannot rank is a kind it cannot say a patch improves, and "
            "guessing would let a promotion through on a comparison nobody made"
        )
    return str(kind)


def strength_of(entry: Mapping[str, Any]) -> int:
    return STRENGTH[strength_key(entry)]


# ------------------------------------------------------------- forbidden values


def _obfuscated_descriptors(value: str) -> list[str]:
    return [found for found in DESCRIPTOR.findall(value) if not is_stable_named_type(found)]


def forbidden_in_value(value: object) -> str:
    """Why *value* may never be written into a host fingerprint, or ``""``.

    Three guards, none of which subsumes another.
    :func:`generalise.forbidden_reason` refuses by shape (registers, member
    names, hex constants, descriptors, anything too short to discriminate);
    :data:`manifest_update.RESOURCE_ID` catches an application resource id
    embedded in a longer string; :data:`agent_cost.ABSOLUTE_PATH` catches one
    machine's workspace, which is how a decode path reaches a record nobody wrote
    it into.
    """
    refusal = forbidden_reason(value)
    if refusal:
        return refusal
    text = str(value)
    if RESOURCE_ID.search(text):
        return (
            f"{text!r} contains an application resource id. 103 of 11,737 drawable names "
            "present in both 430 and 439 kept their id — 0.9%"
        )
    leaked = _obfuscated_descriptors(text)
    if leaked:
        return (
            f"{text!r} names obfuscated descriptor(s) {leaked}. Every 430 host name still "
            "exists in 439 and names a different class, so a descriptor written into a "
            "fingerprint is a join key that returns the wrong class rather than a miss"
        )
    if ABSOLUTE_PATH.search(text):
        return (
            f"{text!r} contains an absolute path. It names one machine's workspace and the "
            "next port cannot open it"
        )
    return ""


#: Which fields of a `hosts` entry carry a value that must survive
#: :func:`forbidden_in_value`, per fingerprint kind. Spelled out per kind, and a
#: kind absent from this table is REFUSED rather than defaulted to "nothing to
#: check" — defaulting is how a new fingerprint kind would acquire an exemption
#: from the scrub rules that nobody ever argued for.
#:
#: The two empty entries are empty for a stated reason, not by omission, and
#: :attr:`Patch.value_rule_note` prints that reason so an empty check never reads
#: as a passed one:
#:
#:   * ``by_anchor`` carries no value at all. The fingerprint IS the hook's own
#:     anchor — already in the manifest, and the same text the patch is spliced
#:     into — so a `by_anchor` promotion introduces no new string for a rule to
#:     refuse. That is also why it is the strongest kind: nothing is carried
#:     between versions, and a version where the anchor stops identifying the
#:     host is a version where it stops identifying the site.
#:   * ``by_agent`` names nothing; it is the absence of a fingerprint.
VALUE_FIELDS: dict[str, tuple[str, ...]] = {
    "named": ("descriptor",),
    "by_literal": ("literal", "co_literals"),
    "by_anchor": (),
    "by_agent": (),
}

#: Why a kind has no value to scrub. Required for every empty entry above, so
#: "this kind is exempt" can never be a blank line in the table.
NO_VALUE_REASON: dict[str, str] = {
    "by_anchor": (
        "the fingerprint is the hook's own anchor, which is already in the manifest and "
        "is the text the patch is spliced into; the proposal introduces no new string"
    ),
    "by_agent": "a by_agent fingerprint names nothing; it is the absence of one",
}


def forbidden_in_note(note: object) -> str:
    """Why *note* may never be written, or ``""``.

    Weaker than :func:`forbidden_in_value` by exactly one rule, and the exception
    is argued rather than convenient: the shipped manifest's host notes cite
    version-stamped descriptors as measurements (``439 -> LX/0DnT; (1 of 181,421
    classes)``), and that is the evidence a human reviewing the entry needs. What
    is refused here is what is a leak in any context — a resource id, which is
    false 99% of the time by the next port, and an absolute path, which names a
    machine.
    """
    if not isinstance(note, str):
        return f"a note is a string, not a {type(note).__name__}"
    if RESOURCE_ID.search(note):
        return (
            f"the note contains an application resource id ({note[:80]!r}…). Ids do not "
            "survive a version step — 103 of 11,737 — so citing one dates the note to a "
            "decode nobody will have"
        )
    if ABSOLUTE_PATH.search(note):
        return (
            f"the note contains an absolute path ({note[:80]!r}…), which names one "
            "machine's workspace rather than anything about the hook"
        )
    return ""


# ---------------------------------------------------------------- the refusals

REFUSE_NO_FINGERPRINT = "no_fingerprint"
REFUSE_UNKNOWN_HOOK = "unknown_hook"
REFUSE_SEVERAL_HOSTS = "several_host_fingerprints"
REFUSE_ONE_VERSION = "corroborated_on_one_version"
REFUSE_SELECTION_NOT_EXACT = "selection_not_exact"
REFUSE_FORBIDDEN_VALUE = "forbidden_value"
REFUSE_NO_VALUE_RULE = "no_value_rule_for_this_kind"
REFUSE_NOT_STRONGER = "not_stronger_than_current"
REFUSE_BLOCKED = "would_not_retire_the_agent"
REFUSE_REFORMATS = "would_reformat_the_manifest"
#: Raised at apply time only.
REFUSE_UNCONFIRMED = "unconfirmed"
REFUSE_PLAN_REFUSED = "plan_refused"
REFUSE_STALE = "manifest_changed_since_plan"
REFUSE_UNVERIFIED = "unverified"
REFUSE_DOES_NOT_LOAD = "patched_manifest_does_not_load"

#: The minimum number of versions a fingerprint must have been measured on. Two,
#: because one cannot distinguish a fingerprint from a coincidence and this
#: project has the measurement to prove it: the 439 systrace literal selected one
#: class on 439 (right), one on 430 (WRONG) and one on 440 (right again).
MIN_CORROBORATING_VERSIONS = 2


@dataclass(frozen=True)
class Refusal:
    """One reason a patch may not be planned or applied, with the measurement behind it."""

    code: str
    reason: str

    def __post_init__(self) -> None:
        if not self.code.strip() or not self.reason.strip():
            raise PatchError(
                "a refusal needs a code and a reason; a bare refusal is indistinguishable "
                "from a stage that stopped for no stated cause"
            )

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "reason": self.reason}


# -------------------------------------------------------------------- the patch


@dataclass(frozen=True)
class Patch:
    """One reviewable manifest edit: what is there, what would replace it, and the bytes.

    Carries the whole before- and after-document rather than a recipe for
    producing one. Two reasons, both about the same hazard: what a human reviews
    in :meth:`diff` is then provably the bytes :func:`apply` writes, and
    :func:`apply` can refuse a manifest that changed since the patch was planned
    instead of overwriting someone else's edit.

    :attr:`expected_hosts` is version-stamped and is an *expectation to check*,
    exactly like `generalise.KnownHost`: it is what the proposal measured the
    fingerprint against, it never reaches the manifest, and :func:`verify` uses it
    only as the answer key for a hook that resolves to nothing today.
    """

    hook_id: str
    manifest_path: Path
    #: The `hosts` entry as the manifest has it now, or ``None`` when the hook or
    #: its host could not be located at all.
    current: Mapping[str, Any] | None
    #: The `hosts` entry that would replace it, or ``None`` when the patch was
    #: refused before anything was materialised.
    proposed: Mapping[str, Any] | None
    #: Versions the proposal recorded a measurement for. What :func:`verify` must
    #: cover and what :func:`apply` refuses to proceed without.
    versions: tuple[str, ...] = ()
    #: ``version -> the host that version is known to use``. Never written down.
    expected_hosts: tuple[tuple[str, str], ...] = ()
    #: Which fields of the proposed entry were actually run through
    #: :func:`forbidden_in_value`. Recorded rather than derived, because for a
    #: `by_anchor` promotion it is empty and an empty scrub must be visibly empty:
    #: "the rules found nothing wrong" and "there was nothing for the rules to
    #: look at" are the same silence otherwise. See :attr:`value_rule_note`.
    checked_values: tuple[str, ...] = ()
    document_before: str = ""
    document_after: str = ""
    refusals: tuple[Refusal, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "manifest_path", Path(self.manifest_path))
        if not self.hook_id.strip():
            raise PatchError("a patch must name the hook it edits")
        if not self.refusals and not self.document_after:
            # The one state that must not exist. A caller that got neither a
            # writable patch nor a stated refusal has been told nothing, and
            # "nothing" reads as approval at every call site in this repo.
            raise PatchError(
                f"{self.hook_id}: a patch with nothing to write and nothing refused. A "
                "stage that returns neither a change nor a reason is reporting success it "
                "did not earn"
            )
        if self.document_after and not self.document_before:
            raise PatchError(
                f"{self.hook_id}: a patch carrying a new document but not the one it "
                "replaces cannot tell whether the manifest moved underneath it"
            )

    # ------------------------------------------------------------------ reading

    @property
    def refused(self) -> bool:
        return bool(self.refusals)

    @property
    def writable(self) -> bool:
        """Is there anything here :func:`apply` could write, refusals aside?"""
        return bool(self.document_after)

    @property
    def expected(self) -> dict[str, str]:
        return dict(self.expected_hosts)

    def expected_host(self, version: str) -> str:
        return self.expected.get(version, "")

    @property
    def value_rule_note(self) -> str:
        """What the forbidden-value rules were actually applied to, in words.

        Exists so a `by_anchor` patch cannot pass the scrub silently. The rules
        genuinely have nothing to check there, and the honest way to report that
        is to say which kind it is and why — not to print nothing and let a reader
        infer that a check ran and passed.
        """
        if self.checked_values:
            fields = ", ".join(dict.fromkeys(self.checked_values))
            return f"forbidden-value rules applied to: {fields}"
        kind = str((self.proposed or {}).get("kind", "")) or "no proposed fingerprint"
        reason = NO_VALUE_REASON.get(kind)
        if reason is None:
            return (
                f"forbidden-value rules applied to NOTHING, and {kind!r} has no stated "
                "reason for carrying no value. Treat that as unchecked"
            )
        return f"forbidden-value rules: none apply to a {kind} fingerprint — {reason}"

    @property
    def before_entry(self) -> str:
        return serialise(self.current).rstrip("\n") if self.current is not None else ""

    @property
    def after_entry(self) -> str:
        return serialise(self.proposed).rstrip("\n") if self.proposed is not None else ""

    def diff(self) -> list[str]:
        """The change as a unified diff over the whole file. Pure, so the CLI is testable.

        Over the whole file rather than the entry alone because the claim being
        reviewed is "only this entry changes", and a diff of the entry alone
        cannot show that.
        """
        if not self.document_after:
            return []
        return list(
            difflib.unified_diff(
                self.document_before.splitlines(keepends=True),
                self.document_after.splitlines(keepends=True),
                fromfile=f"a/{self.manifest_path}",
                tofile=f"b/{self.manifest_path}",
                n=3,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "hook_id": self.hook_id,
            "manifest_path": str(self.manifest_path),
            "current": dict(self.current) if self.current is not None else None,
            "proposed": dict(self.proposed) if self.proposed is not None else None,
            "versions": list(self.versions),
            "checked_values": list(self.checked_values),
            "value_rule_note": self.value_rule_note,
            "refused": self.refused,
            "refusals": [item.to_dict() for item in self.refusals],
            # Deliberately the entry and not the whole document: a JSON report of
            # a patch is read by a human, and 28 KB of unchanged manifest around
            # four changed lines is how a review stops happening.
            "diff": self.diff(),
        }


# ------------------------------------------------------------------ planning it


def _locate(data: Mapping[str, Any], hook_id: str) -> tuple[int, int] | None:
    """``(hook index, host index)`` for the hook's single host fingerprint."""
    for position, entry in enumerate(data.get("hooks", ())):
        if entry.get("hook_id") == hook_id:
            hosts = entry.get("hosts") or []
            return position, len(hosts)
    return None


def plan(proposal: Proposal, manifest_path: Path | str = DEFAULT_MANIFEST_PATH) -> Patch:
    """Turn one proposal into a concrete manifest edit, or into stated refusals.

    Writes nothing and reads the manifest once. Every refusal below is keyed on a
    measurement the proposal or the manifest already carries — never on a
    caller's assurance, because the caller is what is being checked.
    """
    if not isinstance(proposal, Proposal):
        raise PatchError(
            f"plan() takes a generalise.Proposal, got {type(proposal).__name__}. A dict of "
            "one has already lost the Selection objects, which are the only record of "
            "which versions the fingerprint was actually measured against"
        )
    manifest_path = Path(manifest_path)
    raw = manifest_path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise PatchError(f"{manifest_path} is not readable JSON: {error}") from error
    if not isinstance(data, dict) or "hooks" not in data:
        raise PatchError(
            f"{manifest_path} does not look like a hook manifest (no 'hooks' key)"
        )
    # NOT validated through `load_manifest`, and that is deliberate — examined
    # 2026-08-04 because a loose-end note called it a gap. Loading would turn an
    # unrankable fingerprint kind into a `PatchError` where it is currently a
    # `Refusal` inside the returned `Patch`: the operator would get an exception
    # instead of a rendered reason, `writable` would never be consulted, and
    # `test_a_host_kind_this_stage_cannot_rank_is_refused_rather_than_guessed`
    # would be asserting a shape the module no longer produces. The kind IS
    # refused either way; refusing it as data is the better of the two.

    refusals: list[Refusal] = []
    checked: list[str] = []
    versions = tuple(dict.fromkeys(item.version for item in proposal.selections))
    expected = tuple(
        (item.version, item.expected)
        for item in proposal.selections
        if item.expected
    )

    def refused(
        current: Mapping[str, Any] | None = None, proposed: Mapping[str, Any] | None = None
    ) -> Patch:
        # `proposed` is carried on a refused patch only where a reader needs to
        # see WHICH fingerprint was refused — never where the refusal is that the
        # value itself is forbidden, since then there must be nothing to write it
        # from.
        return Patch(
            hook_id=proposal.hook_id,
            manifest_path=manifest_path,
            current=current,
            proposed=proposed,
            versions=versions,
            expected_hosts=expected,
            checked_values=tuple(checked),
            refusals=tuple(refusals),
        )

    # ---- refusals that make the edit unrepresentable, so nothing is materialised

    if not proposal.found:
        refusals.append(
            Refusal(
                REFUSE_NO_FINGERPRINT,
                f"{proposal.hook_id}: the proposal found no durable fingerprint, so there "
                f"is nothing to commit and the hook stays as it is. {proposal.reason}",
            )
        )
        return refused()

    located = _locate(data, proposal.hook_id)
    if located is None:
        refusals.append(
            Refusal(
                REFUSE_UNKNOWN_HOOK,
                f"{manifest_path} has no hook {proposal.hook_id!r}. A proposal for a hook "
                "this manifest does not carry is a proposal against a different manifest, "
                "and adding the hook is not an edit this stage may invent",
            )
        )
        return refused()

    hook_position, host_count = located
    if host_count != 1:
        refusals.append(
            Refusal(
                REFUSE_SEVERAL_HOSTS,
                f"{proposal.hook_id} declares {host_count} host fingerprint(s). This stage "
                "replaces exactly one, and with several there is no rule saying which loses "
                "— picking by order is how the wrong source of truth survives a patch",
            )
        )
        return refused()

    current = dict(data["hooks"][hook_position]["hosts"][0])
    entry = proposal.host_entry()

    # Which fields carry a value is decided by the kind, from a table, and a kind
    # the table does not know is refused. Reading the fields off the entry instead
    # would give any future kind a silent exemption from the scrub rules.
    kind = str(entry.get("kind", ""))
    if kind not in VALUE_FIELDS:
        refusals.append(
            Refusal(
                REFUSE_NO_VALUE_RULE,
                f"{proposal.hook_id}: no rule says which fields of a {kind!r} fingerprint "
                "carry a value that has to be scrubbed. An unlisted kind is refused rather "
                "than treated as carrying none, because 'nothing was checked' and 'nothing "
                "was wrong' are the same silence",
            )
        )
        return refused(current)

    bad_values: list[tuple[str, str]] = []
    for field in VALUE_FIELDS[kind]:
        value = entry.get(field)
        if value is None:
            continue
        for item in value if isinstance(value, list) else [value]:
            checked.append(field)
            refusal = forbidden_in_value(item)
            if refusal:
                bad_values.append((field, refusal))
    note_refusal = forbidden_in_note(entry.get("note", ""))
    if bad_values or note_refusal:
        for field, refusal in bad_values:
            refusals.append(
                Refusal(
                    REFUSE_FORBIDDEN_VALUE, f"{proposal.hook_id}: proposed {field} — {refusal}"
                )
            )
        if note_refusal:
            refusals.append(
                Refusal(
                    REFUSE_FORBIDDEN_VALUE, f"{proposal.hook_id}: {note_refusal}"
                )
            )
        # Deliberately no document and no proposed entry. A forbidden value must
        # not exist anywhere a caller who ignored the refusals could write it from.
        return refused(current)

    # ---- refusals that still leave a reviewable diff

    if len(versions) < MIN_CORROBORATING_VERSIONS:
        refusals.append(
            Refusal(
                REFUSE_ONE_VERSION,
                f"{proposal.hook_id}: measured on {list(versions) or 'no version at all'}. "
                f"A fingerprint needs {MIN_CORROBORATING_VERSIONS} versions before it is "
                "worth committing: the 439 systrace literal selected exactly one class on "
                "439 and it was the right one, exactly one on 430 and it was the WRONG one, "
                "and exactly one on 440 and it was right again. Every single-version check "
                "passes that literal",
            )
        )

    for item in proposal.selections:
        if not item.exact:
            refusals.append(
                Refusal(
                    REFUSE_SELECTION_NOT_EXACT,
                    f"{proposal.hook_id}: {item.reason}",
                )
            )

    if proposal.blocks:
        for where, why in proposal.blocks:
            refusals.append(
                Refusal(
                    REFUSE_BLOCKED,
                    f"{proposal.hook_id}: blocked by {where} — {why}. Committing a "
                    "fingerprint that cannot resolve moves the diff and not the agent "
                    "count, which is the failure stage 10 exists to measure",
                )
            )

    try:
        now, then = strength_of(current), strength_of(entry)
    except PatchError as error:
        # An unrankable kind is a fact about the manifest and the proposal, not a
        # caller contract violation, so it comes back as a refusal.
        refusals.append(Refusal(REFUSE_NOT_STRONGER, f"{proposal.hook_id}: {error}"))
        return refused(current, entry)
    if now >= then:
        refusals.append(
            Refusal(
                REFUSE_NOT_STRONGER,
                f"{proposal.hook_id}: the manifest already fingerprints this host as "
                f"{strength_key(current)!r} (rank {now}) and the proposal offers "
                f"{strength_key(entry)!r} (rank {then}). A patch must move a hook strictly "
                "UP the strength ladder; this one would churn a working fingerprint for a "
                "weaker or equal one and buy back no agent invocation",
            )
        )

    if serialise(data) != raw:
        refusals.append(
            Refusal(
                REFUSE_REFORMATS,
                f"{manifest_path} is not written in the form this stage writes "
                "(json.dumps indent=2, ensure_ascii=False, trailing newline), so applying "
                "a patch would reformat every line of it. A one-entry change buried in a "
                "whole-file reformat is a change nobody reviews",
            )
        )
        return refused(current, entry)

    patched = json.loads(raw)
    patched["hooks"][hook_position]["hosts"][0] = entry
    return Patch(
        hook_id=proposal.hook_id,
        manifest_path=manifest_path,
        current=current,
        proposed=entry,
        versions=versions,
        expected_hosts=expected,
        checked_values=tuple(checked),
        document_before=raw,
        document_after=serialise(patched),
        refusals=tuple(refusals),
    )


# ----------------------------------------------------------------- verifying it

VERIFY_OK = "same_host"
VERIFY_CHANGED = "different_host"
VERIFY_UNRESOLVED = "would_not_resolve"
VERIFY_NO_BASELINE = "no_baseline"
VERIFY_UNCHECKED = "unchecked"

#: The statuses that are a finding *against* the patch, as opposed to an absence.
#: Both block an apply; they are kept apart because "it resolved somewhere else"
#: and "I never looked" need different next moves from a human.
VERIFY_FAILURES = frozenset({VERIFY_CHANGED, VERIFY_UNRESOLVED})


@dataclass(frozen=True)
class DecodeUnderTest:
    """One version's decode and the index built from it.

    The index is required rather than derived. `resolve.resolve_hook` needs one,
    `HookIndex.for_decode` refuses an index built from a different decode, and
    guessing a path next to the decode would eventually bind a 439 index to a 440
    tree — which returns a wrong answer rather than a miss, because obfuscated
    descriptors are recycled.
    """

    version: str
    decode: Path
    index: Path

    def __post_init__(self) -> None:
        require_version(self.version)
        object.__setattr__(self, "decode", Path(self.decode))
        object.__setattr__(self, "index", Path(self.index))


@dataclass(frozen=True)
class VerifyResult:
    """What one version says about one patch: the host before, the host after, and a verdict."""

    version: str
    hook_id: str
    status: str
    before: str = ""
    after: str = ""
    expected: str = ""
    outcome_before: str = ""
    outcome_after: str = ""
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status == VERIFY_OK

    @property
    def failed(self) -> bool:
        return self.status in VERIFY_FAILURES

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "hook_id": self.hook_id,
            "status": self.status,
            "ok": self.ok,
            "before": self.before,
            "after": self.after,
            "expected": self.expected,
            "outcome_before": self.outcome_before,
            "outcome_after": self.outcome_after,
            "reason": self.reason,
        }


def _host_in(document: str, hook_id: str, target: DecodeUnderTest) -> tuple[str, str]:
    """``(descriptor, outcome)`` for one hook, resolving *document* against one decode.

    Written to a temporary file rather than resolved from an in-memory Hook,
    because `load_manifest` is what refuses a malformed manifest and a verify that
    skipped it would be verifying something the pipeline would never load.
    """
    with tempfile.TemporaryDirectory(prefix="dfinsta-verify-") as scratch:
        candidate = Path(scratch) / "hooks.json"
        candidate.write_text(document, encoding="utf-8")
        hooks = {hook.hook_id: hook for hook in load_manifest(candidate)}
    hook = hooks.get(hook_id)
    if hook is None:  # pragma: no cover - plan already refused an unknown hook
        raise PatchError(f"{hook_id} is not in the manifest being verified")
    index = HookIndex.for_decode(target.index, target.decode)
    item = resolve_hook(hook, index, target.decode)
    return item.descriptor or "", item.outcome.value


def verify(
    patch: Patch, decodes: Sequence[DecodeUnderTest]
) -> list[VerifyResult]:
    """Run the real resolver, per version, and report whether the host moved.

    This is the check that can actually be wrong, and it is why the module exists
    separately from a text editor: everything :func:`plan` refuses on is a
    verdict the proposal already wrote down, and a proposal is an argument. This
    re-derives the answer from the decode.

    Reports one result per version the patch claims and one per extra decode
    supplied. A version with no decode comes back :data:`VERIFY_UNCHECKED`, which
    is not ``ok`` and which :func:`apply` refuses on — an absence must never read
    as a pass.
    """
    if not isinstance(patch, Patch):
        raise PatchError(f"verify() takes a Patch, got {type(patch).__name__}")
    if not patch.writable:
        raise PatchError(
            f"{patch.hook_id}: this patch carries no document to verify. It was refused "
            f"before anything was materialised: "
            + "; ".join(item.code for item in patch.refusals)
        )
    by_version: dict[str, DecodeUnderTest] = {}
    for target in decodes:
        if not isinstance(target, DecodeUnderTest):
            raise PatchError(
                f"verify() takes DecodeUnderTest records, got {type(target).__name__}"
            )
        if target.version in by_version:
            raise PatchError(
                f"two decodes were given for version {target.version!r}. Which one the "
                "answer came from would decide the verdict, and nothing here picks"
            )
        by_version[target.version] = target

    order = list(patch.versions) + [
        version for version in by_version if version not in patch.versions
    ]
    results: list[VerifyResult] = []
    for version in order:
        expected = patch.expected_host(version)
        target = by_version.get(version)
        if target is None:
            results.append(
                VerifyResult(
                    version=version,
                    hook_id=patch.hook_id,
                    status=VERIFY_UNCHECKED,
                    expected=expected,
                    reason=(
                        f"no decode was given for {version}, so the patched fingerprint was "
                        "never run against it. This is not a pass: the proposal measured "
                        f"{version} and a fingerprint that selects the wrong class there "
                        "poisons the next port exactly as the 439 systrace literal would "
                        "have"
                    ),
                )
            )
            continue
        try:
            before, outcome_before = _host_in(patch.document_before, patch.hook_id, target)
            after, outcome_after = _host_in(patch.document_after, patch.hook_id, target)
        except (IndexUnusable, ManifestError, OSError) as error:
            results.append(
                VerifyResult(
                    version=version,
                    hook_id=patch.hook_id,
                    status=VERIFY_UNCHECKED,
                    expected=expected,
                    reason=(
                        f"{version} could not be resolved at all: {error}. Recorded as "
                        "unchecked rather than as a pass or a failure — nothing was learned "
                        "about the patch here"
                    ),
                )
            )
            continue
        results.append(
            _judge(patch, version, expected, before, after, outcome_before, outcome_after)
        )
    return results


def _judge(
    patch: Patch,
    version: str,
    expected: str,
    before: str,
    after: str,
    outcome_before: str,
    outcome_after: str,
) -> VerifyResult:
    """The verdict for one version, given what each manifest resolved to there.

    The baseline is ``before`` when the hook resolves today and ``expected`` when
    it does not — which is the normal case for the patch that matters, because a
    `by_agent` hook resolves to nothing without an agent and "the same as nothing"
    would be satisfied by a patch that also resolves to nothing.
    """

    def result(status: str, reason: str) -> VerifyResult:
        return VerifyResult(
            version=version,
            hook_id=patch.hook_id,
            status=status,
            before=before,
            after=after,
            expected=expected,
            outcome_before=outcome_before,
            outcome_after=outcome_after,
            reason=reason,
        )

    if not after:
        return result(
            VERIFY_UNRESOLVED,
            f"{version}: with the patch the hook resolves to no host at all "
            f"({outcome_after}). The manifest resolves it to "
            f"{before or 'nothing'} today",
        )
    if before and expected and before != expected:
        return result(
            VERIFY_CHANGED,
            f"{version}: the manifest resolves this hook to {before} today and the "
            f"proposal recorded the known host as {expected}. Two answer keys that "
            "disagree cannot both be the baseline, so nothing here can say the patch "
            "preserved the host",
        )
    baseline = before or expected
    if not baseline:
        return result(
            VERIFY_NO_BASELINE,
            f"{version}: the patched fingerprint selects {after} ({outcome_after}), but "
            f"the manifest resolves this hook to nothing here ({outcome_before}) and the "
            "proposal recorded no known host for this version. There is nothing to compare "
            "against, so this is evidence of nothing rather than a pass",
        )
    if after != baseline:
        return result(
            VERIFY_CHANGED,
            f"{version}: the patched fingerprint selects {after} and the host here is "
            f"{baseline}. One class is not the same as the right class, and every "
            "single-version check passes that",
        )
    return result(
        VERIFY_OK,
        f"{version}: the patched fingerprint selects {after} ({outcome_after}), which is "
        + (
            f"the host the manifest resolves to today"
            if before
            else "the host this version is known to use"
        )
        + f". Before: {before or 'nothing — ' + outcome_before}",
    )


# ------------------------------------------------------------------ applying it


@dataclass(frozen=True)
class ApplyResult:
    """Whether the manifest was written, and if not, exactly why not."""

    hook_id: str
    manifest_path: Path
    written: bool
    refusals: tuple[Refusal, ...] = ()
    verified: tuple[VerifyResult, ...] = ()

    def __post_init__(self) -> None:
        if self.written == bool(self.refusals):
            raise PatchError(
                f"{self.hook_id}: an apply that wrote and also refused, or that did "
                "neither. A write with no refusal and a refusal with no write are the only "
                "two honest outcomes"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook_id,
            "manifest_path": str(self.manifest_path),
            "written": self.written,
            "refusals": [item.to_dict() for item in self.refusals],
            "verified": [item.to_dict() for item in self.verified],
        }


def write_manifest_atomically(path: Path, document: str) -> None:
    """Temp file in the same directory, validated, then renamed over the target.

    Public because it is the *only* sanctioned way to change `manifest/hooks.json`
    and there is now more than one caller. A sibling reaching for a private name
    is the coupling this project just spent a slice removing between
    `replay_gate` and `activities`; a second copy of the write would be worse
    still, because "validated before it becomes the result" would then be two
    behaviours that have to agree.

    Same directory so the rename is within one filesystem and therefore atomic;
    ``fsync`` before the rename so a crash cannot leave a renamed-but-empty file;
    and `load_manifest` against the temp file BEFORE the rename, so a manifest
    that does not load never reaches the path anything else reads.
    """
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    )
    scratch = Path(handle.name)
    try:
        try:
            handle.write(document)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        # The result, checked before it becomes the result.
        load_manifest(scratch)
        os.replace(scratch, path)
    except BaseException:
        scratch.unlink(missing_ok=True)
        raise


def apply(
    patch: Patch,
    manifest_path: Path | str = DEFAULT_MANIFEST_PATH,
    *,
    confirm: bool = False,
    verified: Sequence[VerifyResult] = (),
) -> ApplyResult:
    """Write the patch, but only with *confirm* and only on a fully verified patch.

    The default is a dry run in the strongest sense: with ``confirm`` falsy this
    function opens nothing for writing at all, so there is no path through it in
    which a caller who forgot the flag still touches the file.

    *verified* is what :func:`verify` returned. Every version the patch claims
    must have a passing result, and no supplied result may be a failure. A version
    with no result is refused as unverified rather than assumed fine, which is the
    whole difference between this and a hand-edit.
    """
    if not isinstance(patch, Patch):
        raise PatchError(f"apply() takes a Patch, got {type(patch).__name__}")
    manifest_path = Path(manifest_path)
    if manifest_path.resolve() != patch.manifest_path.resolve():
        raise PatchError(
            f"this patch was planned against {patch.manifest_path} and apply() was called "
            f"with {manifest_path}. The document it carries is that file's bytes; writing "
            "them anywhere else would replace a manifest nobody diffed"
        )

    refusals: list[Refusal] = []
    if patch.refused:
        refusals.append(
            Refusal(
                REFUSE_PLAN_REFUSED,
                f"{patch.hook_id}: the plan was refused and refusals are not warnings — "
                + "; ".join(f"[{item.code}] {item.reason}" for item in patch.refusals),
            )
        )
    if not patch.writable:
        refusals.append(
            Refusal(
                REFUSE_PLAN_REFUSED,
                f"{patch.hook_id}: this patch carries no document, so there is nothing to "
                "write",
            )
        )

    results = tuple(verified)
    by_version = {item.version: item for item in results}
    for version in patch.versions:
        outcome = by_version.get(version)
        if outcome is None:
            refusals.append(
                Refusal(
                    REFUSE_UNVERIFIED,
                    f"{patch.hook_id}: version {version} has no verification result. The "
                    "proposal measured it, so an apply without one would be committing a "
                    "fingerprint nobody re-derived on that version — and an unchecked "
                    "version is not a passing one",
                )
            )
        elif not outcome.ok:
            refusals.append(
                Refusal(
                    REFUSE_UNVERIFIED,
                    f"{patch.hook_id}: version {version} verified as {outcome.status!r} — "
                    f"{outcome.reason}",
                )
            )
    for outcome in results:
        if outcome.version not in patch.versions and outcome.failed:
            refusals.append(
                Refusal(
                    REFUSE_UNVERIFIED,
                    f"{patch.hook_id}: version {outcome.version} was not one the proposal "
                    f"measured, and it verified as {outcome.status!r} — {outcome.reason}. A "
                    "version outside the corroborating set still counts against a "
                    "fingerprint; the supported range is what a port has to survive",
                )
            )

    if not confirm:
        refusals.append(
            Refusal(
                REFUSE_UNCONFIRMED,
                f"{patch.hook_id}: dry run. Nothing was written. Pass confirm=True (CLI: "
                "--confirm) once the diff above has been read",
            )
        )

    if refusals:
        return ApplyResult(
            patch.hook_id, manifest_path, False, tuple(refusals), results
        )

    on_disk = manifest_path.read_text(encoding="utf-8")
    if on_disk != patch.document_before:
        return ApplyResult(
            patch.hook_id,
            manifest_path,
            False,
            (
                Refusal(
                    REFUSE_STALE,
                    f"{manifest_path} has changed since this patch was planned. The diff "
                    "that was reviewed is not the diff that would be written, so the patch "
                    "must be re-planned against the current file",
                ),
            ),
            results,
        )

    try:
        write_manifest_atomically(manifest_path, patch.document_after)
    except ManifestError as error:
        # The temp file is already gone and the manifest was never touched.
        return ApplyResult(
            patch.hook_id,
            manifest_path,
            False,
            (
                Refusal(
                    REFUSE_DOES_NOT_LOAD,
                    f"{patch.hook_id}: the patched manifest does not load — {error}. "
                    "Nothing was written; a manifest on disk that load_manifest refuses "
                    "would break every stage downstream of this one",
                ),
            ),
            results,
        )

    try:
        load_manifest(manifest_path)
    except ManifestError as error:  # pragma: no cover - the temp file already loaded
        # Belt and braces. The file that landed is byte-identical to one that
        # loaded moments ago, so reaching here means something outside this
        # process moved; put the original back rather than leave a broken manifest.
        write_manifest_atomically(manifest_path, patch.document_before)
        raise PatchError(
            f"{manifest_path} did not load after the patch was written and the original "
            f"has been restored: {error}"
        ) from error

    return ApplyResult(patch.hook_id, manifest_path, True, (), results)


# --------------------------------------------------------------------- rendering


def _rank(entry: Mapping[str, Any]) -> str:
    """The rung this entry sits on, or its kind, for display only.

    Never raises: one of the things `plan` refuses on is a host kind this stage
    cannot rank, and a renderer that died on the refused patch would turn a stated
    refusal into a traceback.
    """
    try:
        return repr(strength_key(entry))
    except PatchError:
        return f"{entry.get('kind')!r} (unrankable)"


def render_patch(patch: Patch) -> list[str]:
    """The plan as lines. Pure, so the CLI's output is testable."""
    lines = [f"{patch.hook_id}  ({patch.manifest_path})"]
    if patch.current is not None:
        lines.append(f"  current   {_rank(patch.current)}")
    if patch.proposed is not None:
        lines.append(f"  proposed  {_rank(patch.proposed)}")
        lines.append(f"  {patch.value_rule_note}")
    lines.append(
        "  measured on: " + (", ".join(patch.versions) or "no version at all")
    )
    if patch.refusals:
        lines.append("  REFUSED")
        for item in patch.refusals:
            lines.append(f"    [{item.code}] {item.reason}")
    elif patch.writable:
        lines.append("  PLANNED — nothing written; run verify, then apply --confirm")
    for line in patch.diff():
        lines.append("  " + line.rstrip("\n"))
    return lines


def render_verification(results: Sequence[VerifyResult]) -> list[str]:
    lines = ["VERIFICATION  (one line per version; unchecked is not a pass)"]
    for item in results:
        mark = " " if item.ok else "!"
        lines.append(
            f" {mark} {item.version:>6}  {item.status:<18} "
            f"{item.before or '-'} -> {item.after or '-'}"
        )
        lines.append(f"        {item.reason}")
    if not results:
        lines.append("    (nothing was verified, which is not the same as nothing failing)")
    return lines


def render_apply(result: ApplyResult) -> list[str]:
    if result.written:
        return [f"WRITTEN  {result.manifest_path}  ({result.hook_id})"]
    lines = [f"NOT WRITTEN  {result.manifest_path}  ({result.hook_id})"]
    for item in result.refusals:
        lines.append(f"  [{item.code}] {item.reason}")
    return lines


# ------------------------------------------------------- reading a proposals file


def _selection(data: Mapping[str, Any], where: str) -> Selection:
    """One `Selection` back out of a proposals document.

    Every field is required. A measurement missing its count or its expected host
    is not a measurement with a gap in it — it is a different document's schema,
    and silently defaulting the missing half would turn a hand-written record into
    a `Selection` that claims something nobody measured.
    """
    missing = [key for key in ("version", "count", "expected") if key not in data]
    if missing:
        raise PatchError(
            f"{where}: a recorded measurement is missing {missing}. This reads the "
            "documents `generalise.write_proposals` writes, where a measurement is a "
            "serialised `Selection`; a file in another schema must be converted rather "
            "than partially read"
        )
    return Selection(
        version=data["version"],
        literals=tuple(data.get("literals", ())),
        count=int(data["count"]),
        sample=tuple(data.get("sample", ())),
        expected=data["expected"],
    )


def read_proposals(path: Path | str) -> tuple[Proposal, ...]:
    """Every proposal in a `generalise.write_proposals` document, as the real type.

    Rebuilt into `generalise.Proposal` rather than read as dicts, so a hand-edited
    proposals file goes through the same constructor a measured one did — which is
    where the "measured on fewer than two versions" and "a selection that is not
    exact" refusals already live.
    """
    path = Path(path)
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or "proposals" not in document:
        raise PatchError(
            f"{path} is not a proposals document (no 'proposals' key). "
            "`generalise.write_proposals` writes the shape this reads"
        )
    out: list[Proposal] = []
    for entry in document["proposals"]:
        host = entry.get("host_entry") or {}
        # Read off the entry, never off `fingerprint_found`: a document written by
        # something other than `generalise.write_proposals` may carry the kind and
        # not the flag, and defaulting to "" there would drop a by_anchor
        # promotion on the floor as "no fingerprint found" instead of saying that
        # this stage cannot express it.
        declared = host.get("kind", "")
        if declared not in KINDS:
            raise PatchError(
                f"{path}: {entry.get('hook_id')!r} proposes a {declared!r} fingerprint, "
                f"which `generalise.Proposal` does not model (it models "
                f"{sorted(item for item in KINDS if item)} or nothing)"
            )
        kind = declared if entry.get("fingerprint_found") else ""
        hook_id = entry["hook_id"]
        out.append(
            Proposal(
                hook_id=hook_id,
                kind=kind,
                literal=host.get("literal", "") or "",
                co_literals=tuple(host.get("co_literals", ())),
                selections=tuple(
                    _selection(item, f"{path}: {hook_id} measured")
                    for item in entry.get("measured", ())
                ),
                reason=entry.get("reason", ""),
                rejected=tuple(
                    Rejection(
                        literals=tuple(item.get("literals", ())),
                        reason=item.get("reason", ""),
                        selections=tuple(
                            _selection(sub, f"{path}: {hook_id} rejected")
                            for sub in item.get("selections", ())
                        ),
                    )
                    for item in entry.get("rejected", ())
                ),
                blocks=tuple(
                    (item["blocked_by"], item["detail"]) for item in entry.get("blocks", ())
                ),
                note=host.get("note", ""),
            )
        )
    return tuple(out)


# ------------------------------------------------------------------------- cli


def _decode_argument(text: str) -> DecodeUnderTest:
    parts = text.split(":")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(
            f"--decode takes VERSION:DECODE:INDEX, got {text!r}"
        )
    version, decode, index = parts
    return DecodeUnderTest(version, Path(decode), Path(index))


def _select(proposals: Sequence[Proposal], hook_id: str | None) -> list[Proposal]:
    if hook_id is None:
        return list(proposals)
    chosen = [item for item in proposals if item.hook_id == hook_id]
    if not chosen:
        raise PatchError(
            f"no proposal for {hook_id!r} in this file; it holds "
            f"{[item.hook_id for item in proposals]}"
        )
    return chosen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def common(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--proposals", type=Path, required=True, help="a generalise proposals JSON"
        )
        command.add_argument(
            "--manifest",
            type=Path,
            default=DEFAULT_MANIFEST_PATH,
            help=f"hook manifest (default {DEFAULT_MANIFEST_PATH})",
        )
        command.add_argument("--hook", help="plan only this hook; default every proposal")
        command.add_argument("--json", action="store_true", help="print the result as JSON")

    plan_cmd = sub.add_parser("plan", help="what would change, and what refuses it")
    common(plan_cmd)

    verify_cmd = sub.add_parser(
        "verify", help="run the resolver per version and report whether the host moved"
    )
    common(verify_cmd)
    verify_cmd.add_argument(
        "--decode",
        type=_decode_argument,
        action="append",
        default=[],
        metavar="VERSION:DECODE:INDEX",
        help="a decode to verify against; repeatable, one per version",
    )

    apply_cmd = sub.add_parser("apply", help="write the manifest — needs --confirm")
    common(apply_cmd)
    apply_cmd.add_argument(
        "--decode",
        type=_decode_argument,
        action="append",
        default=[],
        metavar="VERSION:DECODE:INDEX",
        help="a decode to verify against before writing; repeatable",
    )
    apply_cmd.add_argument(
        "--confirm",
        action="store_true",
        help="actually write. Without it this is a dry run and nothing is opened for writing",
    )

    args = parser.parse_args(argv)

    try:
        proposals = _select(read_proposals(args.proposals), args.hook)
    except PatchError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    reports: list[dict[str, Any]] = []
    lines: list[str] = []
    status = 0
    for proposal in proposals:
        try:
            patch = plan(proposal, args.manifest)
        except (PatchError, OSError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        entry: dict[str, Any] = {"plan": patch.to_dict()}
        lines.extend(render_patch(patch))

        results: list[VerifyResult] = []
        if args.command in {"verify", "apply"} and patch.writable:
            results = verify(patch, args.decode)
            entry["verification"] = [item.to_dict() for item in results]
            lines.extend(render_verification(results))

        if args.command == "apply":
            outcome = apply(
                patch,
                args.manifest,
                confirm=args.confirm,
                verified=results,
            )
            entry["apply"] = outcome.to_dict()
            lines.extend(render_apply(outcome))
            if not outcome.written:
                status = 1
        elif patch.refused or any(not item.ok for item in results):
            status = 1
        lines.append("")
        reports.append(entry)

    if args.json:
        print(json.dumps(reports, indent=2, ensure_ascii=False))
    else:
        for line in lines:
            print(line)
    return status


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

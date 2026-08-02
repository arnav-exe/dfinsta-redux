"""Stage 5a-5c: turn agent proposals into checked operations, or into escalations.

Two of the seven hooks have no mechanical fingerprint. Nothing in the decode
names the profile action-bar delegates, no string literal sits inside them, and
Instagram ships *two* implementations selected at runtime by MobileConfig
`0x81099a000034a6` — so the same APK can need either one. Those are the hooks an
agent has to find.

A blind holdout established that this is feasible rather than hopeful: from an
isolated stock decode and behaviour-level intent alone, two of three
provably-uncontaminated proposers independently reached the hard site,
`LX/06X7;->AP1`, including the runtime selector between the two variants.

It also showed the failure to design against. One proposer justified its answer
with a fabricated claim — that a register was "usually null" when it held a live
listener. The anchor happened to be right anyway. That is the shape of the risk
here: not a proposer that hedges, but one that is confidently wrong and fluent
about it. So nothing in this module believes a proposal. Every proposal is:

    1. re-derived against the decode by a deterministic validator
    2. compared with the other proposers' answers, by content
    3. handed to a verifier that never sees the rationale, only the claim

and each of those emits an :mod:`~dfinsta_pipeline.evidence` claim from a
producer that is not the proposer. A proposal that survives all three is an
operation. Anything else is an escalation with the disagreement attached.

:class:`HostProposal` answers a deliberately narrower question — which class,
and nothing else — because that is the only fact that genuinely varies between
versions. Its docstring records the measurement that prompted it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence, TypeVar

from .contracts import canonical_sha256
from .evidence import (
    NO_PROPOSER,
    EvidenceClaim,
    EvidenceKind,
    EvidenceLedger,
    Producer,
    Subject,
    Verdict,
    agreement_claim,
)
from .hook_manifest import Hook, ManifestError

#: A validator takes (decode, operations) and returns one result dict per
#: operation. `tools/resolver/validate_candidates.validate` has this shape; it is
#: injected rather than imported so this module stays testable without a decode
#: and without reaching across the repo into `tools/`.
Validator = Callable[[Path, list[dict[str, Any]]], list[dict[str, Any]]]


class ProposalError(ValueError):
    """Raised when a proposal is malformed enough that no check could be run."""


@dataclass(frozen=True)
class Proposal:
    """One agent's answer for one hook: where the hook goes and what it looks like.

    ``rationale`` is deliberately *not* part of :attr:`fingerprint`. Two proposers
    agree when they arrived at the same site and the same instructions, not when
    they told the same story about it. Prose agreement between language models is
    close to worthless as corroboration; identical smali is not.
    """

    hook_id: str
    proposer: str
    descriptor: str
    anchor: tuple[str, ...]
    payload: tuple[str, ...]
    rationale: str = ""
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.hook_id.strip():
            raise ProposalError("proposal needs a hook_id")
        if not self.proposer.strip():
            raise ProposalError(
                f"{self.hook_id}: proposal needs a proposer id, or its evidence cannot "
                "be checked for independence"
            )
        if not self.descriptor.strip():
            raise ProposalError(f"{self.hook_id}: proposal needs a host descriptor")
        if not self.anchor:
            raise ProposalError(f"{self.hook_id}: proposal needs an anchor")
        if not self.payload:
            raise ProposalError(f"{self.hook_id}: proposal needs a payload")
        for line in self.anchor:
            if not line.strip():
                # `"" == "".strip()`, so an empty line slips past the whitespace
                # check below while matching nothing — the very failure that check
                # exists to name.
                raise ProposalError(
                    f"{self.hook_id}: anchor contains an empty line, which matches "
                    "nothing; the applier compares against significant lines only"
                )
            if line != line.strip():
                # Three 439 Reels anchors were submitted with leading whitespace and
                # matched zero lines, because the applier compares against a stripped
                # line. Rejecting it here names the mistake instead of reporting
                # "anchor not found" from four stages away.
                raise ProposalError(
                    f"{self.hook_id}: anchor line {line!r} carries surrounding "
                    "whitespace; the applier matches stripped lines, so this would "
                    "silently match nothing"
                )

    @property
    def fingerprint(self) -> str:
        """Content identity used for agreement — never prose.

        Host, anchor *and* payload: two proposers who pick the same site but
        inject different instructions have not agreed about anything that
        matters, and without the payload here which one ships would come down to
        list order.
        """
        return canonical_sha256(
            {
                "descriptor": self.descriptor,
                "anchor": list(self.anchor),
                "payload": list(self.payload),
            }
        )

    def normalised_payload(self) -> tuple[str, ...]:
        """Payload with blank lines dropped and each line stripped.

        Blank-line placement is formatting, and two proposers who wrote the same
        instructions with different spacing have not disagreed about anything.
        """
        return tuple(line.strip() for line in self.payload if line.strip())

    def effect_key(self, hook: Hook) -> str:
        """What this proposal would actually DO, for comparing against another.

        Agreement has to be on the effect, not on the anchor text. Two proposers
        can identify the same insertion point with anchors of different lengths —
        one used the last three lines of a block, another the last two — and
        under a raw-text comparison they read as two different answers when they
        are the same patch. That happened on the first real run.

        For ``insert_after`` the anchor's job is done once it locates a point, so
        only its LAST line matters. For ``replace`` the anchor is the thing being
        replaced, so all of it counts.
        """
        locator = list(self.anchor) if hook.mode == "replace" else [self.anchor[-1]]
        return canonical_sha256(
            {
                "descriptor": self.descriptor,
                "mode": hook.mode,
                "locator": locator,
                "payload": list(self.normalised_payload()),
            }
        )

    def marker_count(self, hook: Hook) -> int:
        return "\n".join(self.payload).count(hook.marker)

    @property
    def key(self) -> str:
        """Identifies this exact proposal, not merely its author.

        Validation results are stored under this. Keying them by proposer alone
        loses a row whenever one proposer offers two answers, and the survivor
        then inherits the other's verdict — a proposal the validator refused
        could be accepted and reported as having passed.
        """
        return f"{self.proposer}#{self.fingerprint[:12]}"

    def as_operation(self, hook: Hook) -> dict[str, Any]:
        """The applier-shaped operation this proposal claims."""
        return {
            "id": self.hook_id,
            "descriptor": self.descriptor,
            "mode": hook.mode,
            "anchor": list(self.anchor),
            "expected_anchor_count": hook.expected_anchor_count,
            "marker": hook.marker,
            "expected_marker_count": hook.expected_marker_count,
            "payload": list(self.payload),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook_id,
            "proposer": self.proposer,
            "descriptor": self.descriptor,
            "anchor": list(self.anchor),
            "payload": list(self.payload),
            "rationale": self.rationale,
            "evidence": list(self.evidence),
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Proposal:
        return cls(
            hook_id=data["hook_id"],
            proposer=data["proposer"],
            descriptor=data["descriptor"],
            anchor=tuple(data["anchor"]),
            payload=tuple(data["payload"]),
            rationale=data.get("rationale", ""),
            evidence=tuple(data.get("evidence", ())),
        )


@dataclass(frozen=True)
class Refutation:
    """A verifier's attempt to break one proposal.

    The verifier is told to *refute*, and ``refuted=True`` is the safe default
    when it cannot decide. A verifier asked "is this right?" agrees with almost
    anything plausible; one asked "show me why this is wrong" that comes back
    empty-handed is worth something.
    """

    hook_id: str
    verifier: str
    refuted: bool
    finding: str
    checked: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.verifier.strip():
            raise ProposalError(f"{self.hook_id}: refutation needs a verifier id")
        if not self.finding.strip():
            raise ProposalError(
                f"{self.hook_id}: refutation needs a finding, including when nothing "
                "was found — 'looked and found nothing' and 'did not look' must not "
                "read the same"
            )


#: A host is a CLASS, so ``L...;`` and nothing else. Not an array (``[LX/0aaa;``),
#: not a primitive (``I``), and not method-qualified (``LX/0aaa;->AP1``) — which is
#: the form an agent naturally writes when it has found the *site* and is naming
#: it. :func:`dfinsta_pipeline.resolve.search_hosts` looks a proposed descriptor up
#: in the index verbatim, so any of those finds nothing and is reported as "does
#: not exist in this version" — a version-drift diagnosis for a typing mistake.
#: ``-`` is permitted because d8 emits ``-$$Lambda$...`` classes and one could
#: legitimately be a host.
CLASS_DESCRIPTOR = re.compile(r"^L[A-Za-z0-9_$-]+(?:/[A-Za-z0-9_$-]+)*;$")


@dataclass(frozen=True)
class HostProposal:
    """One agent's answer to the only fact that genuinely varies: WHICH class.

    A :class:`Proposal` asks an agent to invent an entire patch. The manifest
    already owns the shape of one — an anchor pattern with typed captures and a
    payload template — and six of the seven hooks resolve mechanically from that
    shape once the host is known; ``resolve.Outcome.NEEDS_AGENT`` is returned
    precisely when the missing fact is the host and nothing else.

    Asking for the whole patch therefore manufactures variance, and variance is
    what kills k-of-n agreement. Measured on the first full k-proposer run against
    439: **2 of 3 proposers reached the correct host**, and **1 of 3 agreed by
    effect** — one wrote a 2-line anchor with a 16-line payload, another a 4-line
    anchor with a 2-line payload, and :meth:`Proposal.effect_key` separated them,
    so ``assess`` refused. Correctly: it was asked to compare patches. It was the
    question that was wrong.

    So this carries a host and the evidence for it, and nothing else. Agreement is
    over :attr:`descriptor` alone — see :func:`host_agreement`.
    """

    hook_id: str
    proposer: str
    descriptor: str
    smali_path: str = ""
    evidence: tuple[str, ...] = ()
    alternatives: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.hook_id.strip():
            raise ProposalError("host proposal needs a hook_id")
        if not self.proposer.strip():
            raise ProposalError(
                f"{self.hook_id}: host proposal needs a proposer id, or its evidence "
                "cannot be checked for independence"
            )
        if not self.descriptor.strip():
            raise ProposalError(f"{self.hook_id}: host proposal needs a host descriptor")
        if not CLASS_DESCRIPTOR.match(self.descriptor):
            raise ProposalError(
                f"{self.hook_id}: {self.descriptor!r} is not a smali class descriptor. "
                "It must be exactly `Lpackage/Name;` as the decode writes it — a method "
                "suffix, a missing semicolon or an array prefix resolves to no class at "
                "all, and the Resolve stage reports that as the class not existing in "
                "this version rather than as a malformed answer."
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook_id,
            "proposer": self.proposer,
            "descriptor": self.descriptor,
            "smali_path": self.smali_path,
            "evidence": list(self.evidence),
            "alternatives": list(self.alternatives),
            "unresolved": list(self.unresolved),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> HostProposal:
        return cls(
            hook_id=data["hook_id"],
            proposer=data["proposer"],
            descriptor=data["descriptor"],
            smali_path=data.get("smali_path", ""),
            evidence=tuple(data.get("evidence", ())),
            alternatives=tuple(data.get("alternatives", ())),
            unresolved=tuple(data.get("unresolved", ())),
        )


#: The two shapes an agent's answer can take. Constrained rather than bound to a
#: protocol, because there are exactly two and naming them keeps
#: :func:`one_per_proposer` honest about what it accepts.
_Voice = TypeVar("_Voice", Proposal, HostProposal)


@dataclass
class Assessment:
    """Everything known about one hook's proposals, and whether one may be used."""

    hook_id: str
    accepted: Proposal | None
    claims: tuple[EvidenceClaim, ...]
    proposals: tuple[Proposal, ...]
    validations: Mapping[str, Any] = field(default_factory=dict)
    reason: str = ""

    @property
    def resolved(self) -> bool:
        return self.accepted is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook_id,
            "resolved": self.resolved,
            "reason": self.reason,
            "accepted": self.accepted.to_dict() if self.accepted else None,
            "proposals": [item.to_dict() for item in self.proposals],
            "claims": [claim.to_dict() for claim in self.claims],
            "validations": dict(self.validations),
        }


def group_by_fingerprint(
    proposals: Sequence[Proposal], hook: Hook | None = None
) -> dict[str, list[Proposal]]:
    """Bucket proposals by what they would do.

    With *hook*, buckets on :meth:`Proposal.effect_key` — the same insertion point
    and the same instructions, regardless of how much anchor context each
    proposer chose to quote. Without it, falls back to raw content identity.
    """
    groups: dict[str, list[Proposal]] = {}
    for proposal in proposals:
        key = proposal.effect_key(hook) if hook is not None else proposal.fingerprint
        groups.setdefault(key, []).append(proposal)
    return groups


def independent_proposers(proposals: Sequence[Proposal]) -> bool:
    """Did these answers come from distinct proposers?

    k identical answers from one proposer is one answer, not agreement. Counting
    it as k would let a single confidently-wrong agent manufacture consensus by
    being run repeatedly.
    """
    return len({proposal.proposer for proposal in proposals}) == len(proposals)


def validate_proposals(
    hook: Hook,
    proposals: Sequence[Proposal],
    decode: Path,
    validator: Validator,
) -> dict[str, dict[str, Any]]:
    """Re-derive each proposal's checkable claims from the decode itself.

    Keyed by :attr:`Proposal.key`, which identifies the proposal rather than its
    author — see that property for why keying by proposer is unsafe.
    """
    results: dict[str, dict[str, Any]] = {}
    for proposal in proposals:
        operation = proposal.as_operation(hook)
        try:
            reported = validator(Path(decode), [operation])
        except Exception as error:  # noqa: BLE001 - a validator crash is a failed check
            results[proposal.key] = {
                "proposer": proposal.proposer,
                "verdict": "BROKEN",
                "reason": f"validator raised {type(error).__name__}: {error}",
            }
            continue
        row = dict(reported[0]) if reported else {}
        if not row:
            row = {"verdict": "BROKEN", "reason": "validator returned no result"}
        row.setdefault("proposer", proposal.proposer)
        results[proposal.key] = row
    return results


def one_per_proposer(proposals: Sequence[_Voice]) -> list[_Voice]:
    """First answer from each proposer, so one voice cannot count as several.

    Accepts either kind of answer. It reads nothing but ``proposer``, and the rule
    it enforces — k answers from one agent is one answer — is identical whether
    the agent proposed a whole patch or only a host. Sharing it means a mutation
    that lets one proposer count twice breaks both paths at once, rather than one
    path quietly keeping a guard the other lost.
    """
    seen: set[str] = set()
    out: list[_Voice] = []
    for proposal in proposals:
        if proposal.proposer not in seen:
            seen.add(proposal.proposer)
            out.append(proposal)
    return out


@dataclass(frozen=True)
class HostAgreement:
    """Which host, if any, independent proposers converged on.

    ``agreed_descriptor`` is ``None`` unless the rule passed, and it is the only
    field naming a descriptor. That is deliberate: a caller reading the plurality
    answer has to go through :attr:`group`, where the count is visible, rather
    than through a field whose name reads like a decision.
    """

    agreed_descriptor: str | None
    group: tuple[HostProposal, ...]
    votes: tuple[HostProposal, ...]
    distinct_answers: int
    reason: str = ""

    @property
    def agreed(self) -> bool:
        return self.agreed_descriptor is not None


def host_agreement(
    proposals: Sequence[HostProposal], threshold: float = 0.5
) -> HostAgreement:
    """The host at least two DISTINCT proposers reached, or why there is none.

    The same rule :func:`assess` applies, over the descriptor alone: collapse to
    one vote per proposer, take the plurality, and require both that it clears
    *threshold* and that **at least two distinct proposers** are in it. The second
    condition is not implied by the first — one proposer answering while two
    abstain is a share of 1.0 — and it is the whole point, because a single
    confidently-wrong agent is the failure this project has actually shipped.

    Nothing here breaks a tie by ranking. Two proposers naming two classes is a
    finding for a human, not an input to a ranking function.

    Not wired to :func:`~dfinsta_pipeline.evidence.agreement_claim`, and it cannot
    be as it stands: that function counts a proposal as having answered only when
    it names a descriptor *and* a non-empty anchor, so a set of host proposals
    tallies as zero answered and returns ``not_exercised``. Whoever files this as
    ledger evidence has to widen that check first — and had better notice, because
    the failure is silent in the safe direction and would simply stall every
    by-agent hook.
    """
    votes = one_per_proposer(proposals)
    groups: dict[str, list[HostProposal]] = {}
    for proposal in votes:
        groups.setdefault(proposal.descriptor, []).append(proposal)
    if not groups:
        return HostAgreement(None, (), (), 0, "no host proposals were produced")

    descriptor, group = max(groups.items(), key=lambda item: (len(item[1]), item[0]))
    share = len(group) / len(votes)
    if len(group) < 2:
        if len(votes) < 2:
            reason = (
                "only one proposer answered, so there is nothing to corroborate it. "
                "Agreement across independent proposers is a required item of "
                "evidence, and a single answer cannot supply it however good it looks."
            )
        else:
            reason = (
                f"{len(group)} of {len(votes)} distinct proposers reached the most "
                f"common host ({len(groups)} distinct hosts overall). That is genuine "
                "ambiguity, and it belongs at a gate rather than being broken by ranking."
            )
        return HostAgreement(None, tuple(group), tuple(votes), len(groups), reason)
    if share < threshold:
        return HostAgreement(
            None,
            tuple(group),
            tuple(votes),
            len(groups),
            f"{len(group)} of {len(votes)} distinct proposers reached {descriptor} "
            f"({share:.0%}), below the {threshold:.0%} agreement threshold; "
            f"{len(groups)} distinct hosts were proposed",
        )
    return HostAgreement(
        descriptor,
        tuple(group),
        tuple(votes),
        len(groups),
        f"{len(group)} of {len(votes)} distinct proposers independently reached "
        f"{descriptor}",
    )


#: Everything that has to hold for the anchor to be a usable, unique, unpatched
#: site. `anchor_whitespace_clean` is in here deliberately: the validator's own
#: `ok` does not consider it, because a whitespace-dirty anchor already shows up
#: as `anchor_matches=False`. Naming it separately makes the *reason* visible at
#: the gate instead of "matched 0 times".
_ANCHOR_CHECKS = (
    "descriptor_resolves",
    "anchor_whitespace_clean",
    "anchor_matches",
    "anchor_unique",
)


def _validator_claims(
    hook_id: str, proposer: str, row: Mapping[str, Any]
) -> list[EvidenceClaim]:
    """Map one `validate_candidates` result row onto deterministic evidence.

    The mapping is deliberately stricter than the validator's own ``ok``:
    ``registers_safe is None`` means the liveness check was never evaluated, and
    that is recorded as ``inconclusive`` rather than folded into a pass. A check
    that did not run must never read as a check that succeeded.
    """
    actor = "tools/resolver/validate_candidates.py"
    claims: list[EvidenceClaim] = []
    detail = {"proposer": proposer, "row": {k: v for k, v in row.items() if k != "id"}}

    failures = [name for name in _ANCHOR_CHECKS if row.get(name) is False]
    if row.get("marker_absent") is False:
        failures.append("marker_absent")
    if failures:
        anchor_verdict = Verdict.FAILED
        summary = (
            f"{proposer}'s proposal failed {', '.join(failures)}"
            + (f" — {row['reason']}" if row.get("reason") else "")
        )
    elif any(name not in row for name in _ANCHOR_CHECKS):
        anchor_verdict = Verdict.INCONCLUSIVE
        missing = [name for name in _ANCHOR_CHECKS if name not in row]
        summary = f"{proposer}'s proposal: validator did not report {', '.join(missing)}"
    else:
        anchor_verdict = Verdict.PASSED
        summary = (
            f"{proposer}'s proposal resolves to {row.get('smali_path')} and its anchor "
            f"matches exactly {row.get('anchor_occurrences')} time(s) with no marker present"
        )
    claims.append(
        EvidenceClaim(
            hook_id=hook_id,
            kind=EvidenceKind.ANCHOR_UNIQUE,
            verdict=anchor_verdict,
            producer=Producer.DETERMINISTIC,
            actor=actor,
            summary=summary,
            detail=detail,
        )
    )

    safe = row.get("registers_safe")
    note = str(row.get("registers_note", ""))
    if safe is True:
        register_verdict, register_summary = Verdict.PASSED, f"{proposer}: {note}"
    elif safe is False:
        register_verdict, register_summary = Verdict.FAILED, f"{proposer}: {note}"
    else:
        register_verdict = Verdict.INCONCLUSIVE
        register_summary = (
            f"{proposer}: register liveness was not evaluated"
            + (f" ({note})" if note else "")
        )
    claims.append(
        EvidenceClaim(
            hook_id=hook_id,
            kind=EvidenceKind.REGISTERS_SAFE,
            verdict=register_verdict,
            producer=Producer.DETERMINISTIC,
            actor=actor,
            summary=register_summary,
            detail=detail,
        )
    )
    return claims


def assess(
    hook: Hook,
    proposals: Sequence[Proposal],
    decode: Path,
    validator: Validator,
    refutations: Sequence[Refutation] = (),
    ledger: EvidenceLedger | None = None,
    agreement_threshold: float = 0.5,
) -> Assessment:
    """Run one hook's proposals through validation, agreement, and refutation.

    A proposal is accepted only when it passes the deterministic validator, is
    the answer a majority of *distinct* proposers reached, and no verifier
    refuted it. Any of those failing produces an escalation carrying every
    proposal and every claim, so the human at the gate sees the disagreement
    rather than a verdict.
    """
    claims: list[EvidenceClaim] = []
    if not proposals:
        if ledger is not None:
            # Register anyway, so a hook nobody answered still appears in the
            # readiness report with every kind `not_exercised`. Silently missing
            # from the report reads as "nothing to worry about".
            ledger.register(Subject(hook.hook_id, "agent", proposed_by=NO_PROPOSER))
        return Assessment(
            hook.hook_id,
            None,
            (),
            (),
            reason=f"{hook.hook_id}: no proposals were produced",
        )

    # A payload with no idempotence marker applies once and is then invisible:
    # the applier finds no marker on a re-run and applies it AGAIN. Every one of
    # the first three real proposals omitted it, because it is a mechanical
    # requirement of this pipeline that nothing in the app tells them about.
    unmarked = [
        proposal
        for proposal in proposals
        if proposal.marker_count(hook) != hook.expected_marker_count
    ]
    validations = validate_proposals(hook, proposals, decode, validator)
    surviving = [
        proposal
        for proposal in proposals
        if str(validations.get(proposal.key, {}).get("verdict", "")).upper() == "OK"
    ]

    # One vote per proposer, everywhere. Counting a repeated answer as several
    # would let a single confidently-wrong agent manufacture its own consensus.
    votes = one_per_proposer(proposals)
    groups = group_by_fingerprint(votes, hook)
    winner_key, winner_group = max(
        groups.items(), key=lambda item: (len(item[1]), item[0])
    )

    claims.append(
        agreement_claim(
            hook.hook_id,
            [proposal.to_dict() for proposal in votes],
            threshold=agreement_threshold,
            # Same keys the decision uses, so the recorded claim cannot disagree
            # with the conclusion drawn from the same proposals.
            keys=[proposal.effect_key(hook) for proposal in votes],
        )
    )
    for proposal in proposals:
        claims.extend(
            _validator_claims(
                hook.hook_id, proposal.proposer, validations.get(proposal.key, {})
            )
        )

    winning_proposer = winner_group[0].proposer
    # A verifier that helped produce the accepted answer is not independent
    # evidence, and the ledger would refuse the claim. Separate them here so that
    # arrives as a stated escalation rather than as an exception thrown
    # mid-assessment.
    #
    # Two subtleties, both of which let a proposer clear its own work:
    #   - compared stripped, like the ledger does; exact equality means one
    #     trailing space defeats the check
    #   - every proposer in the winning group is disqualified, not just the first.
    #     `Subject.proposed_by` names only one of them, so the ledger cannot
    #     notice a co-author reviewing the answer it co-wrote.
    authors = {proposal.proposer.strip() for proposal in winner_group}
    self_refutations = [item for item in refutations if item.verifier.strip() in authors]
    independent_refutations = [
        item for item in refutations if item.verifier.strip() not in authors
    ]
    for refutation in independent_refutations:
        claims.append(
            EvidenceClaim(
                hook_id=hook.hook_id,
                kind=EvidenceKind.ADVERSARIAL_VERIFIED,
                verdict=Verdict.FAILED if refutation.refuted else Verdict.PASSED,
                producer=Producer.VERIFIER_AGENT,
                actor=refutation.verifier,
                summary=refutation.finding,
                detail={"checked": list(refutation.checked)},
            )
        )

    if ledger is not None:
        ledger.register(
            Subject(
                hook.hook_id,
                "agent",
                descriptor=winner_group[0].descriptor,
                proposed_by=winning_proposer,
            )
        )
        for claim in claims:
            ledger.record(claim)

    # Decide. Every branch below states which check refused.
    candidates = [proposal for proposal in winner_group if proposal in surviving]
    if unmarked:
        names = ", ".join(
            f"{p.proposer} ({p.marker_count(hook)}/{hook.expected_marker_count})"
            for p in unmarked
        )
        return Assessment(
            hook.hook_id,
            None,
            tuple(claims),
            tuple(proposals),
            validations,
            reason=(
                f"{hook.hook_id}: {names} produced a payload that does not carry the "
                f"idempotence marker {hook.marker!r}. Such a patch applies once and is "
                "then undetectable, so a second run applies it again."
            ),
        )
    if self_refutations:
        names = ", ".join(sorted({item.verifier.strip() for item in self_refutations}))
        return Assessment(
            hook.hook_id,
            None,
            tuple(claims),
            tuple(proposals),
            validations,
            reason=(
                f"{hook.hook_id}: {names} helped produce the accepted answer and also "
                "reviewed it. A verifier that is one of the proposers is not independent "
                "evidence, so this needs a genuine second opinion before it can advance."
            ),
        )
    if not surviving:
        reason = (
            f"{hook.hook_id}: no proposal survived the deterministic validator — "
            + "; ".join(
                f"{name}: {result.get('reason') or result.get('verdict', 'no result')}"
                for name, result in validations.items()
            )
        )
        return Assessment(hook.hook_id, None, tuple(claims), tuple(proposals), validations, reason)

    if any(refutation.refuted for refutation in independent_refutations):
        found = "; ".join(
            f"{item.verifier}: {item.finding}"
            for item in independent_refutations
            if item.refuted
        )
        return Assessment(
            hook.hook_id,
            None,
            tuple(claims),
            tuple(proposals),
            validations,
            reason=f"{hook.hook_id}: a verifier refuted the proposal — {found}",
        )

    share = len(winner_group) / len(votes)
    if share < agreement_threshold or len(winner_group) < 2:
        if len(votes) < 2:
            reason = (
                f"{hook.hook_id}: only one proposer answered, so there is nothing to "
                "corroborate it. Agreement across independent proposers is a required "
                "item of evidence, and a single answer cannot supply it however good it "
                "looks."
            )
        else:
            reason = (
                f"{hook.hook_id}: {len(winner_group)} of {len(votes)} distinct proposers "
                f"reached the most common answer ({len(groups)} distinct answers overall). "
                "That is genuine ambiguity, and it belongs at a gate rather than being "
                "broken by ranking."
            )
        return Assessment(
            hook.hook_id, None, tuple(claims), tuple(proposals), validations, reason=reason
        )

    if not candidates:
        return Assessment(
            hook.hook_id,
            None,
            tuple(claims),
            tuple(proposals),
            validations,
            reason=(
                f"{hook.hook_id}: the agreed answer (fingerprint {winner_key[:12]}) failed "
                "the deterministic validator. Agreement is not evidence when the thing "
                "agreed on does not check out against the decode."
            ),
        )

    return Assessment(
        hook.hook_id,
        candidates[0],
        tuple(claims),
        tuple(proposals),
        validations,
        reason=(
            f"{hook.hook_id}: {len(winner_group)} independent proposers agreed on "
            f"{candidates[0].descriptor}, it passed the validator, and no verifier "
            "refuted it"
        ),
    )


def load_proposals(path: Path | str) -> dict[str, list[Proposal]]:
    """Read a proposals JSON file, grouped by hook.

    Accepts either a flat list of proposal objects or ``{hook_id: [proposal]}``.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    grouped: dict[str, list[Proposal]] = {}
    entries: Iterable[Mapping[str, Any]]
    if isinstance(data, dict):
        collected = []
        for hook_id, items in data.items():
            for item in items:
                declared = item.get("hook_id", hook_id)
                if declared != hook_id:
                    # Letting the entry win would silently move an answer onto a
                    # hook nobody proposed for.
                    raise ProposalError(
                        f"proposal under {hook_id!r} declares hook_id {declared!r}; "
                        "a mislabelled entry must be corrected, not reassigned"
                    )
                collected.append({**item, "hook_id": hook_id})
        entries = collected
    else:
        entries = data
    for entry in entries:
        proposal = Proposal.from_dict(entry)
        grouped.setdefault(proposal.hook_id, []).append(proposal)
    return grouped


def accepted_hosts(assessments: Iterable[Assessment]) -> dict[str, list[str]]:
    """The ``proposals`` mapping :func:`dfinsta_pipeline.resolve.resolve_manifest` wants."""
    out: dict[str, list[str]] = {}
    for assessment in assessments:
        if assessment.accepted is not None:
            out[assessment.hook_id] = [assessment.accepted.descriptor]
    return out


def operations_for(
    assessments: Iterable[Assessment], hooks: Mapping[str, Hook]
) -> list[dict[str, Any]]:
    """Applier operations for accepted proposals only."""
    out = []
    for assessment in assessments:
        if assessment.accepted is None:
            raise ManifestError(
                f"{assessment.hook_id} was not accepted: {assessment.reason}"
            )
        out.append(assessment.accepted.as_operation(hooks[assessment.hook_id]))
    return out

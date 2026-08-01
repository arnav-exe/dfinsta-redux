"""The evidence ledger: what a hook must have accumulated before it may ship.

This is the control the rest of the Resolve stage exists to feed. The failure
mode this project keeps hitting is not a doubtful agent — it is a *confidently
wrong* one. Three separate inert patches passed every check that existed at the
time: the 340 `minshop`/`minishops` substitution that could never match, the 430
settings hook that was perfect statically and dead at runtime because a
MobileConfig flag selected the other action-bar implementation, and a verifier
that searched DEX bytes for a smali string form DEX does not store. In all three
the pipeline reported success. None of them would have been caught by asking the
proposer how sure it was.

So the ledger's rule is: **a hook advances only when every required item of
evidence is present, and each is produced by something other than the proposer.**
Absence is never a pass. A hook with no claims at all escalates with every
required kind recorded as `not_exercised`, rather than sailing through on the
strength of nothing having gone wrong.

`docs/ADK_PIPELINE_PLAN.md` already fixed the vocabulary, and this module uses it
rather than inventing a parallel one: a claim ends `passed`, `failed`,
`inconclusive`, `not_exercised`, `blocked`, or `waived`, and `EvidenceClaim` is
append-only.

**Confidence is recorded and never read.** `docs/ADK_PIPELINE_PLAN.md` puts it
exactly right — "numeric confidence is supplementary; automatic acceptance
requires deterministic uniqueness and proof obligations". Deleting the field
would lose useful signal for a human at a gate, so it is kept, but
:meth:`EvidenceLedger.readiness` never consults it. A test pins that: varying
confidence across its whole range must not change a single verdict.

**Retries are visible.** A required kind that reaches `passed` only after a
`failed` is flagged, because "run it again until it goes green" is the obvious
way to defeat a ledger, and the honest answer is to show a human the sequence
rather than to pretend the last attempt is the only one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .contracts import canonical_json, canonical_sha256

SCHEMA_VERSION = 1

#: Stands in for a proposer on a hook nobody answered, so the hook still appears
#: in the readiness report with every kind `not_exercised` instead of being
#: silently absent. A real registration may replace it.
NO_PROPOSER = "(none)"


class EvidenceError(ValueError):
    """Raised when a claim is malformed, self-attested, or waived without authority."""


class Verdict(str, Enum):
    """How one evidence claim ended. Vocabulary from `docs/ADK_PIPELINE_PLAN.md`."""

    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"
    NOT_EXERCISED = "not_exercised"
    BLOCKED = "blocked"
    WAIVED = "waived"

    @property
    def satisfies(self) -> bool:
        """Does this verdict let a hook advance? Only two do."""
        return self in {Verdict.PASSED, Verdict.WAIVED}

    @property
    def measured_negative(self) -> bool:
        """Did something actually look, and come back unsatisfied?

        Distinct from :attr:`satisfies` because ``not_exercised`` means no
        measurement happened at all — following that with a real one is the
        normal course of a run, not a retry. A ``failed``, ``inconclusive`` or
        ``blocked`` claim *did* measure, so a later pass over the top of it is
        the sequence a gate needs to see.
        """
        return self in {Verdict.FAILED, Verdict.INCONCLUSIVE, Verdict.BLOCKED}


class Producer(str, Enum):
    """What class of thing is allowed to produce a kind of evidence.

    The point of the taxonomy is that a proposer cannot produce its own
    corroboration. An agent that maps a hook is a ``VERIFIER_AGENT`` only when it
    is a *different* agent, and it can never be the ``DETERMINISTIC`` checker or
    the ``DEVICE``.
    """

    DETERMINISTIC = "deterministic"  # a checker that re-derives from the decode
    VERIFIER_AGENT = "verifier_agent"  # a second agent, instructed to refute
    STATISTICS = "statistics"  # agreement across k independent proposers
    DEVICE = "device"  # the phone; the only real oracle
    HUMAN = "human"  # a gate decision


class EvidenceKind(str, Enum):
    """The seven items in the ledger table of `pipeline_flowchart.md`."""

    ANCHOR_UNIQUE = "anchor_unique"
    REGISTERS_SAFE = "registers_safe"
    ADVERSARIAL_VERIFIED = "adversarial_verified"
    PROPOSER_AGREEMENT = "proposer_agreement"
    STATIC_VERIFIED = "static_verified"
    RUNTIME_PROBE = "runtime_probe"
    DIFFERENTIAL = "differential"


#: Who may produce each kind. A claim naming any other producer is rejected at
#: record time, so the "produced by something other than the proposer" rule is
#: enforced by the schema and not by reviewer attention.
ALLOWED_PRODUCERS: Mapping[EvidenceKind, frozenset[Producer]] = {
    EvidenceKind.ANCHOR_UNIQUE: frozenset({Producer.DETERMINISTIC}),
    EvidenceKind.REGISTERS_SAFE: frozenset({Producer.DETERMINISTIC}),
    EvidenceKind.ADVERSARIAL_VERIFIED: frozenset({Producer.VERIFIER_AGENT}),
    EvidenceKind.PROPOSER_AGREEMENT: frozenset({Producer.STATISTICS}),
    EvidenceKind.STATIC_VERIFIED: frozenset({Producer.DETERMINISTIC}),
    EvidenceKind.RUNTIME_PROBE: frozenset({Producer.DEVICE}),
    EvidenceKind.DIFFERENTIAL: frozenset({Producer.DEVICE}),
}

#: What each kind catches, quoted at the gate so a human reading an escalation
#: does not have to already know why the item is on the list.
CATCHES: Mapping[EvidenceKind, str] = {
    EvidenceKind.ANCHOR_UNIQUE: (
        "leading-whitespace anchors that match zero lines; duplicate A0H-style traps "
        "where the same iput appears for Options and for the follow button"
    ),
    EvidenceKind.REGISTERS_SAFE: "a payload clobbering a register that is read later",
    EvidenceKind.ADVERSARIAL_VERIFIED: (
        "confident wrong justifications and mis-sited anchors; one holdout proposer "
        "asserted a live listener register was 'usually null'"
    ),
    EvidenceKind.PROPOSER_AGREEMENT: "genuine ambiguity, measured rather than self-reported",
    EvidenceKind.STATIC_VERIFIED: "malformed injection that still assembles",
    EvidenceKind.RUNTIME_PROBE: (
        "inert patches: the 430 settings hook passed every static assertion and was "
        "dead at runtime"
    ),
    EvidenceKind.DIFFERENTIAL: "a port regression, told apart from a broken probe",
}

#: When each kind can physically exist. Four are derivable from the decode alone
#: and gate the *apply*; three require an artifact that does not exist until the
#: build has run, and gate the *release*. Collapsing the two would make the
#: pre-apply gate unsatisfiable — you cannot run a probe against an APK you have
#: refused to build — and the honest consequence is that a passing pre-apply gate
#: says nothing yet about whether the hook works.
PRE_APPLY = "pre_apply"
POST_BUILD = "post_build"

PHASES: Mapping[EvidenceKind, str] = {
    EvidenceKind.ANCHOR_UNIQUE: PRE_APPLY,
    EvidenceKind.REGISTERS_SAFE: PRE_APPLY,
    EvidenceKind.ADVERSARIAL_VERIFIED: PRE_APPLY,
    EvidenceKind.PROPOSER_AGREEMENT: PRE_APPLY,
    EvidenceKind.STATIC_VERIFIED: POST_BUILD,
    EvidenceKind.RUNTIME_PROBE: POST_BUILD,
    EvidenceKind.DIFFERENTIAL: POST_BUILD,
}

#: Required evidence depends on how the hook was resolved. A mechanically
#: resolved hook has no proposer to corroborate: the deterministic engine either
#: matched exactly one site or refused, and re-running it reproduces the same
#: answer. Demanding an adversarial verifier there would be theatre. An
#: agent-proposed hook needs all seven, because the proposal is the one thing
#: nothing else re-derives.
MECHANICAL_REQUIREMENTS: frozenset[EvidenceKind] = frozenset(
    {
        EvidenceKind.ANCHOR_UNIQUE,
        EvidenceKind.REGISTERS_SAFE,
        EvidenceKind.STATIC_VERIFIED,
        EvidenceKind.RUNTIME_PROBE,
        EvidenceKind.DIFFERENTIAL,
    }
)
AGENT_REQUIREMENTS: frozenset[EvidenceKind] = frozenset(EvidenceKind)


def requirements_for(provenance: str) -> frozenset[EvidenceKind]:
    if provenance == "mechanical":
        return MECHANICAL_REQUIREMENTS
    if provenance == "agent":
        return AGENT_REQUIREMENTS
    raise EvidenceError(
        f"unknown provenance {provenance!r}; expected 'mechanical' or 'agent'. "
        "An unrecognised provenance must not silently pick the smaller requirement set."
    )


@dataclass(frozen=True)
class EvidenceClaim:
    """One externally produced fact about one hook. Append-only; never edited.

    ``actor`` is the concrete identity that produced it — an agent id, a tool
    path, a device serial. ``proposed_by`` on the subject is checked against it,
    which is what makes self-attestation a schema error rather than a judgement
    call.
    """

    hook_id: str
    kind: EvidenceKind
    verdict: Verdict
    producer: Producer
    actor: str
    summary: str
    detail: Mapping[str, Any] = field(default_factory=dict)
    # Recorded for a human at the gate. `readiness()` never reads it.
    confidence: float | None = None
    # Required to waive: a waiver is a human decision, never an agent's.
    decision_id: str | None = None
    rationale: str = ""
    supersedes: str | None = None
    recorded_at: str = ""

    def __post_init__(self) -> None:
        if not self.hook_id.strip():
            raise EvidenceError("claim needs a hook_id")
        if not self.actor.strip():
            raise EvidenceError(
                f"{self.hook_id}/{self.kind.value}: claim needs an actor. Evidence with "
                "no identifiable producer cannot be checked against the proposer."
            )
        if not self.summary.strip():
            raise EvidenceError(f"{self.hook_id}/{self.kind.value}: claim needs a summary")
        allowed = ALLOWED_PRODUCERS[self.kind]
        human_waiver = self.producer is Producer.HUMAN and self.verdict is Verdict.WAIVED
        if self.producer not in allowed and not human_waiver:
            names = ", ".join(sorted(item.value for item in allowed))
            extra = ""
            if self.producer is Producer.HUMAN:
                # A human may decide to proceed without an item; that is a waiver
                # and reads as one at the gate and in the final report. Letting a
                # human record `passed` for a device-produced kind would erase the
                # difference between "the phone showed it" and "someone said so",
                # which is the whole distinction this ledger is built on.
                extra = (
                    " A human may waive this item, which is recorded as `waived`, but may "
                    "not attest to it as `passed`."
                )
            raise EvidenceError(
                f"{self.hook_id}/{self.kind.value}: {self.producer.value} may not produce "
                f"this evidence (allowed: {names}). The kind exists precisely because that "
                f"producer is independent of the proposer.{extra}"
            )
        if self.verdict is Verdict.WAIVED:
            if not self.decision_id or not self.decision_id.strip():
                raise EvidenceError(
                    f"{self.hook_id}/{self.kind.value}: a waiver needs a decision_id. "
                    "Only a human gate may waive required evidence; an agent waiving its "
                    "own missing proof is the exact failure this ledger exists to stop."
                )
            if not self.rationale.strip():
                raise EvidenceError(
                    f"{self.hook_id}/{self.kind.value}: a waiver needs a rationale"
                )
            if self.producer is not Producer.HUMAN:
                raise EvidenceError(
                    f"{self.hook_id}/{self.kind.value}: only a human may waive, not "
                    f"{self.producer.value}"
                )
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise EvidenceError(
                f"{self.hook_id}/{self.kind.value}: confidence must be within 0..1"
            )

    @property
    def claim_id(self) -> str:
        """Content hash of the claim, so a supersede chain can name its parent."""
        return canonical_sha256(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "hook_id": self.hook_id,
            "kind": self.kind.value,
            "verdict": self.verdict.value,
            "producer": self.producer.value,
            "actor": self.actor,
            "summary": self.summary,
            "detail": dict(self.detail),
            "confidence": self.confidence,
            "decision_id": self.decision_id,
            "rationale": self.rationale,
            "supersedes": self.supersedes,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EvidenceClaim:
        if data.get("schema_version") != SCHEMA_VERSION:
            raise EvidenceError(
                f"unsupported evidence schema {data.get('schema_version')!r}"
            )
        return cls(
            hook_id=data["hook_id"],
            kind=EvidenceKind(data["kind"]),
            verdict=Verdict(data["verdict"]),
            producer=Producer(data["producer"]),
            actor=data["actor"],
            summary=data["summary"],
            detail=dict(data.get("detail", {})),
            confidence=data.get("confidence"),
            decision_id=data.get("decision_id"),
            rationale=data.get("rationale", ""),
            supersedes=data.get("supersedes"),
            recorded_at=data.get("recorded_at", ""),
        )


@dataclass(frozen=True)
class Subject:
    """The hook the evidence is about, and who proposed it.

    ``proposed_by`` is empty for a mechanically resolved hook: the deterministic
    engine is not an actor that could be corroborating itself, and re-running it
    reproduces the resolution exactly.
    """

    hook_id: str
    provenance: str  # "mechanical" | "agent"
    descriptor: str | None = None
    proposed_by: str = ""

    def __post_init__(self) -> None:
        requirements_for(self.provenance)  # validates
        if self.provenance == "agent" and not self.proposed_by.strip():
            raise EvidenceError(
                f"{self.hook_id}: an agent-resolved hook must name its proposer, or "
                "'produced by something other than the proposer' cannot be checked"
            )
        if self.provenance == "mechanical" and self.proposed_by.strip():
            raise EvidenceError(
                f"{self.hook_id}: a mechanically resolved hook has no proposer; "
                f"got {self.proposed_by!r}"
            )

    @property
    def required(self) -> frozenset[EvidenceKind]:
        return requirements_for(self.provenance)


@dataclass(frozen=True)
class KindStatus:
    """The state of one required kind for one hook."""

    kind: EvidenceKind
    verdict: Verdict
    claim: EvidenceClaim | None
    attempts: int = 0
    recovered_from_failure: bool = False

    @property
    def satisfied(self) -> bool:
        return self.verdict.satisfies and not self.recovered_from_failure

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "verdict": self.verdict.value,
            "satisfied": self.satisfied,
            "attempts": self.attempts,
            "recovered_from_failure": self.recovered_from_failure,
            "catches": CATCHES[self.kind],
            "actor": self.claim.actor if self.claim else None,
            "summary": self.claim.summary if self.claim else "no claim recorded",
        }


@dataclass(frozen=True)
class Readiness:
    """Whether one hook may advance, and everything a gate needs to decide."""

    hook_id: str
    ready: bool
    statuses: tuple[KindStatus, ...]
    reasons: tuple[str, ...]

    @property
    def missing(self) -> tuple[EvidenceKind, ...]:
        return tuple(
            status.kind
            for status in self.statuses
            if status.verdict is Verdict.NOT_EXERCISED
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook_id,
            "ready": self.ready,
            "reasons": list(self.reasons),
            "statuses": [status.to_dict() for status in self.statuses],
        }


class EvidenceLedger:
    """Append-only claims about hooks, and the readiness they do or do not confer.

    Persisted as JSONL so the record stays greppable and diffable, and so a
    crashed run leaves every claim it had already earned. Nothing here rewrites
    a line: superseding a claim appends a new one naming the old.
    """

    def __init__(self, path: Path | str | None = None):
        self._path = Path(path) if path is not None else None
        self._subjects: dict[str, Subject] = {}
        self._claims: list[EvidenceClaim] = []

    # ------------------------------------------------------------------ state

    def register(self, subject: Subject) -> None:
        existing = self._subjects.get(subject.hook_id)
        if existing is not None and existing != subject:
            if existing.proposed_by == NO_PROPOSER:
                # A hook registered because nobody answered it is a placeholder,
                # there so it appears in the readiness report with every kind
                # `not_exercised` rather than being silently absent. A later pass
                # that does have proposals must be able to replace it.
                self._subjects[subject.hook_id] = subject
                return
            raise EvidenceError(
                f"{subject.hook_id} is already registered as {existing.provenance!r} "
                f"proposed by {existing.proposed_by!r}; re-registering it differently "
                "would silently change which evidence is required"
            )
        self._subjects[subject.hook_id] = subject

    def record(self, claim: EvidenceClaim) -> EvidenceClaim:
        """Append one claim, refusing self-attestation and unauthorised waivers."""
        subject = self._subjects.get(claim.hook_id)
        if subject is None:
            raise EvidenceError(
                f"{claim.hook_id} is not registered; a claim about an unknown subject "
                "cannot be checked against its proposer"
            )
        # Compared stripped, like every other identity in this module. Exact
        # equality would let one trailing space defeat the check.
        if subject.proposed_by.strip() and claim.actor.strip() == subject.proposed_by.strip():
            raise EvidenceError(
                f"{claim.hook_id}/{claim.kind.value}: actor {claim.actor!r} proposed this "
                "hook and may not also produce its evidence. Every item in the ledger must "
                "come from something other than the proposer."
            )
        if claim.kind not in subject.required:
            # Not an error: extra evidence is welcome, it just cannot be what
            # makes a hook ready. Recorded so a gate still sees it.
            pass
        self._claims.append(claim)
        if self._path is not None:
            self._append_to_disk(claim)
        return claim

    def _append_to_disk(self, claim: EvidenceClaim) -> None:
        assert self._path is not None
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as handle:
            handle.write(canonical_json(claim.to_dict()))
            handle.write("\n")

    @property
    def claims(self) -> tuple[EvidenceClaim, ...]:
        return tuple(self._claims)

    def claims_for(self, hook_id: str, kind: EvidenceKind | None = None) -> tuple[EvidenceClaim, ...]:
        return tuple(
            claim
            for claim in self._claims
            if claim.hook_id == hook_id and (kind is None or claim.kind is kind)
        )

    # -------------------------------------------------------------- readiness

    def readiness(self, hook_id: str, phase: str | None = None) -> Readiness:
        """May this hook advance?

        With *phase*, only the kinds that can exist at that point are required —
        ``pre_apply`` before the build, ``post_build`` after it, and ``None`` for
        the full set that gates a release. A pre-apply pass is not a statement
        that the hook works; it only says nothing derivable from the decode
        objects yet.

        Deliberately does not read `confidence`. The decision is a function of
        which externally produced claims exist and how they ended, nothing else.
        """
        subject = self._subjects.get(hook_id)
        if subject is None:
            raise EvidenceError(f"{hook_id} is not registered")
        if phase is not None and phase not in {PRE_APPLY, POST_BUILD}:
            raise EvidenceError(
                f"unknown phase {phase!r}; expected {PRE_APPLY!r} or {POST_BUILD!r}"
            )

        required = subject.required
        if phase is not None:
            required = frozenset(kind for kind in required if PHASES[kind] == phase)

        statuses: list[KindStatus] = []
        reasons: list[str] = []
        for kind in sorted(required, key=lambda item: item.value):
            history = self.claims_for(hook_id, kind)
            if not history:
                statuses.append(KindStatus(kind, Verdict.NOT_EXERCISED, None))
                reasons.append(
                    f"{kind.value}: no claim recorded — catches {CATCHES[kind]}"
                )
                continue
            latest = history[-1]
            # Any prior verdict that measured and came back unsatisfied counts,
            # not just `failed`. The Reels probe's characteristic bad state is
            # `inconclusive` — zero signal with the toggle on and off — so
            # "re-run until the counts happen to differ" would otherwise be the
            # one uncovered path through the guard.
            failed_before = any(
                claim.verdict.measured_negative for claim in history[:-1]
            )
            recovered = failed_before and latest.verdict.satisfies
            statuses.append(
                KindStatus(kind, latest.verdict, latest, len(history), recovered)
            )
            if recovered:
                reasons.append(
                    f"{kind.value}: reached {latest.verdict.value} only after a failure "
                    f"({len(history)} attempts). Re-running until green is how a ledger "
                    "gets defeated, so this goes to a human with the sequence attached."
                )
            elif not latest.verdict.satisfies:
                reasons.append(f"{kind.value}: {latest.verdict.value} — {latest.summary}")

        ready = all(status.satisfied for status in statuses)
        return Readiness(hook_id, ready, tuple(statuses), tuple(reasons))

    def report(self, phase: str | None = None) -> dict[str, Any]:
        """Readiness for every registered hook, plus what must go to a gate."""
        readiness = {
            hook_id: self.readiness(hook_id, phase) for hook_id in sorted(self._subjects)
        }
        escalations = [item.to_dict() for item in readiness.values() if not item.ready]
        return {
            "schema_version": SCHEMA_VERSION,
            "phase": phase or "release",
            "complete": not escalations,
            "hooks": {hook_id: item.to_dict() for hook_id, item in readiness.items()},
            "escalations": escalations,
            "claim_count": len(self._claims),
        }

    # ------------------------------------------------------------ persistence

    @classmethod
    def load(cls, path: Path | str, subjects: Iterable[Subject]) -> EvidenceLedger:
        """Rebuild from an existing JSONL, re-validating every claim.

        Subjects are supplied by the caller rather than stored, because which
        evidence a hook requires follows from how *this* run resolved it, not
        from how a previous one did.
        """
        ledger = cls(path)
        for subject in subjects:
            ledger.register(subject)
        path = Path(path)
        if not path.exists():
            return ledger
        with open(path, "r", encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    claim = EvidenceClaim.from_dict(json.loads(line))
                except (json.JSONDecodeError, KeyError, ValueError) as error:
                    raise EvidenceError(f"{path}:{number}: unreadable claim: {error}") from error
                subject = ledger._subjects.get(claim.hook_id)
                if subject is None:
                    raise EvidenceError(
                        f"{path}:{number}: claim about unregistered hook {claim.hook_id!r}"
                    )
                # Compared stripped, like every other identity in this module:
                # exact equality would let one trailing space defeat the check.
                proposer = subject.proposed_by.strip()
                if proposer and claim.actor.strip() == proposer:
                    raise EvidenceError(
                        f"{path}:{number}: {claim.hook_id} evidence was produced by its own "
                        "proposer; the stored ledger is not trustworthy"
                    )
                ledger._claims.append(claim)
        return ledger


# ------------------------------------------------------------------- builders


def deterministic_claim(
    hook_id: str,
    kind: EvidenceKind,
    passed: bool,
    actor: str,
    summary: str,
    detail: Mapping[str, Any] | None = None,
) -> EvidenceClaim:
    """A claim from a checker that re-derived the fact from the decode itself."""
    return EvidenceClaim(
        hook_id=hook_id,
        kind=kind,
        verdict=Verdict.PASSED if passed else Verdict.FAILED,
        producer=Producer.DETERMINISTIC,
        actor=actor,
        summary=summary,
        detail=dict(detail or {}),
    )


def agreement_claim(
    hook_id: str,
    proposals: Sequence[Mapping[str, Any]],
    actor: str = "resolve.proposer_agreement",
    threshold: float = 0.5,
) -> EvidenceClaim:
    """Agreement across k independent proposers, computed rather than asserted.

    Agreement is on the *descriptor plus anchor* a proposer arrived at, never on
    a self-reported score. Unanimity is not required — the holdout that justified
    building this had two of three proposers reach the hard settings site and the
    third fail outright — but a plurality below ``threshold`` is genuine ambiguity
    and must reach a human.
    """
    # A proposal with no host or no anchor found nothing. Counting those as
    # agreeing lets two proposers that failed outright out-vote one that
    # succeeded, on the hash of an empty answer — "absence is a pass" in the one
    # place this module most forbids it.
    answered = [
        proposal
        for proposal in proposals
        if str(proposal.get("descriptor") or "").strip()
        and list(proposal.get("anchor", ()))
    ]
    if not answered:
        return EvidenceClaim(
            hook_id=hook_id,
            kind=EvidenceKind.PROPOSER_AGREEMENT,
            verdict=Verdict.NOT_EXERCISED,
            producer=Producer.STATISTICS,
            actor=actor,
            summary=(
                "no proposals to compare"
                if not proposals
                else f"none of {len(proposals)} proposals named both a host and an anchor"
            ),
            detail={"proposals": len(proposals), "answered": 0},
        )
    tally: dict[str, int] = {}
    for proposal in answered:
        key = canonical_sha256(
            {
                "descriptor": proposal.get("descriptor"),
                "anchor": list(proposal.get("anchor", ())),
            }
        )
        tally[key] = tally.get(key, 0) + 1
    best_key, best_count = max(tally.items(), key=lambda item: (item[1], item[0]))
    # Share is over everyone who was asked, not only those who answered: two of
    # five agreeing is weak corroboration even if the other three abstained.
    share = best_count / len(proposals)
    agreed = share >= threshold and best_count > 1
    return EvidenceClaim(
        hook_id=hook_id,
        kind=EvidenceKind.PROPOSER_AGREEMENT,
        verdict=Verdict.PASSED if agreed else Verdict.INCONCLUSIVE,
        producer=Producer.STATISTICS,
        actor=actor,
        summary=(
            f"{best_count} of {len(proposals)} proposers agreed on the same "
            f"descriptor and anchor ({share:.0%})"
        ),
        detail={
            "proposals": len(proposals),
            "answered": len(answered),
            "agreed": best_count,
            "share": round(share, 4),
            "threshold": threshold,
            "distinct_answers": len(tally),
            "winning_fingerprint": best_key,
        },
    )


def probe_claim(
    hook_id: str,
    surface: str,
    signal: str,
    enabled_observations: int,
    disabled_observations: int,
    requires_two_directional_delta: bool,
    actor: str,
    waiver_note: str = "",
) -> EvidenceClaim:
    """A runtime probe result from the device, judged on the delta, not the count.

    Zero signal in both directions is ``inconclusive``, never a pass. That is the
    Reels case exactly: `replaceReelsEndpoint` blanks the endpoint upstream of
    `throwIfBlocked`, so block-counting sees nothing whether the toggle is on or
    off. Reading that as "no blocks, nothing wrong" would certify an inert hook.
    """
    if enabled_observations < 0 or disabled_observations < 0:
        # A harness returning -1 for "the probe did not run" would otherwise be
        # read as a delta and certify the hook.
        raise EvidenceError(
            f"{hook_id}: observation counts must be >= 0, got "
            f"{enabled_observations} enabled and {disabled_observations} disabled. "
            "A negative count is a harness sentinel, not a measurement."
        )
    detail = {
        "surface": surface,
        "signal": signal,
        "enabled_observations": enabled_observations,
        "disabled_observations": disabled_observations,
        "requires_two_directional_delta": requires_two_directional_delta,
    }
    if not requires_two_directional_delta:
        if not waiver_note.strip():
            raise EvidenceError(
                f"{hook_id}: a probe with no two-directional delta must say why. "
                "A silent waiver is how an inert hook passes verification."
            )
        detail["waiver_note"] = waiver_note
        satisfied = enabled_observations > 0
        return EvidenceClaim(
            hook_id=hook_id,
            kind=EvidenceKind.RUNTIME_PROBE,
            verdict=Verdict.PASSED if satisfied else Verdict.FAILED,
            producer=Producer.DEVICE,
            actor=actor,
            summary=(
                f"{surface}: {enabled_observations} observation(s) of {signal!r}; "
                "not toggleable, so presence is the whole proof"
            ),
            detail=detail,
        )
    if enabled_observations == disabled_observations:
        verdict = Verdict.INCONCLUSIVE
        summary = (
            f"{surface}: {signal!r} observed {enabled_observations} time(s) with the "
            "toggle on and the same with it off. No delta in either direction means the "
            "probe cannot see this hook — it is not a pass."
        )
    elif disabled_observations > enabled_observations:
        verdict = Verdict.FAILED
        summary = (
            f"{surface}: the signal moved the wrong way "
            f"({enabled_observations} on vs {disabled_observations} off)"
        )
    else:
        verdict = Verdict.PASSED
        summary = (
            f"{surface}: {signal!r} present {enabled_observations} time(s) enabled and "
            f"{disabled_observations} disabled — a two-directional delta"
        )
    return EvidenceClaim(
        hook_id=hook_id,
        kind=EvidenceKind.RUNTIME_PROBE,
        verdict=verdict,
        producer=Producer.DEVICE,
        actor=actor,
        summary=summary,
        detail=detail,
    )


def waiver(
    hook_id: str,
    kind: EvidenceKind,
    decision_id: str,
    actor: str,
    rationale: str,
    supersedes: str | None = None,
) -> EvidenceClaim:
    """A human waiving one required item, on the record, with a reason."""
    return EvidenceClaim(
        hook_id=hook_id,
        kind=kind,
        verdict=Verdict.WAIVED,
        producer=Producer.HUMAN,
        actor=actor,
        summary=f"waived at gate: {rationale}",
        decision_id=decision_id,
        rationale=rationale,
        supersedes=supersedes,
    )


def stamped(claim: EvidenceClaim, recorded_at: str) -> EvidenceClaim:
    """Attach a timestamp outside the builders, so nothing here reads the clock.

    Workflow code must stay deterministic under replay, so the time comes from
    the caller — an Activity or a test — rather than from `datetime.now()` here.
    """
    return replace(claim, recorded_at=recorded_at)

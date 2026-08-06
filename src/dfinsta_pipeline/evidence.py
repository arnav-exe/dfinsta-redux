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

#: A hook this run did not apply, because the decode already carries its patch.
#: Register liveness cannot be re-derived once the payload is in place — the
#: check needs an unpatched anchor — so demanding it would make a normal re-run
#: permanently unsatisfiable. What remains is genuine and deterministic: the
#: marker is this pipeline's own idempotence stamp, and its presence at exactly
#: the expected count in exactly one class proves this exact payload is in this
#: exact place. The post-build kinds still apply in full, so the built APK is
#: held to the same standard however its patches got there.
ALREADY_APPLIED_REQUIREMENTS: frozenset[EvidenceKind] = frozenset(
    {
        EvidenceKind.ANCHOR_UNIQUE,
        EvidenceKind.STATIC_VERIFIED,
        EvidenceKind.RUNTIME_PROBE,
        EvidenceKind.DIFFERENTIAL,
    }
)

PROVENANCES = ("mechanical", "agent", "already_applied")


def requirements_for(provenance: str) -> frozenset[EvidenceKind]:
    if provenance == "mechanical":
        return MECHANICAL_REQUIREMENTS
    if provenance == "agent":
        return AGENT_REQUIREMENTS
    if provenance == "already_applied":
        return ALREADY_APPLIED_REQUIREMENTS
    raise EvidenceError(
        f"unknown provenance {provenance!r}; expected one of {', '.join(PROVENANCES)}. "
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
    #: Which Instagram version this claim is about, e.g. "440".
    #:
    #: Added 2026-08-06. Until then a claim carried `hook_id` and nothing else
    #: identifying the port, so the version was knowable only from the *filename*
    #: a human chose (`manifest/runtime_evidence/440.jsonl`) — in the path, not
    #: the data. A report cannot join what a path spells.
    version: str | None = None
    #: The APK the claim is about, when there is one.
    #:
    #: **Absent means "this claim predates the artifact", not "unknown".** Every
    #: pre-apply kind is recorded before anything is built, so `anchor_unique` and
    #: `registers_safe` can never carry one and a reader must not treat their
    #: absence as a gap. The post-build kinds can, and a `runtime_probe` that does
    #: not is the case that matters: 440's device evidence names a device serial
    #: and never which APK was installed.
    build_sha256: str | None = None

    def __post_init__(self) -> None:
        if not self.hook_id.strip():
            raise EvidenceError("claim needs a hook_id")
        if self.version is not None and not self.version.strip():
            raise EvidenceError(
                f"{self.hook_id}/{self.kind.value}: version is present and blank. Omit it "
                "rather than recording an empty one — absent says 'not attributed', an "
                "empty string says 'attributed to nothing'."
            )
        if self.build_sha256 is not None and (
            len(self.build_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.build_sha256)
        ):
            raise EvidenceError(
                f"{self.hook_id}/{self.kind.value}: build_sha256 must be a lowercase "
                f"SHA-256, got {self.build_sha256!r}"
            )
        if self.build_sha256 is not None and PHASES[self.kind] == PRE_APPLY:
            raise EvidenceError(
                f"{self.hook_id}/{self.kind.value}: a pre-apply claim cannot name a build. "
                "It is recorded before anything is built, so a hash here would be a claim "
                "about an artifact that did not exist when the fact was established."
            )
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
        data = {
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
        # An unattributed claim writes NO KEY, rather than a null. `claim_id` is a
        # content hash of exactly this dict and `supersedes` names a parent by it,
        # so emitting `"version": null` on every claim would change the id of every
        # claim already on disk and break every stored supersede chain. Same rule
        # the gate journal follows for `payload_sha256`, and for the same reason:
        # an additive field must leave pre-change files byte-identical.
        if self.version is not None:
            data["version"] = self.version
        if self.build_sha256 is not None:
            data["build_sha256"] = self.build_sha256
        return data

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
            version=data.get("version"),
            build_sha256=data.get("build_sha256"),
        )


@dataclass(frozen=True)
class Attribution:
    """What run a batch of claims belongs to: when, which version, which build.

    Held by an :class:`EvidenceLedger` and applied in `record`, so a run has one
    place that can forget rather than one per builder.

    `build_sha256` is set only once the APK exists, which is why it is separate
    from the other two and why `with_build` returns a new value rather than
    mutating: the pre-apply claims of a run are recorded before the build and are
    genuinely not about any artifact. See :func:`attributed`.
    """

    recorded_at: str
    version: str
    build_sha256: str | None = None

    def __post_init__(self) -> None:
        if not self.recorded_at.strip():
            raise EvidenceError("attribution needs a recorded_at")
        if not self.version.strip():
            raise EvidenceError("attribution needs a version")
        # Checked HERE, where the value still has a provenance to name, and not
        # only at the `record` that eventually uses it. Without this the digest
        # was accepted by `bind_build`, carried silently, and rejected several
        # steps later by `EvidenceClaim` -- by which point the error names a hook
        # rather than the report the value came out of. `driver` binds from a
        # verifier report's `apk_sha256` and had its own weaker length-only
        # check, so an uppercase digest passed there and killed a finished port
        # from inside the claim builder. One value, one rule.
        if self.build_sha256 is not None and (
            len(self.build_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.build_sha256)
        ):
            raise EvidenceError(
                f"attribution build_sha256 must be a lowercase SHA-256, got "
                f"{self.build_sha256!r}"
            )

    def with_build(self, build_sha256: str) -> Attribution:
        return replace(self, build_sha256=build_sha256)

    def apply(self, claim: EvidenceClaim) -> EvidenceClaim:
        return attributed(
            claim,
            recorded_at=self.recorded_at,
            version=self.version,
            build_sha256=self.build_sha256,
        )


@dataclass(frozen=True)
class Subject:
    """The hook the evidence is about, and who proposed it.

    ``proposed_by`` is empty for a mechanically resolved hook: the deterministic
    engine is not an actor that could be corroborating itself, and re-running it
    reproduces the resolution exactly.
    """

    hook_id: str
    provenance: str  # one of PROVENANCES
    descriptor: str | None = None
    proposed_by: str = ""

    def __post_init__(self) -> None:
        requirements_for(self.provenance)  # validates
        if self.provenance == "agent" and not self.proposed_by.strip():
            raise EvidenceError(
                f"{self.hook_id}: an agent-resolved hook must name its proposer, or "
                "'produced by something other than the proposer' cannot be checked"
            )
        if self.provenance != "agent" and self.proposed_by.strip():
            raise EvidenceError(
                f"{self.hook_id}: a {self.provenance} hook has no proposer; "
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

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        attribution: Attribution | None = None,
    ):
        self._path = Path(path) if path is not None else None
        self._subjects: dict[str, Subject] = {}
        self._claims: list[EvidenceClaim] = []
        # Set once per run by whoever knows what the run is about, and applied in
        # `record`. Attaching it here rather than at each recording site is the
        # point: a claim reaches the file through exactly one method, so there is
        # one place that can forget, instead of one per builder. Left None by
        # tests and by any caller that has no run identity, and an unattributed
        # claim is written exactly as before.
        self._attribution = attribution

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

    def bind_build(self, build_sha256: str) -> None:
        """Name the artifact this run's later claims are about.

        Separate from the constructor because the APK does not exist when the
        ledger is opened: a run records its pre-apply evidence, then builds, then
        records what the build proved. Claims already written keep no build hash,
        which is correct — they were established before there was one.

        Rebinding to a *different* hash is refused. One run produces one APK, and
        a ledger whose later claims silently pointed at a second one would be the
        worst kind of wrong: every claim individually true, the set describing no
        artifact that ever existed.
        """

        if self._attribution is None:
            return
        existing = self._attribution.build_sha256
        if existing is not None and existing != build_sha256:
            raise EvidenceError(
                f"this ledger's claims already name build {existing}; refusing to "
                f"re-bind to {build_sha256}. One run, one artifact."
            )
        self._attribution = self._attribution.with_build(build_sha256)

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
        if self._attribution is not None:
            if claim.version is None:
                claim = self._attribution.apply(claim)
            elif not claim.recorded_at:
                # A claim that arrives with its own version keeps it —
                # `differential` is the case, since it is about two versions and
                # a single `version` field cannot express that. But it must still
                # be *dated*: skipping attribution whole meant such a claim kept
                # `recorded_at=""`, which is the very hole this all closes, and
                # bringing your own version is no reason to be unorderable in
                # time. The build hash is deliberately still withheld — a claim
                # spanning two builds cannot name one of them.
                claim = replace(claim, recorded_at=self._attribution.recorded_at)
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


@dataclass(frozen=True)
class AnswerShape:
    """What a complete answer to one kind of question looks like.

    Agreement is measured over answers, and a proposal missing the thing it was
    asked for did not answer. That rule is not negotiable — counting empty
    answers as agreeing lets two proposers that failed outright out-vote one that
    succeeded, on the hash of an empty answer, which is "absence is a pass" in
    the one place this module most forbids it. But *which* fields make an answer
    complete depends on what was asked.

    Two questions are asked in this pipeline. ``proposer.proposer_prompt`` asks
    for a whole patch, and an answer to it names a host and an anchor.
    ``proposer.host_prompt`` asks only which class, deliberately: the manifest
    already owns the anchor pattern and the payload template, and asking an agent
    to reinvent them manufactures the variance that then reads as disagreement.
    Measured on 439 — 2 of 3 proposers reached the correct host, 1 of 3 agreed
    once anchors and payloads were compared. Judging a host answer by the
    whole-patch shape scores it on fields nobody asked it for, so every host
    agreement tallies as zero answered and comes back ``not_exercised``.

    So a caller names the question rather than relaxing the check, and the named
    fields do both jobs: a proposal answers only if all of them are present, and
    identity for the tally is over exactly those fields and no others. One list,
    so a question can never be scored on a field it did not ask about.
    """

    #: Recorded on the claim, so a gate can tell a host agreement from a
    #: whole-patch one rather than having to infer it from the summary.
    name: str
    #: Every field an answer must carry, and the whole of its identity.
    fields: tuple[str, ...]
    #: How the summary names what the unanswered proposals were missing.
    wanted: str

    def answered(self, proposal: Mapping[str, Any]) -> bool:
        """Did this proposal supply everything the question asked for?

        Whitespace is not an answer, and neither is an empty list: an agent that
        returns ``{"descriptor": "  "}`` found nothing and said so at length.
        """
        return all(_supplied(proposal.get(field)) for field in self.fields)

    def identity(self, proposal: Mapping[str, Any]) -> str:
        """Content hash of the asked-for fields, and of nothing else.

        Two host proposals that name the same class agree, whatever else their
        dicts carry, because the class is the entire question. Hashing a field
        the question did not ask about would split a genuine agreement.
        """
        return canonical_sha256(
            {field: _comparable(proposal.get(field)) for field in self.fields}
        )


def _supplied(value: Any) -> bool:
    """Is this field an answer, rather than the absence of one?

    Whitespace is not an answer — an agent that returns ``{"descriptor": "  "}``
    found nothing and formatted it — and neither is an empty anchor, which names
    no site to inject at. Every other empty form (``None``, ``[]``, ``()``, an
    absent key) is absence too, so the rule is truthiness with strings stripped
    first.
    """
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def _comparable(value: Any) -> Any:
    """Normalise a field for hashing, so the container type decides nothing.

    Callers thread proposals through JSON and through dataclasses; a tuple anchor
    and an equal list anchor are the same answer.
    """
    if isinstance(value, tuple):
        return list(value)
    return value


#: An answer to "where does this patch go and what does it say" — the shape
#: `PROPOSAL_SCHEMA` asks for. The default, because widening what counts as an
#: answer must be a thing a call site says out loud.
FULL_PROPOSAL = AnswerShape(
    "full_proposal", ("descriptor", "anchor"), "both a host and an anchor"
)

#: An answer to "which class" — the shape `HOST_SCHEMA` asks for. There is no
#: anchor to require because none was requested; see :class:`AnswerShape`.
HOST_ONLY = AnswerShape("host", ("descriptor",), "a host class")


def agreement_claim(
    hook_id: str,
    proposals: Sequence[Mapping[str, Any]],
    actor: str = "resolve.proposer_agreement",
    threshold: float = 0.5,
    keys: Sequence[str] | None = None,
    asked: AnswerShape = FULL_PROPOSAL,
) -> EvidenceClaim:
    """Agreement across k independent proposers, computed rather than asserted.

    Agreement is on what a proposer arrived at, never on a self-reported score —
    the *descriptor plus anchor* for a whole-patch proposal, the descriptor alone
    when *asked* is :data:`HOST_ONLY` and a class is the whole question.
    Unanimity is not required — the holdout that justified building this had two
    of three proposers reach the hard settings site and the third fail outright —
    but a plurality below ``threshold`` is genuine ambiguity and must reach a
    human.

    The claim is a ``proposer_agreement`` either way. What it catches, who may
    produce it and when it can exist are identical for both questions; only the
    shape of an answer differs, and that is data about this claim rather than a
    second kind of evidence. ``detail["asked"]`` records which question it
    answers, so a gate is never shown agreement about a class and left to assume
    agreement about a patch.

    *proposals* must already be one per proposer. This counts what it is given:
    the same agent's answer three times is three votes here, which is why
    ``assess`` and ``host_agreement`` collapse through
    :func:`~dfinsta_pipeline.proposals.one_per_proposer` before calling.
    """
    if keys is not None and len(keys) != len(proposals):
        raise EvidenceError(
            f"{hook_id}: {len(keys)} agreement keys for {len(proposals)} proposals; "
            "a mismatched pairing would tally the wrong answers together"
        )
    indexed = list(enumerate(proposals))
    # A proposal that did not supply what was asked for found nothing. Counting
    # those as agreeing lets two proposers that failed outright out-vote one that
    # succeeded, on the hash of an empty answer — "absence is a pass" in the one
    # place this module most forbids it.
    answered = [
        (position, proposal)
        for position, proposal in indexed
        if asked.answered(proposal)
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
                else f"none of {len(proposals)} proposals named {asked.wanted}"
            ),
            detail={"proposals": len(proposals), "answered": 0, "asked": asked.name},
        )
    tally: dict[str, int] = {}
    for position, proposal in answered:
        # `keys` lets the caller tally on what a proposal would DO rather than on
        # the text it quoted. Two proposers can locate the same insertion point
        # with anchors of different lengths; tallying raw text calls that a
        # disagreement, and then this claim contradicts the decision made from
        # the same proposals.
        key = keys[position] if keys is not None else asked.identity(proposal)
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
            f"{best_count} of {len(proposals)} proposers independently reached the "
            f"same answer ({share:.0%})"
        ),
        detail={
            "proposals": len(proposals),
            "answered": len(answered),
            "agreed": best_count,
            "share": round(share, 4),
            "threshold": threshold,
            "distinct_answers": len(tally),
            "winning_fingerprint": best_key,
            # Which question this agreement answers. A host agreement and a
            # whole-patch agreement satisfy the same required item, so the record
            # has to say which one a gate is looking at.
            "asked": asked.name,
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


def attributed(
    claim: EvidenceClaim,
    *,
    recorded_at: str,
    version: str,
    build_sha256: str | None = None,
) -> EvidenceClaim:
    """Attach when, which version, and — when there is one — which build.

    **One call rather than three, because the failure mode is partial
    attribution.** Every one of these is optional on the claim and each was
    missing for a different reason: `recorded_at` because `stamped()` existed and
    the driver never called it, `version` because it was never a field, and
    `build_sha256` because nothing joined a probe to the APK it ran against. Three
    separate `replace(...)` calls at each recording site is three chances to fill
    two and forget the third, and a claim with a version and no timestamp is
    exactly as unjoinable as one with neither.

    `build_sha256` is genuinely optional and its absence is meaningful: pre-apply
    evidence is established before anything is built. A caller recording a whole
    run's claims has one build hash and a mix of phases, so **this drops it for
    the pre-apply kinds** rather than making every call site classify its own
    claim — that classification is `PHASES`, and asking each caller to repeat it
    is how the two copies drift.

    `EvidenceClaim` still *refuses* a pre-apply claim naming a build. The split is
    deliberate: this helper is the safe path for a caller attributing a batch,
    and the constructor is the strict one for anyone building a claim by hand.
    Relaxing the constructor to match would remove the check entirely.

    The clock still belongs to the caller, for the same replay-determinism reason
    `stamped` documents.
    """

    return replace(
        claim,
        recorded_at=recorded_at,
        version=version,
        build_sha256=None if PHASES[claim.kind] == PRE_APPLY else build_sha256,
    )

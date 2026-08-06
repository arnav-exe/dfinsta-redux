"""Tests for the evidence ledger — the control that decides whether a hook ships.

The module under test exists because of a specific, repeated failure: three inert
patches (340 `minshop`, the 430 settings hook, the DEX-string verifier) passed
every check the pipeline had and were dead at runtime. Nothing about them looked
uncertain. So the rule the ledger enforces is not "is the proposer confident" but
"does every required item of evidence exist, produced by something other than the
proposer". Absence is never a pass.

These tests are written from that rule rather than from the code's shape. The
ones that matter most are the ones that would still fail if someone rewrote the
module: a hook with no claims escalates rather than sails through; a proposer
cannot corroborate itself; a human may waive but may not attest; an inconclusive
probe is not a pass; re-running until green is visible; and `confidence` — which
is recorded — moves no verdict at all.

`MutationTests` does not add coverage. It re-attacks four guards that already
have positive tests, from the direction a broken implementation would take, so
that "the guard is present" and "the guard bites" stay separate claims. Each of
its docstrings says what would reach a device if that guard were removed.

`AnswerShapeAgreementTests` covers the one place where "absence is never a pass"
had to be made narrower without being weakened: agreement is scored against the
question that was asked, so a host proposal answering "which class" counts, while
a proposal that supplied nothing still does not — in either shape.

`KnownGapTests` pins four behaviours that are reported rather than fixed. Each
records what today's code does so a future fix fails loudly instead of silently
changing what the ledger certifies.
"""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from dfinsta_pipeline.evidence import (
    AGENT_REQUIREMENTS,
    ALLOWED_PRODUCERS,
    CATCHES,
    FULL_PROPOSAL,
    HOST_ONLY,
    MECHANICAL_REQUIREMENTS,
    SCHEMA_VERSION,
    AnswerShape,
    EvidenceClaim,
    EvidenceError,
    EvidenceKind,
    EvidenceLedger,
    KindStatus,
    Producer,
    Readiness,
    Subject,
    Verdict,
    agreement_claim,
    attributed,
    deterministic_claim,
    probe_claim,
    requirements_for,
    stamped,
    waiver,
)
from dfinsta_pipeline import evidence as evidence_module


# --------------------------------------------------------------------- fixture

# One plausible identity per producer class. They are deliberately unlike each
# other and unlike PROPOSER, because the whole point of the taxonomy is that the
# thing producing evidence is not the thing that proposed the hook.
ACTORS = {
    Producer.DETERMINISTIC: "verify.deterministic_checks",
    Producer.VERIFIER_AGENT: "agent:holdout-verifier",
    Producer.STATISTICS: "resolve.proposer_agreement",
    Producer.DEVICE: "device:R58N1234567",
    Producer.HUMAN: "sam@dfinsta",
}

PROPOSER = "agent:resolver-1"

# The two kinds a mechanically resolved hook does not need: there is no proposer
# to refute and no proposal to measure agreement about.
AGENT_ONLY = frozenset(
    {EvidenceKind.ADVERSARIAL_VERIFIED, EvidenceKind.PROPOSER_AGREEMENT}
)

STAMP = "2026-08-01T12:00:00Z"


def sole_producer(kind: EvidenceKind) -> Producer:
    """The one producer allowed for a kind.

    Every kind currently allows exactly one; the assertion is here so that a
    future kind allowing two makes this helper fail rather than pick arbitrarily
    out of a frozenset whose iteration order is not stable across runs.
    """
    allowed = ALLOWED_PRODUCERS[kind]
    assert len(allowed) == 1, f"{kind.value} allows {len(allowed)} producers"
    return next(iter(allowed))


def claim_for(
    hook_id: str,
    kind: EvidenceKind,
    verdict: Verdict = Verdict.PASSED,
    *,
    actor: str | None = None,
    summary: str | None = None,
    **extra: object,
) -> EvidenceClaim:
    """A well-formed claim of `kind`, from whichever producer that kind allows."""
    producer = sole_producer(kind)
    return EvidenceClaim(
        hook_id=hook_id,
        kind=kind,
        verdict=verdict,
        producer=producer,
        actor=ACTORS[producer] if actor is None else actor,
        summary=summary or f"{kind.value}: {verdict.value}",
        **extra,  # type: ignore[arg-type]
    )


def as_bytes(value: object) -> bytes:
    """Canonical bytes for an exact comparison, not a dict-equality one."""
    return json.dumps(value, sort_keys=True).encode("utf-8")


class LedgerTestCase(unittest.TestCase):
    """A ledger, the two subject shapes, and a one-line way to satisfy a hook."""

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)

    def mechanical(self, hook_id: str = "hook.mech") -> Subject:
        return Subject(hook_id, "mechanical", descriptor="LX/05t2;")

    def agent(self, hook_id: str = "hook.agent", proposed_by: str = PROPOSER) -> Subject:
        return Subject(hook_id, "agent", descriptor="LX/05t2;", proposed_by=proposed_by)

    def ledger(self, *subjects: Subject, path: Path | None = None) -> EvidenceLedger:
        ledger = EvidenceLedger(path)
        for subject in subjects:
            ledger.register(subject)
        return ledger

    def satisfy(
        self,
        ledger: EvidenceLedger,
        subject: Subject,
        *,
        skip: frozenset[EvidenceKind] = frozenset(),
        confidence: float | None = None,
    ) -> EvidenceLedger:
        """Record one passing claim per required kind, from the right producer."""
        for kind in sorted(subject.required, key=lambda item: item.value):
            if kind in skip:
                continue
            claim = claim_for(subject.hook_id, kind)
            ledger.record(stamped(replace(claim, confidence=confidence), STAMP))
        return ledger


# ------------------------------------------------------------ absence of proof


class AbsentEvidenceTests(LedgerTestCase):
    """Zero claims must escalate, loudly and by name.

    This is the single most important behaviour in the module. A hook that has
    accumulated nothing has, by construction, had nothing go wrong — and reading
    that as success is how all three inert patches shipped.
    """

    def test_a_mechanical_hook_with_no_claims_is_not_ready(self):
        subject = self.mechanical()
        readiness = self.ledger(subject).readiness(subject.hook_id)
        self.assertIs(readiness.ready, False)

    def test_every_required_kind_of_an_unexercised_hook_reads_not_exercised(self):
        # Not "passed by default" and not absent from the report: each required
        # item is present in the statuses with an explicit not_exercised verdict,
        # so a gate sees the shape of what is missing rather than an empty list.
        subject = self.mechanical()
        readiness = self.ledger(subject).readiness(subject.hook_id)
        self.assertEqual(len(readiness.statuses), len(MECHANICAL_REQUIREMENTS))
        for status in readiness.statuses:
            with self.subTest(kind=status.kind.value):
                self.assertIs(status.verdict, Verdict.NOT_EXERCISED)
                self.assertIs(status.satisfied, False)
                self.assertEqual(status.attempts, 0)
                self.assertIsNone(status.claim)
        self.assertEqual(set(readiness.missing), set(MECHANICAL_REQUIREMENTS))

    def test_an_agent_hook_with_no_claims_reports_all_seven_kinds_missing(self):
        subject = self.agent()
        readiness = self.ledger(subject).readiness(subject.hook_id)
        self.assertIs(readiness.ready, False)
        self.assertEqual(set(readiness.missing), set(AGENT_REQUIREMENTS))

    def test_reasons_name_every_missing_kind(self):
        subject = self.agent()
        readiness = self.ledger(subject).readiness(subject.hook_id)
        self.assertEqual(len(readiness.reasons), len(AGENT_REQUIREMENTS))
        for kind in AGENT_REQUIREMENTS:
            with self.subTest(kind=kind.value):
                self.assertTrue(
                    any(reason.startswith(f"{kind.value}:") for reason in readiness.reasons),
                    f"no reason names {kind.value}",
                )

    def test_each_missing_reason_quotes_what_that_kind_catches(self):
        """A human at a gate must not have to already know why an item is listed.

        The escalation is the only thing they see. If it says `runtime_probe:
        missing` and nothing else, the cheapest response is to waive it; if it
        says what a runtime probe catches — inert patches, the 430 settings hook
        — the cost of waiving it is visible.
        """
        subject = self.agent()
        readiness = self.ledger(subject).readiness(subject.hook_id)
        joined = "\n".join(readiness.reasons)
        for kind in AGENT_REQUIREMENTS:
            with self.subTest(kind=kind.value):
                self.assertIn(CATCHES[kind], joined)

    def test_the_catches_text_also_reaches_the_serialised_status(self):
        # `reasons` is prose for a human; `statuses` is what a UI renders. Both
        # carry the explanation so neither path loses it.
        subject = self.mechanical()
        readiness = self.ledger(subject).readiness(subject.hook_id)
        for status in readiness.statuses:
            with self.subTest(kind=status.kind.value):
                self.assertEqual(status.to_dict()["catches"], CATCHES[status.kind])
                self.assertEqual(status.to_dict()["summary"], "no claim recorded")
                self.assertIsNone(status.to_dict()["actor"])

    def test_every_kind_has_a_catches_entry(self):
        # `readiness` indexes CATCHES unconditionally; a kind added without one
        # would raise KeyError at the gate, which is the worst possible moment.
        self.assertEqual(set(CATCHES), set(EvidenceKind))
        for kind in EvidenceKind:
            with self.subTest(kind=kind.value):
                self.assertTrue(CATCHES[kind].strip())

    def test_partial_evidence_still_escalates_on_what_is_absent(self):
        # Real runs are partial. Two of seven present must not read as progress
        # towards ready; it is still "not ready", naming the other five.
        subject = self.agent()
        ledger = self.ledger(subject)
        ledger.record(claim_for(subject.hook_id, EvidenceKind.ANCHOR_UNIQUE))
        ledger.record(claim_for(subject.hook_id, EvidenceKind.REGISTERS_SAFE))
        readiness = ledger.readiness(subject.hook_id)
        self.assertIs(readiness.ready, False)
        self.assertEqual(
            set(readiness.missing),
            set(AGENT_REQUIREMENTS) - {EvidenceKind.ANCHOR_UNIQUE, EvidenceKind.REGISTERS_SAFE},
        )

    def test_an_unregistered_hook_raises_rather_than_reporting_ready(self):
        # `all([])` is True, so a readiness that answered about unknown hooks
        # would report the emptiest possible hook as the readiest.
        with self.assertRaises(EvidenceError) as caught:
            EvidenceLedger().readiness("hook.nobody-registered")
        self.assertIn("hook.nobody-registered", str(caught.exception))

    def test_a_claim_about_an_unregistered_hook_is_refused(self):
        with self.assertRaises(EvidenceError) as caught:
            EvidenceLedger().record(claim_for("hook.ghost", EvidenceKind.ANCHOR_UNIQUE))
        self.assertIn("not registered", str(caught.exception))


# ---------------------------------------------------------- self-attestation


class SelfAttestationTests(LedgerTestCase):
    """A proposer may not produce its own corroboration."""

    def test_recording_a_claim_by_the_proposer_raises(self):
        subject = self.agent()
        ledger = self.ledger(subject)
        claim = claim_for(subject.hook_id, EvidenceKind.ANCHOR_UNIQUE, actor=PROPOSER)
        with self.assertRaises(EvidenceError) as caught:
            ledger.record(claim)
        message = str(caught.exception)
        self.assertIn(PROPOSER, message)
        self.assertIn("may not also produce its evidence", message)

    def test_the_refused_claim_is_not_appended(self):
        # A guard that raised after appending would leave the claim readable via
        # `claims_for`, and readiness would count it.
        subject = self.agent()
        ledger = self.ledger(subject)
        with self.assertRaises(EvidenceError):
            ledger.record(
                claim_for(subject.hook_id, EvidenceKind.ANCHOR_UNIQUE, actor=PROPOSER)
            )
        self.assertEqual(ledger.claims, ())
        self.assertEqual(ledger.claims_for(subject.hook_id), ())

    def test_every_kind_is_refused_when_the_proposer_is_the_actor(self):
        # The check is on the ledger, not on any one builder, so it must hold
        # for all seven kinds regardless of which producer class they name.
        subject = self.agent()
        ledger = self.ledger(subject)
        for kind in sorted(AGENT_REQUIREMENTS, key=lambda item: item.value):
            with self.subTest(kind=kind.value):
                with self.assertRaises(EvidenceError):
                    ledger.record(claim_for(subject.hook_id, kind, actor=PROPOSER))

    def test_a_different_actor_is_accepted(self):
        # The guard must discriminate, not simply refuse: an independent checker
        # producing the same evidence is exactly what the ledger wants.
        subject = self.agent()
        ledger = self.ledger(subject)
        claim = claim_for(
            subject.hook_id, EvidenceKind.ANCHOR_UNIQUE, actor="verify.deterministic_checks"
        )
        recorded = ledger.record(claim)
        self.assertEqual(recorded, claim)
        self.assertEqual(ledger.claims, (claim,))

    def test_an_actor_that_merely_resembles_the_proposer_is_accepted(self):
        # `agent:resolver-2` is a different agent from `agent:resolver-1`; the
        # guard is identity, not family resemblance.
        subject = self.agent()
        ledger = self.ledger(subject)
        claim = claim_for(subject.hook_id, EvidenceKind.ANCHOR_UNIQUE, actor="agent:resolver-2")
        self.assertEqual(ledger.record(claim), claim)

    def test_a_claim_with_no_actor_cannot_be_built_at_all(self):
        """The empty-actor case is closed at construction, before the ledger sees it.

        A mechanical subject has `proposed_by == ""`, so an empty actor would
        compare equal to it. That comparison never happens, because a claim
        without an identifiable producer is rejected by `EvidenceClaim` itself —
        evidence nobody produced cannot be checked against anybody.
        """
        for actor in ("", "   ", "\t\n"):
            with self.subTest(actor=repr(actor)):
                with self.assertRaises(EvidenceError) as caught:
                    claim_for("hook.mech", EvidenceKind.ANCHOR_UNIQUE, actor=actor)
                self.assertIn("needs an actor", str(caught.exception))

    def test_a_mechanical_subject_does_not_match_an_empty_actor(self):
        """The `proposed_by and ...` short-circuit, proved on a forced bad claim.

        `EvidenceClaim` refuses an empty actor, so the only way to reach the
        ledger's comparison with one is to construct the object and then break
        it — which is what a corrupted record or a future refactor would do. The
        outcome must be sane: a mechanically resolved hook has no proposer, so
        "" is not its proposer and the claim is recorded normally rather than
        being rejected as self-attestation for a hook nobody proposed.
        """
        subject = self.mechanical()
        ledger = self.ledger(subject)
        claim = claim_for(subject.hook_id, EvidenceKind.ANCHOR_UNIQUE)
        self.assertEqual(subject.proposed_by, "")
        object.__setattr__(claim, "actor", "")  # bypasses __post_init__ deliberately
        self.assertEqual(claim.actor, subject.proposed_by)

        recorded = ledger.record(claim)  # must not raise "proposed this hook"
        self.assertIs(recorded, claim)
        self.assertEqual(ledger.claims_for(subject.hook_id), (claim,))

    def test_a_mechanical_subject_accepts_any_actor_it_is_given(self):
        # The other direction of the same short-circuit: with no proposer to
        # collide with, no actor string is ever self-attestation.
        subject = self.mechanical()
        ledger = self.ledger(subject)
        for actor in ("verify.deterministic_checks", PROPOSER, "mechanical", "0"):
            with self.subTest(actor=actor):
                ledger.record(
                    claim_for(subject.hook_id, EvidenceKind.ANCHOR_UNIQUE, actor=actor)
                )
        self.assertEqual(len(ledger.claims), 4)


# ---------------------------------------------------------- producer taxonomy


class ProducerTaxonomyTests(LedgerTestCase):
    """Who may produce each kind, checked over the whole matrix."""

    def test_every_kind_has_an_allowed_producer_set(self):
        self.assertEqual(set(ALLOWED_PRODUCERS), set(EvidenceKind))
        for kind, allowed in ALLOWED_PRODUCERS.items():
            with self.subTest(kind=kind.value):
                self.assertTrue(allowed)
                self.assertTrue(allowed <= set(Producer))

    def test_no_kind_admits_a_human_as_a_producer(self):
        # A human is never independent evidence. The only human contribution the
        # schema takes is a waiver, which is verdict-shaped, not producer-shaped.
        for kind, allowed in ALLOWED_PRODUCERS.items():
            with self.subTest(kind=kind.value):
                self.assertNotIn(Producer.HUMAN, allowed)

    def test_every_disallowed_producer_of_every_kind_raises(self):
        """The full 7x5 matrix, not a spot check.

        A taxonomy enforced for four of seven kinds is not a taxonomy: the one
        unguarded kind is where a proposer's own output lands.
        """
        for kind in sorted(EvidenceKind, key=lambda item: item.value):
            for producer in sorted(Producer, key=lambda item: item.value):
                if producer in ALLOWED_PRODUCERS[kind]:
                    continue
                with self.subTest(kind=kind.value, producer=producer.value):
                    with self.assertRaises(EvidenceError) as caught:
                        EvidenceClaim(
                            hook_id="hook.matrix",
                            kind=kind,
                            verdict=Verdict.PASSED,
                            producer=producer,
                            actor=ACTORS[producer],
                            summary="produced by the wrong class of thing",
                        )
                    message = str(caught.exception)
                    self.assertIn(producer.value, message)
                    self.assertIn(kind.value, message)
                    self.assertIn("may not produce", message)

    def test_every_allowed_producer_of_every_kind_is_accepted(self):
        # The complement of the matrix above: the guard must be a filter, not a
        # wall. If this and the previous test both pass, the boundary is exact.
        for kind in sorted(EvidenceKind, key=lambda item: item.value):
            for producer in sorted(ALLOWED_PRODUCERS[kind], key=lambda item: item.value):
                with self.subTest(kind=kind.value, producer=producer.value):
                    claim = EvidenceClaim(
                        hook_id="hook.matrix",
                        kind=kind,
                        verdict=Verdict.PASSED,
                        producer=producer,
                        actor=ACTORS[producer],
                        summary="produced by the right class of thing",
                    )
                    self.assertIs(claim.producer, producer)

    def test_the_error_names_the_producers_that_would_have_been_allowed(self):
        with self.assertRaises(EvidenceError) as caught:
            EvidenceClaim(
                hook_id="hook.matrix",
                kind=EvidenceKind.RUNTIME_PROBE,
                verdict=Verdict.PASSED,
                producer=Producer.DETERMINISTIC,
                actor="verify.deterministic_checks",
                summary="static check pretending to be a device",
            )
        self.assertIn("allowed: device", str(caught.exception))

    def test_a_static_checker_cannot_stand_in_for_the_device(self):
        """The 430 settings hook in one assertion.

        It passed every deterministic check and was dead on the phone. If a
        DETERMINISTIC producer could file `runtime_probe`, the ledger would have
        recorded the static pass as runtime proof and shipped it again.
        """
        for kind in (EvidenceKind.RUNTIME_PROBE, EvidenceKind.DIFFERENTIAL):
            with self.subTest(kind=kind.value):
                with self.assertRaises(EvidenceError):
                    EvidenceClaim(
                        hook_id="hook.430.settings",
                        kind=kind,
                        verdict=Verdict.PASSED,
                        producer=Producer.DETERMINISTIC,
                        actor="verify.deterministic_checks",
                        summary="every static assertion held",
                    )

    def test_an_agent_cannot_file_its_own_statistical_agreement(self):
        with self.assertRaises(EvidenceError):
            EvidenceClaim(
                hook_id="hook.agent",
                kind=EvidenceKind.PROPOSER_AGREEMENT,
                verdict=Verdict.PASSED,
                producer=Producer.VERIFIER_AGENT,
                actor="agent:holdout-verifier",
                summary="we all agreed",
            )

    def test_a_claim_needs_a_hook_id_and_a_summary(self):
        with self.assertRaises(EvidenceError) as caught:
            claim_for("   ", EvidenceKind.ANCHOR_UNIQUE)
        self.assertIn("hook_id", str(caught.exception))
        with self.assertRaises(EvidenceError) as caught:
            claim_for("hook.x", EvidenceKind.ANCHOR_UNIQUE, summary="  ")
        self.assertIn("summary", str(caught.exception))

    def test_confidence_outside_zero_to_one_is_rejected(self):
        for value in (-0.1, 1.1, 42.0):
            with self.subTest(confidence=value):
                with self.assertRaises(EvidenceError) as caught:
                    claim_for("hook.x", EvidenceKind.ANCHOR_UNIQUE, confidence=value)
                self.assertIn("within 0..1", str(caught.exception))


# --------------------------------------------------------------- human waivers


class HumanWaiverTests(LedgerTestCase):
    """A human may decide to proceed without an item. That is not the same as proof."""

    def test_a_human_may_not_attest_that_a_kind_passed(self):
        """Recording `passed` by hand would erase the ledger's whole distinction.

        `passed` means the phone showed it, or a checker re-derived it. If a
        human could write `passed`, the report could no longer tell "the device
        confirmed it" from "someone said so", which is the one difference every
        gate decision rests on.
        """
        for kind in sorted(EvidenceKind, key=lambda item: item.value):
            with self.subTest(kind=kind.value):
                with self.assertRaises(EvidenceError) as caught:
                    EvidenceClaim(
                        hook_id="hook.gate",
                        kind=kind,
                        verdict=Verdict.PASSED,
                        producer=Producer.HUMAN,
                        actor=ACTORS[Producer.HUMAN],
                        summary="I checked it myself",
                    )
                self.assertIn("may not attest to it as `passed`", str(caught.exception))

    def test_a_human_may_not_record_any_non_waiver_verdict(self):
        # The exemption is verdict-specific: `waived` and nothing else.
        for verdict in Verdict:
            if verdict is Verdict.WAIVED:
                continue
            with self.subTest(verdict=verdict.value):
                with self.assertRaises(EvidenceError):
                    EvidenceClaim(
                        hook_id="hook.gate",
                        kind=EvidenceKind.RUNTIME_PROBE,
                        verdict=verdict,
                        producer=Producer.HUMAN,
                        actor=ACTORS[Producer.HUMAN],
                        summary="a human opinion about the device",
                    )

    def test_a_human_waiver_with_a_decision_and_a_rationale_is_accepted(self):
        claim = waiver(
            "hook.gate",
            EvidenceKind.RUNTIME_PROBE,
            decision_id="GATE-2026-08-01-7",
            actor=ACTORS[Producer.HUMAN],
            rationale="no device pool this cycle; hook is behind a default-off toggle",
        )
        self.assertIs(claim.verdict, Verdict.WAIVED)
        self.assertIs(claim.producer, Producer.HUMAN)
        self.assertEqual(claim.decision_id, "GATE-2026-08-01-7")
        self.assertIn("no device pool", claim.summary)

    def test_a_human_may_waive_every_kind(self):
        for kind in sorted(EvidenceKind, key=lambda item: item.value):
            with self.subTest(kind=kind.value):
                claim = waiver(kind=kind, hook_id="hook.gate", decision_id="G-1",
                               actor=ACTORS[Producer.HUMAN], rationale="documented")
                self.assertIs(claim.verdict, Verdict.WAIVED)

    def test_a_waiver_without_a_decision_id_is_refused(self):
        """An undocumented waiver is indistinguishable from a hook that skipped.

        The decision id is what makes the waiver auditable back to a person and
        a moment; without it, "waived" is just a nicer word for "missing".
        """
        for decision_id in (None, "", "   "):
            with self.subTest(decision_id=repr(decision_id)):
                with self.assertRaises(EvidenceError) as caught:
                    EvidenceClaim(
                        hook_id="hook.gate",
                        kind=EvidenceKind.RUNTIME_PROBE,
                        verdict=Verdict.WAIVED,
                        producer=Producer.HUMAN,
                        actor=ACTORS[Producer.HUMAN],
                        summary="waived",
                        decision_id=decision_id,
                        rationale="device pool down",
                    )
                self.assertIn("needs a decision_id", str(caught.exception))

    def test_a_waiver_without_a_rationale_is_refused(self):
        for rationale in ("", "   "):
            with self.subTest(rationale=repr(rationale)):
                with self.assertRaises(EvidenceError) as caught:
                    EvidenceClaim(
                        hook_id="hook.gate",
                        kind=EvidenceKind.RUNTIME_PROBE,
                        verdict=Verdict.WAIVED,
                        producer=Producer.HUMAN,
                        actor=ACTORS[Producer.HUMAN],
                        summary="waived",
                        decision_id="G-1",
                        rationale=rationale,
                    )
                self.assertIn("needs a rationale", str(caught.exception))

    def test_a_non_human_producer_may_not_waive(self):
        """An agent waiving its own missing proof is the exact failure mode.

        Everything else in the ledger is checkable; a waiver is the one verdict
        that asserts nothing was measured. Letting a machine issue one hands the
        pipeline a way to certify itself.
        """
        for producer in sorted(Producer, key=lambda item: item.value):
            if producer is Producer.HUMAN:
                continue
            kind = next(
                item for item in EvidenceKind if producer in ALLOWED_PRODUCERS[item]
            )
            with self.subTest(producer=producer.value, kind=kind.value):
                with self.assertRaises(EvidenceError) as caught:
                    EvidenceClaim(
                        hook_id="hook.gate",
                        kind=kind,
                        verdict=Verdict.WAIVED,
                        producer=producer,
                        actor=ACTORS[producer],
                        summary="waiving myself",
                        decision_id="G-1",
                        rationale="I decided this was fine",
                    )
                message = str(caught.exception)
                self.assertIn("only a human may waive", message)
                self.assertIn(producer.value, message)

    def test_a_waived_kind_satisfies_its_requirement(self):
        # A waiver is a decision to proceed, so it must actually let the hook
        # through — otherwise gates would route around the ledger entirely.
        subject = self.mechanical()
        ledger = self.ledger(subject)
        self.satisfy(ledger, subject, skip=frozenset({EvidenceKind.RUNTIME_PROBE}))
        ledger.record(
            waiver(
                subject.hook_id,
                EvidenceKind.RUNTIME_PROBE,
                decision_id="G-1",
                actor=ACTORS[Producer.HUMAN],
                rationale="hook is compile-time only on this build",
            )
        )
        readiness = ledger.readiness(subject.hook_id)
        self.assertIs(readiness.ready, True)
        self.assertEqual(readiness.reasons, ())

    def test_a_waiver_still_reads_as_waived_and_not_as_passed(self):
        # It lets the hook advance, but the report must never launder it into a
        # pass: a reader has to be able to count how much was decided vs proved.
        subject = self.mechanical()
        ledger = self.ledger(subject)
        ledger.record(
            waiver(
                subject.hook_id,
                EvidenceKind.DIFFERENTIAL,
                decision_id="G-1",
                actor=ACTORS[Producer.HUMAN],
                rationale="no prior version to differ from",
            )
        )
        status = next(
            item
            for item in ledger.readiness(subject.hook_id).statuses
            if item.kind is EvidenceKind.DIFFERENTIAL
        )
        self.assertEqual(status.to_dict()["verdict"], "waived")
        self.assertIs(status.satisfied, True)
        self.assertEqual(status.to_dict()["actor"], ACTORS[Producer.HUMAN])

    def test_only_passed_and_waived_satisfy(self):
        satisfying = {verdict for verdict in Verdict if verdict.satisfies}
        self.assertEqual(satisfying, {Verdict.PASSED, Verdict.WAIVED})


# ------------------------------------------------------------------ confidence


class ConfidenceIsNeverReadTests(LedgerTestCase):
    """Varying confidence over its whole range must not move a single verdict.

    This is the direct answer to the failure mode. All three inert patches were
    proposed confidently; a pipeline that weighted self-reported certainty would
    have ranked them highest. The field is kept because it is useful to a human
    at a gate, and it is load-bearing for nothing.
    """

    CONFIDENCES = (0.0, 0.5, 1.0, None)

    # The hook ids deliberately avoid the word "confidence": one test greps the
    # serialised readiness for it, and a fixture name would satisfy the grep.
    def ready_hook(self, confidence: float | None) -> Readiness:
        subject = self.mechanical("hook.green")
        ledger = self.ledger(subject)
        self.satisfy(ledger, subject, confidence=confidence)
        return ledger.readiness(subject.hook_id)

    def escalating_hook(self, confidence: float | None) -> Readiness:
        """A hook that ran everything and failed two items.

        Every required kind carries a claim, deliberately: if some kinds were
        simply absent, a `readiness` that *did* weight confidence would still
        report not-ready for the missing ones and this fixture would pass a
        confidence-reading implementation. Here the only thing standing between
        the hook and ready is two unsatisfying verdicts, both of which carry
        whatever confidence is under test.
        """
        subject = self.agent("hook.amber")
        ledger = self.ledger(subject)
        unsatisfying = {
            EvidenceKind.REGISTERS_SAFE: Verdict.FAILED,
            EvidenceKind.RUNTIME_PROBE: Verdict.INCONCLUSIVE,
        }
        for kind in sorted(subject.required, key=lambda item: item.value):
            claim = claim_for(subject.hook_id, kind, unsatisfying.get(kind, Verdict.PASSED))
            ledger.record(stamped(replace(claim, confidence=confidence), STAMP))
        return ledger.readiness(subject.hook_id)

    def test_the_baseline_ready_hook_is_actually_ready(self):
        # Guards the test below: comparing four identical `False`s would prove
        # nothing about whether confidence is read.
        self.assertIs(self.ready_hook(None).ready, True)

    def test_the_baseline_escalating_hook_actually_escalates(self):
        readiness = self.escalating_hook(None)
        self.assertIs(readiness.ready, False)
        self.assertTrue(readiness.reasons)
        # Nothing is merely absent: every kind was claimed, so the two
        # unsatisfying verdicts are the only reason it is not ready.
        self.assertEqual(readiness.missing, ())
        self.assertEqual(
            {status.kind for status in readiness.statuses if not status.satisfied},
            {EvidenceKind.REGISTERS_SAFE, EvidenceKind.RUNTIME_PROBE},
        )

    def test_a_ready_hook_serialises_identically_at_every_confidence(self):
        baseline = as_bytes(self.ready_hook(None).to_dict())
        for confidence in self.CONFIDENCES:
            with self.subTest(confidence=confidence):
                self.assertEqual(as_bytes(self.ready_hook(confidence).to_dict()), baseline)

    def test_an_escalating_hook_serialises_identically_at_every_confidence(self):
        baseline = as_bytes(self.escalating_hook(None).to_dict())
        for confidence in self.CONFIDENCES:
            with self.subTest(confidence=confidence):
                self.assertEqual(
                    as_bytes(self.escalating_hook(confidence).to_dict()), baseline
                )

    def test_maximum_confidence_does_not_rescue_a_failing_hook(self):
        """The blunt version: total certainty on every claim, still not ready.

        All three inert patches would have scored 1.0 here. The failed and the
        inconclusive item are the only blockers, so an implementation that let a
        high enough confidence stand in for evidence would flip this to ready.
        """
        readiness = self.escalating_hook(1.0)
        self.assertIs(readiness.ready, False)
        self.assertEqual(
            {status.kind for status in readiness.statuses if not status.satisfied},
            {EvidenceKind.REGISTERS_SAFE, EvidenceKind.RUNTIME_PROBE},
        )

    def test_zero_confidence_does_not_sink_a_passing_hook(self):
        # And the other direction, so the test cannot pass by ignoring the flag
        # in one place and honouring it in another.
        readiness = self.ready_hook(0.0)
        self.assertIs(readiness.ready, True)

    def test_confidence_is_recorded_even_though_it_is_never_read(self):
        # Deleting the field would lose signal for a human, so it must survive
        # the round trip; it just must not reach `readiness`.
        claim = claim_for("hook.x", EvidenceKind.ANCHOR_UNIQUE, confidence=0.25)
        self.assertEqual(claim.to_dict()["confidence"], 0.25)
        self.assertEqual(EvidenceClaim.from_dict(claim.to_dict()).confidence, 0.25)

    def test_the_readiness_report_never_mentions_confidence(self):
        # A field absent from the serialised readiness cannot be weighted by a
        # downstream consumer either.
        readiness = self.ready_hook(1.0)
        self.assertNotIn("confidence", json.dumps(readiness.to_dict()))
        for status in readiness.statuses:
            with self.subTest(kind=status.kind.value):
                self.assertNotIn("confidence", status.to_dict())


# ------------------------------------------------------------- requirement sets


class RequirementSetTests(LedgerTestCase):
    """What each provenance owes, and what an unrecognised one owes."""

    def test_mechanical_requires_exactly_five_kinds(self):
        self.assertEqual(len(MECHANICAL_REQUIREMENTS), 5)
        self.assertEqual(requirements_for("mechanical"), MECHANICAL_REQUIREMENTS)

    def test_mechanical_does_not_require_the_two_agent_only_kinds(self):
        """There is no proposer to refute and no proposal to measure agreement on.

        The deterministic engine either matched exactly one site or refused, and
        re-running it reproduces the answer. Demanding an adversarial verifier
        of it would be theatre, and theatre is what gets waived first.
        """
        self.assertEqual(MECHANICAL_REQUIREMENTS & AGENT_ONLY, frozenset())
        self.assertEqual(AGENT_REQUIREMENTS - MECHANICAL_REQUIREMENTS, AGENT_ONLY)

    def test_agent_requires_all_seven_kinds(self):
        self.assertEqual(len(AGENT_REQUIREMENTS), 7)
        self.assertEqual(AGENT_REQUIREMENTS, frozenset(EvidenceKind))
        self.assertEqual(requirements_for("agent"), AGENT_REQUIREMENTS)

    def test_the_agent_set_is_a_strict_superset_of_the_mechanical_one(self):
        self.assertTrue(MECHANICAL_REQUIREMENTS < AGENT_REQUIREMENTS)

    def test_an_unknown_provenance_raises_rather_than_defaulting(self):
        """Falling back to the smaller set is the dangerous default.

        A typo in a provenance string would silently drop adversarial
        verification and proposer agreement from an agent-proposed hook — the
        two items that exist specifically because a proposal is the one thing
        nothing else re-derives.
        """
        for provenance in ("", "Mechanical", "AGENT", "agent ", "automatic", "unknown"):
            with self.subTest(provenance=repr(provenance)):
                with self.assertRaises(EvidenceError) as caught:
                    requirements_for(provenance)
                message = str(caught.exception)
                self.assertIn(repr(provenance), message)
                self.assertIn("must not silently pick the smaller requirement set", message)

    def test_the_requirement_sets_are_frozen(self):
        # A caller mutating the module-level set would change what every later
        # hook in the process owes.
        for name, value in (
            ("mechanical", MECHANICAL_REQUIREMENTS),
            ("agent", AGENT_REQUIREMENTS),
        ):
            with self.subTest(provenance=name):
                self.assertIsInstance(value, frozenset)
                self.assertFalse(hasattr(value, "add"))

    def test_a_subject_exposes_the_set_its_provenance_owes(self):
        self.assertEqual(self.mechanical().required, MECHANICAL_REQUIREMENTS)
        self.assertEqual(self.agent().required, AGENT_REQUIREMENTS)


class SubjectProvenanceTests(LedgerTestCase):
    """A subject must state who proposed it — or state that nobody did."""

    def test_an_agent_subject_must_name_its_proposer(self):
        """Without a name, "produced by something other than the proposer" is unenforceable.

        An agent-resolved hook with an anonymous proposer would accept evidence
        from that same agent, because there is nothing to compare the actor to.
        """
        for proposed_by in ("", "   ", "\t"):
            with self.subTest(proposed_by=repr(proposed_by)):
                with self.assertRaises(EvidenceError) as caught:
                    Subject("hook.anon", "agent", proposed_by=proposed_by)
                self.assertIn("must name its proposer", str(caught.exception))

    def test_a_mechanical_subject_must_not_name_a_proposer(self):
        """A named proposer on a mechanical hook means one of the two is a lie.

        Either it was agent-resolved and is claiming the smaller requirement
        set, or the name is noise that the self-attestation check will start
        comparing actors against.
        """
        with self.assertRaises(EvidenceError) as caught:
            Subject("hook.mech", "mechanical", proposed_by=PROPOSER)
        message = str(caught.exception)
        self.assertIn("has no proposer", message)
        self.assertIn(PROPOSER, message)

    def test_both_well_formed_shapes_are_accepted(self):
        self.assertEqual(Subject("hook.mech", "mechanical").proposed_by, "")
        self.assertEqual(Subject("hook.agent", "agent", proposed_by=PROPOSER).proposed_by, PROPOSER)

    def test_a_subject_with_an_unknown_provenance_is_refused(self):
        with self.assertRaises(EvidenceError):
            Subject("hook.x", "handwritten", proposed_by=PROPOSER)

    def test_re_registering_a_hook_differently_is_refused(self):
        """Re-registration would silently change which evidence is required.

        Registering as `agent`, collecting five mechanical items, then
        re-registering as `mechanical` is a two-line way to drop the adversarial
        requirement after the fact.
        """
        ledger = self.ledger(self.agent("hook.x"))
        with self.assertRaises(EvidenceError) as caught:
            ledger.register(Subject("hook.x", "mechanical"))
        self.assertIn("already registered", str(caught.exception))

    def test_re_registering_an_identical_subject_is_harmless(self):
        # Idempotent registration keeps `load` and a re-run from fighting.
        ledger = self.ledger(self.agent("hook.x"))
        ledger.register(self.agent("hook.x"))
        self.assertEqual(len(ledger.report()["hooks"]), 1)


# ------------------------------------------------------------- retry to green


class RetryToGreenTests(LedgerTestCase):
    """"Run it again until it goes green" must be visible, not erased."""

    def failed_then_passed(self, hook_id: str = "hook.retry") -> EvidenceLedger:
        subject = self.mechanical(hook_id)
        ledger = self.ledger(subject)
        ledger.record(
            claim_for(hook_id, EvidenceKind.ANCHOR_UNIQUE, Verdict.FAILED,
                      summary="anchor matched two sites")
        )
        ledger.record(
            claim_for(hook_id, EvidenceKind.ANCHOR_UNIQUE, Verdict.PASSED,
                      summary="anchor matched one site")
        )
        return ledger

    def status_for(self, ledger: EvidenceLedger, hook_id: str, kind: EvidenceKind) -> KindStatus:
        return next(
            item for item in ledger.readiness(hook_id).statuses if item.kind is kind
        )

    def test_a_kind_that_went_green_after_a_failure_is_flagged(self):
        status = self.status_for(
            self.failed_then_passed(), "hook.retry", EvidenceKind.ANCHOR_UNIQUE
        )
        self.assertIs(status.recovered_from_failure, True)
        self.assertIs(status.verdict, Verdict.PASSED)

    def test_a_recovered_kind_is_not_satisfied_despite_its_latest_verdict(self):
        """The latest attempt is not the only attempt.

        `satisfied` deliberately disagrees with `verdict` here: the last run
        passed, and the hook still owes a human an explanation of why the first
        one did not.
        """
        status = self.status_for(
            self.failed_then_passed(), "hook.retry", EvidenceKind.ANCHOR_UNIQUE
        )
        self.assertIs(status.verdict.satisfies, True)
        self.assertIs(status.satisfied, False)

    def test_a_recovered_kind_keeps_the_whole_hook_out_of_ready(self):
        ledger = self.failed_then_passed()
        self.satisfy(
            ledger,
            self.mechanical("hook.retry"),
            skip=frozenset({EvidenceKind.ANCHOR_UNIQUE}),
        )
        readiness = ledger.readiness("hook.retry")
        self.assertIs(readiness.ready, False)
        # Every other kind is satisfied, so the flag is the only thing holding it.
        others = [item for item in readiness.statuses if item.kind is not EvidenceKind.ANCHOR_UNIQUE]
        self.assertTrue(all(item.satisfied for item in others))

    def test_the_reason_reports_how_many_attempts_it_took(self):
        # A gate needs the count to tell one retry from twenty.
        readiness = self.failed_then_passed().readiness("hook.retry")
        reason = next(item for item in readiness.reasons if item.startswith("anchor_unique:"))
        self.assertIn("2 attempts", reason)
        self.assertIn("only after a failure", reason)

    def test_the_attempt_count_is_carried_on_the_status(self):
        status = self.status_for(
            self.failed_then_passed(), "hook.retry", EvidenceKind.ANCHOR_UNIQUE
        )
        self.assertEqual(status.attempts, 2)
        self.assertEqual(status.to_dict()["attempts"], 2)

    def test_passed_then_passed_is_not_flagged(self):
        # Re-running a check that never failed is ordinary. Flagging it would
        # train readers to ignore the flag.
        subject = self.mechanical("hook.clean")
        ledger = self.ledger(subject)
        for _ in range(2):
            ledger.record(claim_for("hook.clean", EvidenceKind.ANCHOR_UNIQUE, Verdict.PASSED))
        status = self.status_for(ledger, "hook.clean", EvidenceKind.ANCHOR_UNIQUE)
        self.assertIs(status.recovered_from_failure, False)
        self.assertIs(status.satisfied, True)
        self.assertEqual(status.attempts, 2)

    def test_a_single_pass_is_not_flagged(self):
        subject = self.mechanical("hook.once")
        ledger = self.ledger(subject)
        ledger.record(claim_for("hook.once", EvidenceKind.ANCHOR_UNIQUE, Verdict.PASSED))
        status = self.status_for(ledger, "hook.once", EvidenceKind.ANCHOR_UNIQUE)
        self.assertIs(status.recovered_from_failure, False)
        self.assertEqual(status.attempts, 1)

    def test_failed_then_passed_then_passed_is_still_flagged(self):
        """The failure is in the history, not in the last two entries.

        Padding a green run with another green run is the obvious way to push a
        failure out of view; the check looks at the whole sequence.
        """
        ledger = self.failed_then_passed("hook.padded")
        ledger.record(claim_for("hook.padded", EvidenceKind.ANCHOR_UNIQUE, Verdict.PASSED))
        status = self.status_for(ledger, "hook.padded", EvidenceKind.ANCHOR_UNIQUE)
        self.assertIs(status.recovered_from_failure, True)
        self.assertIs(status.satisfied, False)
        self.assertEqual(status.attempts, 3)

    def test_a_waiver_after_a_failure_is_also_flagged(self):
        # `satisfies` covers `waived` too, so waiving a kind that already failed
        # must not quietly clear the retry flag.
        subject = self.mechanical("hook.waived-after-fail")
        ledger = self.ledger(subject)
        ledger.record(
            claim_for("hook.waived-after-fail", EvidenceKind.RUNTIME_PROBE, Verdict.FAILED)
        )
        ledger.record(
            waiver(
                "hook.waived-after-fail",
                EvidenceKind.RUNTIME_PROBE,
                decision_id="G-9",
                actor=ACTORS[Producer.HUMAN],
                rationale="known flake in the harness",
            )
        )
        status = self.status_for(ledger, "hook.waived-after-fail", EvidenceKind.RUNTIME_PROBE)
        self.assertIs(status.recovered_from_failure, True)
        self.assertIs(status.satisfied, False)

    def test_a_failure_that_never_recovered_reports_the_failure_not_the_retry(self):
        # Two failures is not a "recovery"; the reason must read as a failure so
        # the escalation says what went wrong rather than how often.
        subject = self.mechanical("hook.stuck")
        ledger = self.ledger(subject)
        for _ in range(2):
            ledger.record(
                claim_for("hook.stuck", EvidenceKind.ANCHOR_UNIQUE, Verdict.FAILED,
                          summary="anchor matched two sites")
            )
        status = self.status_for(ledger, "hook.stuck", EvidenceKind.ANCHOR_UNIQUE)
        self.assertIs(status.recovered_from_failure, False)
        self.assertIs(status.satisfied, False)
        reason = next(
            item
            for item in ledger.readiness("hook.stuck").reasons
            if item.startswith("anchor_unique:")
        )
        self.assertIn("failed", reason)
        self.assertIn("anchor matched two sites", reason)


# ------------------------------------------------------------------ probe logic


class ProbeClaimTests(unittest.TestCase):
    """A runtime probe is judged on the delta, never on the count."""

    def probe(self, enabled, disabled, *, required=True, note="") -> EvidenceClaim:
        return probe_claim(
            hook_id="hook.reels",
            surface="reels_tab",
            signal="throwIfBlocked",
            enabled_observations=enabled,
            disabled_observations=disabled,
            requires_two_directional_delta=required,
            actor=ACTORS[Producer.DEVICE],
            waiver_note=note,
        )

    def test_zero_in_both_directions_is_inconclusive(self):
        """The Reels case, exactly.

        `replaceReelsEndpoint` blanks the endpoint upstream of `throwIfBlocked`,
        so block-counting sees nothing whether the toggle is on or off. Reading
        that as "no blocks, nothing wrong" certifies an inert hook — the probe
        cannot see the hook, which is not the same as the hook working.
        """
        claim = self.probe(0, 0)
        self.assertIs(claim.verdict, Verdict.INCONCLUSIVE)
        self.assertIs(claim.verdict.satisfies, False)
        self.assertIn("cannot see this hook", claim.summary)
        self.assertIn("not a pass", claim.summary)

    def test_equal_non_zero_counts_are_also_inconclusive(self):
        # The signal is there in both directions, so the toggle changed nothing:
        # same conclusion, and for the same reason.
        for count in (1, 4, 100):
            with self.subTest(count=count):
                self.assertIs(self.probe(count, count).verdict, Verdict.INCONCLUSIVE)

    def test_more_signal_with_the_toggle_off_is_a_failure(self):
        claim = self.probe(1, 5)
        self.assertIs(claim.verdict, Verdict.FAILED)
        self.assertIn("wrong way", claim.summary)

    def test_signal_only_when_disabled_is_a_failure(self):
        self.assertIs(self.probe(0, 3).verdict, Verdict.FAILED)

    def test_more_signal_with_the_toggle_on_is_a_pass(self):
        claim = self.probe(5, 0)
        self.assertIs(claim.verdict, Verdict.PASSED)
        self.assertIn("two-directional delta", claim.summary)

    def test_a_pass_needs_only_a_delta_not_a_clean_zero_when_disabled(self):
        # Real devices are noisy; the rule is the direction of the difference.
        self.assertIs(self.probe(9, 2).verdict, Verdict.PASSED)

    def test_the_observations_are_recorded_for_the_gate(self):
        detail = self.probe(5, 1).detail
        self.assertEqual(detail["enabled_observations"], 5)
        self.assertEqual(detail["disabled_observations"], 1)
        self.assertEqual(detail["surface"], "reels_tab")
        self.assertEqual(detail["signal"], "throwIfBlocked")
        self.assertIs(detail["requires_two_directional_delta"], True)

    def test_a_probe_is_always_attributed_to_the_device(self):
        for enabled, disabled in ((0, 0), (0, 3), (3, 0)):
            with self.subTest(enabled=enabled, disabled=disabled):
                claim = self.probe(enabled, disabled)
                self.assertIs(claim.producer, Producer.DEVICE)
                self.assertIs(claim.kind, EvidenceKind.RUNTIME_PROBE)

    def test_waiving_the_delta_without_a_note_raises(self):
        """A silent waiver is how an inert hook passes verification.

        Some hooks genuinely are not toggleable, so the waiver has to exist. It
        just has to be written down, because a boolean nobody had to justify is
        the cheapest thing in the pipeline to flip.
        """
        for note in ("", "   ", "\n"):
            with self.subTest(note=repr(note)):
                with self.assertRaises(EvidenceError) as caught:
                    self.probe(3, 0, required=False, note=note)
                message = str(caught.exception)
                self.assertIn("must say why", message)
                self.assertIn("hook.reels", message)

    def test_a_noted_waiver_with_observations_passes(self):
        claim = self.probe(
            3, 0, required=False, note="startup hook; no toggle exists on this build"
        )
        self.assertIs(claim.verdict, Verdict.PASSED)
        self.assertIn("presence is the whole proof", claim.summary)
        self.assertEqual(
            claim.detail["waiver_note"], "startup hook; no toggle exists on this build"
        )

    def test_a_noted_waiver_with_no_observations_fails(self):
        """With the delta waived, presence is the only evidence left.

        Zero observations then means the probe saw nothing at all, which is a
        failure and not an inconclusive: there is no second direction left to
        blame it on.
        """
        claim = self.probe(0, 0, required=False, note="startup hook; no toggle exists")
        self.assertIs(claim.verdict, Verdict.FAILED)
        self.assertIs(claim.verdict.satisfies, False)

    def test_a_waived_probe_ignores_the_disabled_count_entirely(self):
        # Nothing was toggled, so whatever landed in the "disabled" bucket is
        # not evidence either way; only presence counts.
        for disabled in (0, 7):
            with self.subTest(disabled=disabled):
                claim = self.probe(2, disabled, required=False, note="not toggleable")
                self.assertIs(claim.verdict, Verdict.PASSED)

    def test_the_waiver_note_is_absent_from_a_required_delta_probe(self):
        self.assertNotIn("waiver_note", self.probe(3, 0).detail)

    def test_an_inconclusive_probe_does_not_make_a_hook_ready(self):
        # The end-to-end version: everything else green, probe unmeasurable.
        subject = Subject("hook.reels", "mechanical")
        ledger = EvidenceLedger()
        ledger.register(subject)
        for kind in sorted(MECHANICAL_REQUIREMENTS, key=lambda item: item.value):
            if kind is EvidenceKind.RUNTIME_PROBE:
                continue
            ledger.record(claim_for("hook.reels", kind))
        ledger.record(self.probe(0, 0))
        readiness = ledger.readiness("hook.reels")
        self.assertIs(readiness.ready, False)
        self.assertEqual(len(readiness.reasons), 1)
        self.assertIn("runtime_probe: inconclusive", readiness.reasons[0])


# --------------------------------------------------------------- agreement


class AgreementClaimTests(unittest.TestCase):
    """Agreement is measured over what proposers produced, not what they claim."""

    SITE = {"descriptor": "LX/05t2;", "anchor": ["iput-object {v0}, p0, LX/05t2;->A00:Z"]}
    OTHER = {"descriptor": "LX/04Pn;", "anchor": ["invoke-virtual {v1}, LX/04Pn;->run()V"]}
    THIRD = {"descriptor": "LX/0aOK;", "anchor": ["const-string v2, \"clips/discover/\""]}

    def test_no_proposals_is_not_exercised(self):
        """Nothing measured is not the same as nothing wrong.

        `not_exercised` keeps the empty case out of `satisfies`, so a stage that
        never ran agreement cannot contribute a green item to the ledger.
        """
        claim = agreement_claim("hook.x", [])
        self.assertIs(claim.verdict, Verdict.NOT_EXERCISED)
        self.assertIs(claim.verdict.satisfies, False)
        self.assertIn("no proposals", claim.summary)

    def test_unanimity_passes(self):
        claim = agreement_claim("hook.x", [self.SITE, dict(self.SITE), dict(self.SITE)])
        self.assertIs(claim.verdict, Verdict.PASSED)
        self.assertEqual(claim.detail["agreed"], 3)
        self.assertEqual(claim.detail["proposals"], 3)
        self.assertEqual(claim.detail["share"], 1.0)
        self.assertEqual(claim.detail["distinct_answers"], 1)

    def test_a_single_proposal_does_not_pass(self):
        """One proposer agreeing with itself is the self-attestation failure again.

        A share of 100% over a sample of one is arithmetic, not corroboration,
        so the code requires `best_count > 1` on top of the threshold.
        """
        claim = agreement_claim("hook.x", [self.SITE])
        self.assertIs(claim.verdict, Verdict.INCONCLUSIVE)
        self.assertIs(claim.verdict.satisfies, False)
        self.assertEqual(claim.detail["share"], 1.0)  # the share alone would have passed
        self.assertEqual(claim.detail["agreed"], 1)

    def test_a_plurality_below_the_threshold_is_inconclusive(self):
        """Genuine ambiguity must reach a human rather than be resolved by plurality."""
        proposals = [self.SITE, dict(self.SITE), self.OTHER, self.THIRD,
                     {"descriptor": "LX/9999;", "anchor": []}]
        claim = agreement_claim("hook.x", proposals)
        self.assertIs(claim.verdict, Verdict.INCONCLUSIVE)
        self.assertEqual(claim.detail["agreed"], 2)
        # The share is over everyone asked, including the one that did not answer:
        # two of five agreeing is weak corroboration however the others abstained.
        self.assertEqual(claim.detail["share"], 0.4)
        # Four proposals named a site; the fifth named a class but no anchor, so
        # it identified nowhere to inject and is not one of the distinct answers.
        self.assertEqual(claim.detail["answered"], 4)
        self.assertEqual(claim.detail["distinct_answers"], 3)

    def test_a_majority_at_the_threshold_passes(self):
        # Unanimity is not required: the holdout that justified this had two of
        # three proposers reach the hard settings site and the third fail.
        claim = agreement_claim("hook.x", [self.SITE, dict(self.SITE), self.OTHER])
        self.assertIs(claim.verdict, Verdict.PASSED)
        self.assertEqual(claim.detail["share"], 0.6667)

    def test_the_threshold_is_configurable_and_respected(self):
        proposals = [self.SITE, dict(self.SITE), self.OTHER]
        self.assertIs(agreement_claim("hook.x", proposals, threshold=0.9).verdict,
                      Verdict.INCONCLUSIVE)
        self.assertIs(agreement_claim("hook.x", proposals, threshold=0.6).verdict,
                      Verdict.PASSED)

    def test_agreement_is_insensitive_to_differing_prose(self):
        """Two proposers reaching the same site agree, however differently they argue.

        Comparing rationale text would make agreement a measure of writing style
        and would let two identical answers read as a disagreement — which then
        escalates a hook that nothing is actually wrong with, training gates to
        rubber-stamp.
        """
        verbose = dict(
            self.SITE,
            rationale="The iput at offset 0x1c is the only write to the boolean, and "
            "the surrounding block is the options row rather than the follow button.",
            evidence=["decompiled A00", "xref count 1", "layout id 0x7f0b0042"],
            confidence=0.42,
        )
        terse = dict(self.SITE, rationale="only write", evidence=[], confidence=0.99)
        claim = agreement_claim("hook.x", [verbose, terse])
        self.assertIs(claim.verdict, Verdict.PASSED)
        self.assertEqual(claim.detail["distinct_answers"], 1)
        self.assertEqual(claim.detail["agreed"], 2)

    def test_a_differing_anchor_is_a_disagreement_even_at_the_same_descriptor(self):
        # The half that makes the previous test meaningful: agreement is over
        # descriptor *and* anchor, because the same class holds many sites.
        same_class_other_site = dict(
            self.SITE, anchor=["iput-object {v3}, p0, LX/05t2;->A01:Z"]
        )
        claim = agreement_claim("hook.x", [self.SITE, same_class_other_site])
        self.assertIs(claim.verdict, Verdict.INCONCLUSIVE)
        self.assertEqual(claim.detail["distinct_answers"], 2)

    def test_a_differing_descriptor_is_a_disagreement_at_the_same_anchor(self):
        same_anchor_other_class = dict(self.SITE, descriptor="LX/04Pn;")
        claim = agreement_claim("hook.x", [self.SITE, same_anchor_other_class])
        self.assertIs(claim.verdict, Verdict.INCONCLUSIVE)
        self.assertEqual(claim.detail["distinct_answers"], 2)

    def test_anchor_order_is_significant(self):
        # An anchor is a sequence of lines; reordering it is a different anchor.
        first = {"descriptor": "LX/05t2;", "anchor": ["line a", "line b"]}
        second = {"descriptor": "LX/05t2;", "anchor": ["line b", "line a"]}
        self.assertEqual(agreement_claim("hook.x", [first, second]).detail["distinct_answers"], 2)

    def test_a_tuple_anchor_matches_an_equal_list_anchor(self):
        # Callers thread proposals through JSON and through dataclasses; the
        # container type must not decide whether two proposers agreed.
        as_tuple = {"descriptor": "LX/05t2;", "anchor": ("line a", "line b")}
        as_list = {"descriptor": "LX/05t2;", "anchor": ["line a", "line b"]}
        claim = agreement_claim("hook.x", [as_tuple, as_list])
        self.assertIs(claim.verdict, Verdict.PASSED)
        self.assertEqual(claim.detail["distinct_answers"], 1)

    def test_the_claim_is_always_statistical_and_never_from_an_agent(self):
        for proposals in ([], [self.SITE], [self.SITE, dict(self.SITE)]):
            with self.subTest(count=len(proposals)):
                claim = agreement_claim("hook.x", proposals)
                self.assertIs(claim.producer, Producer.STATISTICS)
                self.assertIs(claim.kind, EvidenceKind.PROPOSER_AGREEMENT)

    def test_the_winning_fingerprint_identifies_the_agreed_answer(self):
        # So a gate can tell which of several answers the majority reached,
        # rather than only how many reached it.
        one = agreement_claim("hook.x", [self.SITE, dict(self.SITE)])
        two = agreement_claim("hook.x", [self.OTHER, dict(self.OTHER)])
        self.assertNotEqual(
            one.detail["winning_fingerprint"], two.detail["winning_fingerprint"]
        )

    def test_the_summary_states_the_count_out_of_the_total(self):
        claim = agreement_claim("hook.x", [self.SITE, dict(self.SITE), self.OTHER])
        self.assertIn("2 of 3", claim.summary)


class AnswerShapeAgreementTests(LedgerTestCase):
    """Agreement is scored against the question that was actually asked.

    Two questions exist. `proposer.proposer_prompt` asks for a whole patch;
    `proposer.host_prompt` asks only which class, because the manifest owns the
    anchor and the payload and asking an agent to reinvent them manufactures the
    variance that reads as disagreement — measured on 439 as 2 of 3 proposers on
    the host and 1 of 3 once anchors and payloads were compared.

    Scoring a host answer by the whole-patch shape made every host agreement
    `not_exercised`, so the by-agent hooks stalled at the gate with the ledger
    reporting that nothing had been measured. These tests pin the widening AND
    the thing it must not cost: an answer that supplied nothing still does not
    count as agreeing, in either shape.
    """

    #: One host proposer's answer, in the shape `HostProposal.to_dict` writes.
    HOST = {
        "hook_id": "install_settings_long_click_actionbar",
        "proposer": "agent-a",
        "descriptor": "LX/06X7;",
        "smali_path": "smali_classes6/X/06X7.smali",
        "evidence": ["smali_classes6/X/06X7.smali:412"],
        "alternatives": [],
        "unresolved": [],
    }

    #: One whole-patch answer, for the half of each comparison that is unchanged.
    PATCH = {
        "proposer": "agent-a",
        "descriptor": "LX/05t2;",
        "anchor": ["iput-object {v0}, p0, LX/05t2;->A00:Z"],
    }

    def hosts(self, *descriptors: str) -> list[dict[str, object]]:
        """One host proposal per descriptor, each from a differently named agent."""
        return [
            dict(self.HOST, proposer=f"agent-{index}", descriptor=descriptor)
            for index, descriptor in enumerate(descriptors)
        ]

    def test_two_of_three_host_proposers_agreeing_produce_a_real_verdict(self):
        """The blocking case: a clean host agreement used to come back unmeasured.

        It failed safe — the hook stalled rather than shipped — which is exactly
        why nobody would notice it from a green run.
        """
        claim = agreement_claim(
            "hook.host", self.hosts("LX/06X7;", "LX/06X7;", "LX/0Di2;"), asked=HOST_ONLY
        )
        self.assertIs(claim.verdict, Verdict.PASSED)
        self.assertIsNot(claim.verdict, Verdict.NOT_EXERCISED)
        self.assertEqual(claim.detail["answered"], 3)
        self.assertEqual(claim.detail["agreed"], 2)
        self.assertEqual(claim.detail["share"], 0.6667)
        self.assertEqual(claim.detail["distinct_answers"], 2)
        self.assertIn("2 of 3", claim.summary)

    def test_the_agreed_host_actually_satisfies_the_ledger_item(self):
        # The point of the fix is not the verdict but what it unblocks: the
        # required `proposer_agreement` item goes green off a host agreement.
        subject = self.agent("hook.host.ledger")
        ledger = self.ledger(subject)
        self.satisfy(
            ledger, subject, skip=frozenset({EvidenceKind.PROPOSER_AGREEMENT})
        )
        ledger.record(
            agreement_claim(
                subject.hook_id, self.hosts("LX/06X7;", "LX/06X7;"), asked=HOST_ONLY
            )
        )
        self.assertIs(ledger.readiness(subject.hook_id).ready, True)

    def test_a_host_agreement_is_still_one_kind_of_proposer_agreement(self):
        """One kind, two answer shapes — the shape is data on the claim.

        Splitting `EvidenceKind` would make `AGENT_REQUIREMENTS`, which is every
        kind, demand both a host agreement and a whole-patch one from every
        agent-resolved hook. A hook resolved by host discovery has no whole-patch
        agreement to give and would stall forever: the same failure, rebuilt.
        """
        for asked in (FULL_PROPOSAL, HOST_ONLY):
            with self.subTest(asked=asked.name):
                claim = agreement_claim(
                    "hook.host", self.hosts("LX/06X7;", "LX/06X7;"), asked=asked
                )
                self.assertIs(claim.kind, EvidenceKind.PROPOSER_AGREEMENT)
                self.assertIs(claim.producer, Producer.STATISTICS)
                self.assertIn(claim.kind, AGENT_REQUIREMENTS)

    def test_the_claim_records_which_question_it_answers(self):
        """A gate must never be shown agreement about a class as agreement about a patch."""
        host = agreement_claim(
            "hook.host", self.hosts("LX/06X7;", "LX/06X7;"), asked=HOST_ONLY
        )
        patch = agreement_claim("hook.x", [dict(self.PATCH), dict(self.PATCH)])
        self.assertEqual(host.detail["asked"], "host")
        self.assertEqual(patch.detail["asked"], "full_proposal")
        # Including when nothing answered, which is the case a reader is most
        # likely to be trying to explain.
        self.assertEqual(
            agreement_claim("hook.host", [], asked=HOST_ONLY).detail["asked"], "host"
        )

    def test_the_default_question_still_demands_an_anchor(self):
        """Widening had to be something a call site says out loud.

        `assess` compares whole patches, and three proposers who agreed only on
        the class are exactly the 439 result that `assess` was right to refuse.
        If the host shape were the default, that refusal would silently become a
        pass.
        """
        claim = agreement_claim("hook.host", self.hosts("LX/06X7;", "LX/06X7;"))
        self.assertIs(claim.verdict, Verdict.NOT_EXERCISED)
        self.assertIn("both a host and an anchor", claim.summary)
        self.assertEqual(claim.detail["answered"], 0)

    def test_a_descriptor_with_an_empty_anchor_still_did_not_answer(self):
        # The original property, unchanged: naming a class is not identifying a
        # site to inject at, and a patch was what was asked for.
        for anchor in ([], (), None):
            with self.subTest(anchor=anchor):
                claim = agreement_claim(
                    "hook.x",
                    [
                        {"descriptor": "LX/05t2;", "anchor": anchor},
                        {"descriptor": "LX/05t2;", "anchor": anchor},
                    ],
                )
                self.assertIs(claim.verdict, Verdict.NOT_EXERCISED)

    def test_identity_is_over_the_asked_for_fields_and_no_others(self):
        """Two host answers naming one class agree, whatever else their dicts carry.

        Hashing a field the question did not ask about would split a genuine
        agreement — and the anchor is precisely the field the host question left
        out because it varies for reasons that are not disagreement.
        """
        pair = [
            dict(self.HOST, proposer="agent-a", anchor=["iput-object v13, v1, LX/09rb;->A0H:I"]),
            dict(self.HOST, proposer="agent-b", anchor=["const-string v2, \"options\"", "nop"]),
        ]
        as_host = agreement_claim("hook.host", pair, asked=HOST_ONLY)
        self.assertIs(as_host.verdict, Verdict.PASSED)
        self.assertEqual(as_host.detail["distinct_answers"], 1)

        # And the same two answers, judged as whole patches, are two answers.
        as_patch = agreement_claim("hook.host", pair)
        self.assertIs(as_patch.verdict, Verdict.INCONCLUSIVE)
        self.assertEqual(as_patch.detail["distinct_answers"], 2)

    def test_one_host_proposer_is_not_corroboration(self):
        # `best_count > 1` survives the widening: a share of 100% over a sample
        # of one is arithmetic, and one confidently wrong agent is the failure
        # this project has actually shipped.
        claim = agreement_claim("hook.host", self.hosts("LX/06X7;"), asked=HOST_ONLY)
        self.assertIs(claim.verdict, Verdict.INCONCLUSIVE)
        self.assertEqual(claim.detail["share"], 1.0)
        self.assertEqual(claim.detail["agreed"], 1)

    def test_a_host_plurality_below_the_threshold_still_escalates(self):
        # Two of five is the largest group and still weak corroboration; genuine
        # ambiguity belongs at a gate rather than being broken by ranking.
        claim = agreement_claim(
            "hook.host",
            self.hosts("LX/06X7;", "LX/06X7;", "LX/0Di2;", "LX/0Di3;", "LX/0Di4;"),
            asked=HOST_ONLY,
        )
        self.assertIs(claim.verdict, Verdict.INCONCLUSIVE)
        self.assertIs(claim.verdict.satisfies, False)
        self.assertEqual(claim.detail["share"], 0.4)
        self.assertEqual(claim.detail["agreed"], 2)

        # And the threshold is still the thing deciding it.
        self.assertIs(
            agreement_claim(
                "hook.host",
                self.hosts("LX/06X7;", "LX/06X7;", "LX/0Di2;", "LX/0Di3;", "LX/0Di4;"),
                threshold=0.4,
                asked=HOST_ONLY,
            ).verdict,
            Verdict.PASSED,
        )

    def test_the_keys_length_guard_fires_for_either_question(self):
        # A mismatched pairing tallies one proposer's answer under another's key,
        # so the recorded claim describes an agreement nobody reached.
        for asked in (FULL_PROPOSAL, HOST_ONLY):
            for keys in ([], ["a"], ["a", "b", "c"]):
                with self.subTest(asked=asked.name, keys=len(keys)):
                    with self.assertRaises(EvidenceError) as caught:
                        agreement_claim(
                            "hook.host",
                            self.hosts("LX/06X7;", "LX/06X7;"),
                            keys=keys,
                            asked=asked,
                        )
                    self.assertIn("agreement keys", str(caught.exception))

    def test_supplied_keys_still_decide_the_tally(self):
        # The `keys` seam is unchanged by the widening: what a proposal would DO
        # still beats the text it quoted, in either shape.
        split = agreement_claim(
            "hook.host",
            self.hosts("LX/06X7;", "LX/06X7;"),
            keys=["effect-1", "effect-2"],
            asked=HOST_ONLY,
        )
        self.assertIs(split.verdict, Verdict.INCONCLUSIVE)
        self.assertEqual(split.detail["distinct_answers"], 2)

    def test_two_proposers_that_named_no_class_cannot_out_vote_one_that_did(self):
        """ATTACK. Mutation: score a host answer as answered whatever it contains.

        This is the guard the widening most endangers, because the obvious way
        to let host proposals through is to stop asking for anything at all.
        Under that mutant the two agents that gave up hash to the same empty
        answer, out-vote the one that found the class, and the ledger records
        `passed` with `winning_fingerprint` naming the hash of nothing — a
        by-agent hook certified as corroborated by two proposers that failed.
        """
        gave_up = [
            {"proposer": "agent-a", "descriptor": None, "unresolved": ["no candidate"]},
            {"proposer": "agent-b", "descriptor": "   ", "unresolved": ["gave up"]},
        ]
        found = dict(self.HOST, proposer="agent-c")
        claim = agreement_claim("hook.host", [*gave_up, found], asked=HOST_ONLY)

        self.assertIsNot(claim.verdict, Verdict.PASSED)
        self.assertIs(claim.verdict.satisfies, False)
        self.assertEqual(claim.detail["answered"], 1)
        self.assertEqual(claim.detail["agreed"], 1)
        # The one real answer is what won, not the pair of empty ones.
        self.assertEqual(claim.detail["winning_fingerprint"], HOST_ONLY.identity(found))
        self.assertNotEqual(
            claim.detail["winning_fingerprint"], HOST_ONLY.identity(gave_up[0])
        )

        # Nothing but empty answers reports that nothing was measured.
        for empty in ([], [{}, {}], gave_up):
            with self.subTest(empty=len(empty)):
                self.assertIs(
                    agreement_claim("hook.host", empty, asked=HOST_ONLY).verdict,
                    Verdict.NOT_EXERCISED,
                )

        # And the ledger will not let that claim make a hook ready, with every
        # other required item already satisfied.
        subject = self.agent("hook.host.attack")
        ledger = self.ledger(subject)
        self.satisfy(ledger, subject, skip=frozenset({EvidenceKind.PROPOSER_AGREEMENT}))
        ledger.record(
            agreement_claim(subject.hook_id, [*gave_up, found], asked=HOST_ONLY)
        )
        readiness = ledger.readiness(subject.hook_id)
        self.assertIs(readiness.ready, False)
        blockers = [item.kind for item in readiness.statuses if not item.satisfied]
        self.assertEqual(blockers, [EvidenceKind.PROPOSER_AGREEMENT])

    def test_an_answer_shape_names_its_own_question(self):
        # The parameter exists so a call site reads as "this was a host
        # question", not as "skip a check". Both constants say which.
        self.assertEqual((FULL_PROPOSAL.name, FULL_PROPOSAL.fields),
                         ("full_proposal", ("descriptor", "anchor")))
        self.assertEqual((HOST_ONLY.name, HOST_ONLY.fields), ("host", ("descriptor",)))
        self.assertIsInstance(HOST_ONLY, AnswerShape)

    def test_a_shape_answers_only_when_every_asked_for_field_is_supplied(self):
        shape = AnswerShape("both", ("descriptor", "anchor"), "a host and an anchor")
        self.assertIs(shape.answered({"descriptor": "LX/05t2;", "anchor": ["x"]}), True)
        for missing in (
            {"descriptor": "LX/05t2;"},
            {"anchor": ["x"]},
            {"descriptor": "LX/05t2;", "anchor": []},
            {"descriptor": " ", "anchor": ["x"]},
            {"descriptor": None, "anchor": ["x"]},
            {},
        ):
            with self.subTest(missing=sorted(missing)):
                self.assertIs(shape.answered(missing), False)

    def test_a_tuple_field_and_an_equal_list_field_are_one_answer(self):
        # Callers thread proposals through JSON and through dataclasses; the
        # container type must not decide whether two proposers agreed.
        self.assertEqual(
            FULL_PROPOSAL.identity({"descriptor": "LX/05t2;", "anchor": ("a", "b")}),
            FULL_PROPOSAL.identity({"descriptor": "LX/05t2;", "anchor": ["a", "b"]}),
        )


# ----------------------------------------------------------------- persistence


class PersistenceTests(LedgerTestCase):
    """JSONL, append-only, re-validated on the way back in."""

    def setUp(self):
        super().setUp()
        self.path = self.tmp / "run" / "evidence.jsonl"
        self.subject = self.agent("hook.persist")

    def write_lines(self, *payloads: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("".join(f"{line}\n" for line in payloads), encoding="utf-8")

    def test_claims_round_trip_through_the_file(self):
        ledger = self.ledger(self.subject, path=self.path)
        self.satisfy(ledger, self.subject)
        reloaded = EvidenceLedger.load(self.path, [self.subject])
        self.assertEqual(reloaded.claims, ledger.claims)

    def test_a_round_tripped_ledger_reports_the_same_readiness(self):
        # The file is the record; a reload that disagreed with the live ledger
        # would mean the gate's answer depended on whether the run crashed.
        ledger = self.ledger(self.subject, path=self.path)
        self.satisfy(ledger, self.subject)
        reloaded = EvidenceLedger.load(self.path, [self.subject])
        self.assertEqual(
            as_bytes(reloaded.readiness(self.subject.hook_id).to_dict()),
            as_bytes(ledger.readiness(self.subject.hook_id).to_dict()),
        )
        self.assertIs(reloaded.readiness(self.subject.hook_id).ready, True)

    def test_every_field_survives_the_round_trip(self):
        ledger = self.ledger(self.subject, path=self.path)
        claim = stamped(
            claim_for(
                self.subject.hook_id,
                EvidenceKind.ANCHOR_UNIQUE,
                confidence=0.75,
                supersedes="a" * 64,
                detail={"matches": 1, "path": "smali_classes3/LX/05t2.smali"},
            ),
            STAMP,
        )
        ledger.record(claim)
        reloaded = EvidenceLedger.load(self.path, [self.subject])
        self.assertEqual(reloaded.claims[0], claim)
        self.assertEqual(reloaded.claims[0].claim_id, claim.claim_id)
        self.assertEqual(reloaded.claims[0].recorded_at, STAMP)

    def test_recording_twice_appends_two_lines(self):
        """Append-only, so a crashed run keeps every claim it had already earned.

        A writer that rewrote the file would also make superseding destructive,
        and the supersede chain is the only place the earlier answer survives.
        """
        ledger = self.ledger(self.subject, path=self.path)
        ledger.record(claim_for(self.subject.hook_id, EvidenceKind.ANCHOR_UNIQUE))
        first = self.path.read_text(encoding="utf-8")
        ledger.record(claim_for(self.subject.hook_id, EvidenceKind.REGISTERS_SAFE))
        second = self.path.read_text(encoding="utf-8")
        self.assertEqual(len(first.splitlines()), 1)
        self.assertEqual(len(second.splitlines()), 2)
        self.assertTrue(second.startswith(first))  # the first line was not touched

    def test_a_repeated_claim_of_the_same_kind_is_appended_not_replaced(self):
        # The retry flag depends on the earlier attempt still being on disk.
        ledger = self.ledger(self.subject, path=self.path)
        ledger.record(
            claim_for(self.subject.hook_id, EvidenceKind.ANCHOR_UNIQUE, Verdict.FAILED)
        )
        ledger.record(
            claim_for(self.subject.hook_id, EvidenceKind.ANCHOR_UNIQUE, Verdict.PASSED)
        )
        self.assertEqual(len(self.path.read_text(encoding="utf-8").splitlines()), 2)
        reloaded = EvidenceLedger.load(self.path, [self.subject])
        status = next(
            item
            for item in reloaded.readiness(self.subject.hook_id).statuses
            if item.kind is EvidenceKind.ANCHOR_UNIQUE
        )
        self.assertIs(status.recovered_from_failure, True)

    def test_each_claim_occupies_exactly_one_line(self):
        # JSONL only stays greppable and diffable if nothing wraps.
        ledger = self.ledger(self.subject, path=self.path)
        self.satisfy(ledger, self.subject)
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), len(AGENT_REQUIREMENTS))
        for line in lines:
            with self.subTest(line=line[:40]):
                self.assertEqual(json.loads(line)["schema_version"], SCHEMA_VERSION)

    def test_the_parent_directory_is_created_on_demand(self):
        self.assertFalse(self.path.parent.exists())
        ledger = self.ledger(self.subject, path=self.path)
        ledger.record(claim_for(self.subject.hook_id, EvidenceKind.ANCHOR_UNIQUE))
        self.assertTrue(self.path.exists())

    def test_a_ledger_with_no_path_writes_nothing(self):
        ledger = self.ledger(self.subject)
        ledger.record(claim_for(self.subject.hook_id, EvidenceKind.ANCHOR_UNIQUE))
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_a_missing_file_loads_as_an_empty_ledger(self):
        """A first run has no file, and must not be a hard error.

        It must also not be a *pass*: the subjects are still registered, so the
        hook escalates with every required kind not exercised.
        """
        ledger = EvidenceLedger.load(self.tmp / "never-written.jsonl", [self.subject])
        self.assertEqual(ledger.claims, ())
        readiness = ledger.readiness(self.subject.hook_id)
        self.assertIs(readiness.ready, False)
        self.assertEqual(set(readiness.missing), set(AGENT_REQUIREMENTS))

    def test_a_stored_claim_produced_by_its_own_proposer_is_rejected(self):
        """The self-attestation guard must hold on the way in, not only at record time.

        `load` bypasses `record`, so a file hand-edited (or written by an older,
        looser build) would otherwise re-enter the ledger unchecked and count
        towards readiness.
        """
        self_attested = claim_for(
            self.subject.hook_id, EvidenceKind.ANCHOR_UNIQUE, actor=PROPOSER
        )
        self.write_lines(json.dumps(self_attested.to_dict()))
        with self.assertRaises(EvidenceError) as caught:
            EvidenceLedger.load(self.path, [self.subject])
        message = str(caught.exception)
        self.assertIn("produced by its own proposer", message)
        self.assertIn("not trustworthy", message)
        self.assertIn(f"{self.path}:1", message)

    def test_a_self_attested_claim_is_rejected_wherever_it_sits_in_the_file(self):
        good = claim_for(self.subject.hook_id, EvidenceKind.ANCHOR_UNIQUE)
        bad = claim_for(self.subject.hook_id, EvidenceKind.REGISTERS_SAFE, actor=PROPOSER)
        self.write_lines(json.dumps(good.to_dict()), json.dumps(bad.to_dict()))
        with self.assertRaises(EvidenceError) as caught:
            EvidenceLedger.load(self.path, [self.subject])
        self.assertIn(f"{self.path}:2", str(caught.exception))

    def test_a_malformed_line_names_its_line_number(self):
        # A 4000-line ledger is unusable to debug without one.
        good = claim_for(self.subject.hook_id, EvidenceKind.ANCHOR_UNIQUE)
        self.write_lines(
            json.dumps(good.to_dict()), json.dumps(good.to_dict()), "{not json at all"
        )
        with self.assertRaises(EvidenceError) as caught:
            EvidenceLedger.load(self.path, [self.subject])
        message = str(caught.exception)
        self.assertIn(f"{self.path}:3", message)
        self.assertIn("unreadable claim", message)

    def test_a_line_missing_a_required_field_is_unreadable_rather_than_defaulted(self):
        """A claim with no actor must not load as a claim with an empty actor.

        Defaulting would resurrect exactly the case the constructor refuses:
        evidence whose producer cannot be checked against the proposer.
        """
        payload = claim_for(self.subject.hook_id, EvidenceKind.ANCHOR_UNIQUE).to_dict()
        del payload["actor"]
        self.write_lines(json.dumps(payload))
        with self.assertRaises(EvidenceError) as caught:
            EvidenceLedger.load(self.path, [self.subject])
        self.assertIn(f"{self.path}:1", str(caught.exception))

    def test_a_stored_claim_violating_the_producer_taxonomy_is_rejected(self):
        payload = claim_for(self.subject.hook_id, EvidenceKind.RUNTIME_PROBE).to_dict()
        payload["producer"] = Producer.DETERMINISTIC.value
        self.write_lines(json.dumps(payload))
        with self.assertRaises(EvidenceError) as caught:
            EvidenceLedger.load(self.path, [self.subject])
        self.assertIn("unreadable claim", str(caught.exception))

    def test_a_wrong_schema_version_is_refused(self):
        payload = claim_for(self.subject.hook_id, EvidenceKind.ANCHOR_UNIQUE).to_dict()
        payload["schema_version"] = SCHEMA_VERSION + 1
        self.write_lines(json.dumps(payload))
        with self.assertRaises(EvidenceError) as caught:
            EvidenceLedger.load(self.path, [self.subject])
        self.assertIn("unsupported evidence schema", str(caught.exception))

    def test_a_claim_about_an_unregistered_hook_is_refused_at_load(self):
        """Which evidence a hook owes follows from how *this* run resolved it.

        Subjects are supplied by the caller rather than stored, so a stale claim
        about a hook this run does not know about must stop the load rather than
        sit in the ledger unattributed.
        """
        stray = claim_for("hook.from-a-previous-run", EvidenceKind.ANCHOR_UNIQUE)
        self.write_lines(json.dumps(stray.to_dict()))
        with self.assertRaises(EvidenceError) as caught:
            EvidenceLedger.load(self.path, [self.subject])
        self.assertIn("unregistered hook", str(caught.exception))
        self.assertIn("hook.from-a-previous-run", str(caught.exception))

    def test_blank_lines_are_skipped(self):
        good = claim_for(self.subject.hook_id, EvidenceKind.ANCHOR_UNIQUE)
        self.write_lines("", json.dumps(good.to_dict()), "   ", "")
        ledger = EvidenceLedger.load(self.path, [self.subject])
        self.assertEqual(ledger.claims, (good,))

    def test_a_loaded_ledger_keeps_appending_to_the_same_file(self):
        ledger = self.ledger(self.subject, path=self.path)
        ledger.record(claim_for(self.subject.hook_id, EvidenceKind.ANCHOR_UNIQUE))
        reloaded = EvidenceLedger.load(self.path, [self.subject])
        reloaded.record(claim_for(self.subject.hook_id, EvidenceKind.REGISTERS_SAFE))
        self.assertEqual(len(self.path.read_text(encoding="utf-8").splitlines()), 2)
        self.assertEqual(len(EvidenceLedger.load(self.path, [self.subject]).claims), 2)

    def test_load_accepts_a_string_path(self):
        ledger = self.ledger(self.subject, path=self.path)
        ledger.record(claim_for(self.subject.hook_id, EvidenceKind.ANCHOR_UNIQUE))
        self.assertEqual(len(EvidenceLedger.load(str(self.path), [self.subject]).claims), 1)

    def test_claims_for_filters_by_hook_and_kind(self):
        other = self.mechanical("hook.other")
        ledger = self.ledger(self.subject, other)
        ledger.record(claim_for(self.subject.hook_id, EvidenceKind.ANCHOR_UNIQUE))
        ledger.record(claim_for(self.subject.hook_id, EvidenceKind.REGISTERS_SAFE))
        ledger.record(claim_for("hook.other", EvidenceKind.ANCHOR_UNIQUE))
        self.assertEqual(len(ledger.claims_for(self.subject.hook_id)), 2)
        self.assertEqual(
            len(ledger.claims_for(self.subject.hook_id, EvidenceKind.ANCHOR_UNIQUE)), 1
        )
        self.assertEqual(ledger.claims_for("hook.nobody"), ())

    def test_the_claim_id_is_a_content_hash(self):
        # A supersede chain names its parent by id, so the id must follow the
        # content and not the position in the file.
        claim = claim_for("hook.x", EvidenceKind.ANCHOR_UNIQUE)
        self.assertEqual(claim.claim_id, replace(claim).claim_id)
        self.assertNotEqual(
            claim.claim_id, replace(claim, summary="a different summary").claim_id
        )
        self.assertEqual(len(claim.claim_id), 64)


# --------------------------------------------------------------------- report


class ReportTests(LedgerTestCase):
    """The report is what leaves the process, so it must survive JSON."""

    def test_the_report_survives_a_json_round_trip(self):
        """It is handed to a Temporal Activity result and to a gate UI.

        A dict holding an Enum or a frozenset would serialise here and explode
        at the workflow boundary, which is the least debuggable place for it.
        """
        subject = self.agent()
        ledger = self.ledger(subject)
        self.satisfy(ledger, subject, skip=frozenset({EvidenceKind.RUNTIME_PROBE}))
        ledger.record(
            claim_for(subject.hook_id, EvidenceKind.RUNTIME_PROBE, Verdict.INCONCLUSIVE)
        )
        report = ledger.report()
        self.assertEqual(json.loads(json.dumps(report)), report)

    def test_a_report_with_every_verdict_shape_survives_json(self):
        subject = self.agent("hook.mixed")
        ledger = self.ledger(subject)
        ledger.record(claim_for("hook.mixed", EvidenceKind.ANCHOR_UNIQUE, Verdict.PASSED))
        ledger.record(claim_for("hook.mixed", EvidenceKind.REGISTERS_SAFE, Verdict.FAILED))
        ledger.record(
            claim_for("hook.mixed", EvidenceKind.STATIC_VERIFIED, Verdict.INCONCLUSIVE)
        )
        ledger.record(
            claim_for("hook.mixed", EvidenceKind.RUNTIME_PROBE, Verdict.BLOCKED)
        )
        ledger.record(
            waiver("hook.mixed", EvidenceKind.DIFFERENTIAL, "G-1",
                   ACTORS[Producer.HUMAN], "no prior build")
        )
        report = ledger.report()
        self.assertEqual(json.loads(json.dumps(report)), report)

    def test_complete_is_false_when_anything_escalates(self):
        subject = self.mechanical()
        report = self.ledger(subject).report()
        self.assertIs(report["complete"], False)
        self.assertEqual(len(report["escalations"]), 1)

    def test_complete_is_true_only_when_nothing_escalates(self):
        subject = self.mechanical()
        ledger = self.ledger(subject)
        self.satisfy(ledger, subject)
        report = ledger.report()
        self.assertIs(report["complete"], True)
        self.assertEqual(report["escalations"], [])

    def test_complete_tracks_escalations_exactly_across_a_mixed_run(self):
        """`complete` is the one field a caller is most likely to branch on.

        It has to mean "no escalations" and nothing looser, in every mixture of
        ready and unready hooks — including the one where a single hook out of
        many is not ready.
        """
        ready = self.mechanical("hook.ready")
        unready = self.agent("hook.unready")
        ledger = self.ledger(ready, unready)
        self.satisfy(ledger, ready)
        report = ledger.report()
        self.assertIs(report["complete"], False)
        self.assertEqual([item["hook_id"] for item in report["escalations"]], ["hook.unready"])
        self.assertIs(report["hooks"]["hook.ready"]["ready"], True)
        self.assertIs(report["hooks"]["hook.unready"]["ready"], False)

        self.satisfy(ledger, unready)
        report = ledger.report()
        self.assertIs(report["complete"], True)
        self.assertEqual(report["escalations"], [])

    def test_an_empty_ledger_is_complete_because_it_holds_no_hooks(self):
        # Vacuous, and worth pinning: "complete" here means "nothing to
        # escalate", not "everything was verified".
        report = EvidenceLedger().report()
        self.assertIs(report["complete"], True)
        self.assertEqual(report["hooks"], {})
        self.assertEqual(report["claim_count"], 0)

    def test_every_escalation_also_appears_under_hooks(self):
        subject = self.agent()
        report = self.ledger(subject, self.mechanical()).report()
        for escalation in report["escalations"]:
            with self.subTest(hook_id=escalation["hook_id"]):
                self.assertEqual(report["hooks"][escalation["hook_id"]], escalation)

    def test_the_report_counts_every_claim_including_extra_kinds(self):
        subject = self.mechanical()
        ledger = self.ledger(subject)
        self.satisfy(ledger, subject)
        # An extra kind a mechanical hook does not require is still recorded,
        # so a gate can see it, and still counted.
        ledger.record(
            claim_for(subject.hook_id, EvidenceKind.ADVERSARIAL_VERIFIED, Verdict.PASSED)
        )
        report = ledger.report()
        self.assertEqual(report["claim_count"], len(MECHANICAL_REQUIREMENTS) + 1)
        self.assertEqual(
            len(report["hooks"][subject.hook_id]["statuses"]), len(MECHANICAL_REQUIREMENTS)
        )

    def test_the_report_carries_a_schema_version(self):
        self.assertEqual(EvidenceLedger().report()["schema_version"], SCHEMA_VERSION)

    def test_hooks_are_reported_in_a_stable_order(self):
        ledger = self.ledger(
            self.mechanical("hook.z"), self.mechanical("hook.a"), self.mechanical("hook.m")
        )
        self.assertEqual(list(ledger.report()["hooks"]), ["hook.a", "hook.m", "hook.z"])


# ---------------------------------------------------------------- determinism


class DeterminismTests(LedgerTestCase):
    """Nothing here reads a clock, a random source, or a set's iteration order.

    Every one of these runs inside a Temporal workflow or an Activity that can
    be replayed. A function whose second call differs from its first turns a
    replay into a non-deterministic-workflow error, and — worse — makes the
    ledger's answer depend on when it was asked.
    """

    def test_no_module_level_function_differs_between_two_calls(self):
        subject_calls = {
            "requirements_for": lambda: requirements_for("agent"),
            "deterministic_claim": lambda: deterministic_claim(
                "hook.x", EvidenceKind.ANCHOR_UNIQUE, True, "chk", "one site",
                {"matches": 1},
            ),
            "agreement_claim": lambda: agreement_claim(
                "hook.x",
                [{"descriptor": "LX/05t2;", "anchor": ["a"]},
                 {"descriptor": "LX/05t2;", "anchor": ["a"]},
                 {"descriptor": "LX/04Pn;", "anchor": ["b"]}],
            ),
            "probe_claim": lambda: probe_claim(
                "hook.x", "reels", "throwIfBlocked", 3, 0, True, "device:1"
            ),
            "waiver": lambda: waiver(
                "hook.x", EvidenceKind.RUNTIME_PROBE, "G-1", "sam", "documented"
            ),
            "stamped": lambda: stamped(
                deterministic_claim("hook.x", EvidenceKind.ANCHOR_UNIQUE, True, "chk", "s"),
                STAMP,
            ),
            # Takes the clock from its caller for exactly the reason this class
            # exists, so two calls with the same arguments must agree.
            "attributed": lambda: attributed(
                probe_claim("hook.x", "reels", "throwIfBlocked", 3, 0, True, "device:1"),
                recorded_at=STAMP,
                version="440",
                build_sha256="c9" + "0" * 62,
            ),
        }
        # If a function is added to evidence.py, this fails until it is covered.
        defined = {
            name
            for name in dir(evidence_module)
            if not name.startswith("_")
            and callable(getattr(evidence_module, name))
            and getattr(getattr(evidence_module, name), "__module__", None)
            == evidence_module.__name__
            and not isinstance(getattr(evidence_module, name), type)
        }
        self.assertEqual(defined, set(subject_calls))

        for name, call in sorted(subject_calls.items()):
            with self.subTest(function=name):
                self.assertEqual(call(), call())

    def test_a_claim_serialises_identically_twice(self):
        claim = agreement_claim(
            "hook.x",
            [{"descriptor": "LX/05t2;", "anchor": ["a"]},
             {"descriptor": "LX/05t2;", "anchor": ["a"]}],
        )
        self.assertEqual(as_bytes(claim.to_dict()), as_bytes(claim.to_dict()))
        self.assertEqual(claim.claim_id, claim.claim_id)

    def test_readiness_is_identical_on_a_second_call(self):
        subject = self.agent()
        ledger = self.ledger(subject)
        self.satisfy(ledger, subject, skip=frozenset({EvidenceKind.RUNTIME_PROBE}))
        first = ledger.readiness(subject.hook_id)
        second = ledger.readiness(subject.hook_id)
        self.assertEqual(first, second)
        self.assertEqual(as_bytes(first.to_dict()), as_bytes(second.to_dict()))

    def test_the_report_is_identical_on_a_second_call(self):
        subject = self.agent()
        other = self.mechanical()
        ledger = self.ledger(subject, other)
        self.satisfy(ledger, other)
        self.assertEqual(as_bytes(ledger.report()), as_bytes(ledger.report()))

    def test_readiness_does_not_mutate_the_ledger(self):
        # Calling it twice must be free of side effects, or a replay that calls
        # it once and a run that calls it twice would diverge.
        subject = self.agent()
        ledger = self.ledger(subject)
        self.satisfy(ledger, subject)
        before = ledger.claims
        ledger.readiness(subject.hook_id)
        ledger.report()
        self.assertEqual(ledger.claims, before)

    def test_status_order_follows_the_kind_name_not_the_frozenset(self):
        """`required` is a frozenset, whose iteration order varies by run.

        Sorting by `kind.value` is what keeps two workers reporting the same
        hook in the same order, and what makes the serialised readiness
        comparable across runs at all.
        """
        subject = self.agent()
        readiness = self.ledger(subject).readiness(subject.hook_id)
        kinds = [status.kind.value for status in readiness.statuses]
        self.assertEqual(kinds, sorted(kinds))

    def test_reasons_follow_the_same_order_as_statuses(self):
        subject = self.agent()
        readiness = self.ledger(subject).readiness(subject.hook_id)
        named = [reason.split(":", 1)[0] for reason in readiness.reasons]
        self.assertEqual(named, sorted(named))

    def test_nothing_in_the_module_reads_the_clock(self):
        """`stamped` exists precisely so the builders do not call `datetime.now()`.

        A builder that stamped itself would produce a different claim (and a
        different `claim_id`) on every replay.
        """
        claim = deterministic_claim("hook.x", EvidenceKind.ANCHOR_UNIQUE, True, "chk", "s")
        self.assertEqual(claim.recorded_at, "")
        self.assertEqual(probe_claim("h", "s", "sig", 1, 0, True, "d").recorded_at, "")
        self.assertEqual(agreement_claim("h", []).recorded_at, "")
        self.assertEqual(waiver("h", EvidenceKind.DIFFERENTIAL, "G", "sam", "r").recorded_at, "")
        stamp = stamped(claim, STAMP)
        self.assertEqual(stamp.recorded_at, STAMP)
        self.assertEqual(claim.recorded_at, "")  # the original is untouched


# ------------------------------------------------------------------- mutations


class MutationTests(LedgerTestCase):
    """Each guard, re-attacked from the direction a broken implementation takes.

    A positive test proves the guard exists. These prove it bites: every one
    constructs the input a specific plausible mutation would wave through, and
    asserts the outcome that mutation could not produce.
    """

    def test_a_proposer_cannot_corroborate_itself(self):
        """Mutation: drop the `claim.actor == subject.proposed_by` check in `record`.

        In production the proposing agent files all seven items itself and the
        hook goes green on nothing but its own say-so. That is the 340
        `minshop` substitution: one agent, one confident answer, no second
        opinion anywhere, an inert patch on a user's phone.
        """
        subject = self.agent("hook.self")
        ledger = self.ledger(subject)
        for kind in sorted(AGENT_REQUIREMENTS, key=lambda item: item.value):
            with self.subTest(kind=kind.value):
                with self.assertRaises(EvidenceError):
                    ledger.record(claim_for("hook.self", kind, actor=PROPOSER))
        self.assertEqual(ledger.claims, ())
        self.assertIs(ledger.readiness("hook.self").ready, False)

        # The identical evidence from anyone else does make it ready, so the
        # actor string is the only thing between this hook and a device.
        independent = self.agent("hook.independent")
        clean = self.ledger(independent)
        for kind in sorted(AGENT_REQUIREMENTS, key=lambda item: item.value):
            clean.record(claim_for("hook.independent", kind))
        self.assertIs(clean.readiness("hook.independent").ready, True)

    def test_an_unmeasurable_probe_cannot_be_read_as_a_pass(self):
        """Mutation: widen `Verdict.satisfies` to include INCONCLUSIVE.

        In production the Reels hook ships. `replaceReelsEndpoint` blanks the
        endpoint upstream of `throwIfBlocked`, so the probe counts zero blocks
        with the toggle on and zero with it off. Under the mutant that reads as
        "nothing went wrong" and the hook is certified without anything, at any
        point, having observed it work.
        """
        subject = self.mechanical("hook.reels")
        ledger = self.ledger(subject)
        self.satisfy(ledger, subject, skip=frozenset({EvidenceKind.RUNTIME_PROBE}))
        ledger.record(
            probe_claim("hook.reels", "reels_tab", "throwIfBlocked", 0, 0, True,
                        ACTORS[Producer.DEVICE])
        )
        readiness = ledger.readiness("hook.reels")
        self.assertIs(readiness.ready, False)

        # Everything else is satisfied: the inconclusive probe is load-bearing.
        blockers = [item for item in readiness.statuses if not item.satisfied]
        self.assertEqual([item.kind for item in blockers], [EvidenceKind.RUNTIME_PROBE])

        # The mutant's own arithmetic, spelled out: widening `satisfies` by one
        # member flips this hook to ready.
        widened = {Verdict.PASSED, Verdict.WAIVED, Verdict.INCONCLUSIVE}
        self.assertTrue(all(item.verdict in widened for item in readiness.statuses))
        self.assertNotIn(Verdict.INCONCLUSIVE, {v for v in Verdict if v.satisfies})

    def test_re_running_until_green_cannot_defeat_the_ledger(self):
        """Mutation: `satisfied` returns `self.verdict.satisfies`, ignoring the flag.

        In production a flaky-looking check is simply re-run until it passes and
        the ledger records only that it passed. Every failure the pipeline ever
        saw becomes invisible, and the one signal that a hook is marginal — that
        it needed three goes — never reaches a human.
        """
        subject = self.mechanical("hook.retry")
        ledger = self.ledger(subject)
        self.satisfy(ledger, subject, skip=frozenset({EvidenceKind.STATIC_VERIFIED}))
        ledger.record(
            claim_for("hook.retry", EvidenceKind.STATIC_VERIFIED, Verdict.FAILED,
                      summary="smali did not assemble")
        )
        ledger.record(
            claim_for("hook.retry", EvidenceKind.STATIC_VERIFIED, Verdict.PASSED,
                      summary="smali assembled")
        )
        readiness = ledger.readiness("hook.retry")
        self.assertIs(readiness.ready, False)

        # Under the mutant every latest verdict satisfies, so the hook ships.
        self.assertTrue(all(item.verdict.satisfies for item in readiness.statuses))
        self.assertFalse(all(item.satisfied for item in readiness.statuses))

        # And the flag is what does it, on the status itself.
        flagged = next(
            item for item in readiness.statuses if item.kind is EvidenceKind.STATIC_VERIFIED
        )
        self.assertIs(flagged.satisfied, False)
        self.assertIs(replace(flagged, recovered_from_failure=False).satisfied, True)

    def test_an_agent_hook_cannot_skip_adversarial_verification(self):
        """Mutation: `requirements_for` returns MECHANICAL_REQUIREMENTS on unknown input.

        In production a provenance string that does not match — a rename, a
        typo, an upstream `"agent_proposed"` — silently drops
        `adversarial_verified` and `proposer_agreement`. Those are the two items
        that exist because a proposal is the one thing nothing else re-derives:
        the 430 settings hook was statically perfect and the holdout verifier is
        what would have argued with it. The hook would then ship on five
        mechanical checks that all agree with a wrong premise.
        """
        for provenance in ("agent_proposed", "Agent", "agent ", "llm", ""):
            with self.subTest(provenance=provenance):
                with self.assertRaises(EvidenceError):
                    requirements_for(provenance)
                with self.assertRaises(EvidenceError):
                    Subject("hook.typo", provenance, proposed_by=PROPOSER)

        # The consequence, made concrete: the same five mechanical claims are
        # ready under the mechanical requirement set and not ready under the
        # agent one. Only the requirement set differs.
        agent_subject = self.agent("hook.provenance")
        agent_ledger = self.ledger(agent_subject)
        mechanical_subject = self.mechanical("hook.provenance")
        mechanical_ledger = self.ledger(mechanical_subject)
        for kind in sorted(MECHANICAL_REQUIREMENTS, key=lambda item: item.value):
            claim = claim_for("hook.provenance", kind)
            agent_ledger.record(claim)
            mechanical_ledger.record(claim)

        self.assertIs(mechanical_ledger.readiness("hook.provenance").ready, True)
        agent_readiness = agent_ledger.readiness("hook.provenance")
        self.assertIs(agent_readiness.ready, False)
        self.assertEqual(set(agent_readiness.missing), AGENT_ONLY)


# ------------------------------------------------------------------ known gaps


class ReportedDefectTests(LedgerTestCase):
    """Regression tests for four defects this suite found and the module then fixed.

    Each docstring records what the defect would have cost, because that is the
    reason to keep the test rather than the reason it once failed.
    """

    def test_proposers_that_produced_nothing_do_not_count_as_agreeing(self):
        """`agreement_claim` used to fingerprint `{descriptor: None, anchor: []}` as an answer.

        Two proposers that failed outright therefore agreed with each other and
        outvoted a third that actually found the site, returning `passed` with
        `winning_fingerprint` naming the hash of nothing. That is "absence is a
        pass" in the one place this module most forbids it.
        """
        proposals = [
            {"proposer": "agent-a", "descriptor": None, "anchor": [], "rationale": "gave up"},
            {"proposer": "agent-b", "descriptor": None, "anchor": [], "rationale": "no candidate"},
            {"proposer": "agent-c", "descriptor": "LX/05t2;", "anchor": ["iput-object {v0}"]},
        ]
        claim = agreement_claim("hook.defect-a", proposals)
        self.assertIsNot(claim.verdict, Verdict.PASSED)
        self.assertEqual(claim.detail["answered"], 1)
        # The degenerate forms report that nothing was measured, not unanimity.
        for empty in ([{}, {}], [], [{"descriptor": "  ", "anchor": []}]):
            with self.subTest(empty=empty):
                self.assertIs(
                    agreement_claim("hook.defect-a", empty).verdict, Verdict.NOT_EXERCISED
                )

    def test_a_measured_negative_re_run_until_green_is_flagged(self):
        """`recovered_from_failure` used to look only for `FAILED`.

        The Reels probe's own bad state is `inconclusive` — zero signal with the
        toggle on and off — so the cheapest route to green, re-running until the
        counts happen to differ, left the hook satisfied with no reason in the
        escalation. That was the one uncovered path through the retry guard.
        """
        for first in (Verdict.INCONCLUSIVE, Verdict.BLOCKED, Verdict.FAILED):
            with self.subTest(first=first.value):
                subject = self.mechanical(f"hook.defect-b.{first.value}")
                ledger = self.ledger(subject)
                ledger.record(claim_for(subject.hook_id, EvidenceKind.RUNTIME_PROBE, first))
                ledger.record(
                    claim_for(subject.hook_id, EvidenceKind.RUNTIME_PROBE, Verdict.PASSED)
                )
                status = next(
                    item
                    for item in ledger.readiness(subject.hook_id).statuses
                    if item.kind is EvidenceKind.RUNTIME_PROBE
                )
                self.assertIs(status.recovered_from_failure, True)
                self.assertIs(status.satisfied, False)
                self.assertEqual(status.attempts, 2)

    def test_not_exercised_then_passed_is_not_a_retry(self):
        """A kind that was never measured, then measured once, is the normal course.

        Only a verdict that actually looked and came back unsatisfied makes a
        later pass suspicious, so `not_exercised` must not trip the guard —
        otherwise every ordinary run would escalate.
        """
        subject = self.mechanical("hook.defect-b.not-exercised")
        ledger = self.ledger(subject)
        ledger.record(
            claim_for(subject.hook_id, EvidenceKind.RUNTIME_PROBE, Verdict.NOT_EXERCISED)
        )
        ledger.record(claim_for(subject.hook_id, EvidenceKind.RUNTIME_PROBE, Verdict.PASSED))
        status = next(
            item
            for item in ledger.readiness(subject.hook_id).statuses
            if item.kind is EvidenceKind.RUNTIME_PROBE
        )
        self.assertIs(status.recovered_from_failure, False)
        self.assertIs(status.satisfied, True)

    def test_a_trailing_space_does_not_defeat_the_self_attestation_check(self):
        """`record` used to compare actor and proposer with `==`, unnormalised.

        Every other identity in the module strips before testing, so an actor of
        `'agent:resolver-1 '` was a well-formed claim whose producer is the
        proposer under any human reading — and the ledger accepted it.
        """
        subject = self.agent("hook.defect-c")
        ledger = self.ledger(subject)
        for actor in (f"{PROPOSER} ", f" {PROPOSER}", f"\t{PROPOSER}"):
            with self.subTest(actor=repr(actor)):
                with self.assertRaises(EvidenceError):
                    ledger.record(
                        claim_for("hook.defect-c", EvidenceKind.ANCHOR_UNIQUE, actor=actor)
                    )

    def test_negative_observation_counts_are_refused(self):
        """`probe_claim` used to accept them and could return `passed`.

        A harness returning -1 for "the probe did not run" produced `passed`
        whenever the enabled sentinel was arithmetically greater than the
        disabled one, with a summary reading "present -1 time(s) enabled".
        """
        for enabled, disabled in ((-1, -2), (-1, 0), (0, -1)):
            with self.subTest(enabled=enabled, disabled=disabled):
                with self.assertRaises(EvidenceError):
                    probe_claim(
                        "hook.defect-d",
                        "reels",
                        "sig",
                        enabled,
                        disabled,
                        True,
                        ACTORS[Producer.DEVICE],
                    )


if __name__ == "__main__":
    unittest.main()

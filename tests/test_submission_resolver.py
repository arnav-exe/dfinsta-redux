"""The final-verification gate subject, derived from real recorded state.

`prepare_replay_verification_gate_activity` had never been invoked against a
recorded ledger anywhere in the suite: every existing test stubs it, so the
Workflow tests prove the Workflow calls *something* and the contract tests prove
the returned dataclass validates, but nothing proved the body works. These tests
close that gap, and then close the one that matters for the trusted submission
client: the same derivation must produce the same answer through a runtime that
cannot write, because a client that can create the state it is checking is not
checking anything.

The recorded state is not rebuilt here. `ReplayFinalApkVerificationActivityTests`
already assembles exactly it -- an admitted replay, completed framework, decode
and apply operations, and a completed `replay_build_patched_apk_v1` with a real
receipt -- so it is instantiated and driven, the same way its own
`record_predecessors` drives `ReplayBuildActivityTests`. Importing the module
rather than the class is deliberate: a `TestCase` subclass bound as a module
attribute is collected by the loader, and importing the name would silently rerun
that whole file as part of this one.
"""

import hashlib
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dfinsta_pipeline import activities, replay_gate, submission
from dfinsta_pipeline.activities import (
    configure_runtime,
    prepare_replay_verification_gate_activity,
    runtime,
)
from dfinsta_pipeline.contracts import GateDecision, GateRequest, canonical_json
from dfinsta_pipeline.ledger import Ledger
from dfinsta_pipeline.replay_contracts import (
    AdmittedReplayHandleV1,
    ReplayVerificationGateV1,
)
from tests import test_phase_b_verification_activity as verification_helpers

READ_ONLY_MESSAGE = "Ledger is open read-only"
UNRECORDED_RUN_ID = "run-never-admitted"


def ledger_fingerprint(path: Path) -> tuple[int, int, str]:
    """Size, nanosecond mtime and content digest of the ledger file.

    All three, because each alone is weak: a same-size overwrite keeps the size,
    a second-granularity check would miss a fast rewrite, and a digest alone
    would miss a rewrite of identical bytes. Together they are a behavioural
    statement that the file was not written -- much stronger than grepping the
    derivation for write calls, which only proves nobody wrote one today.
    """

    stat = path.stat()
    return (stat.st_size, stat.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())


class RecordedReplayFixture(unittest.IsolatedAsyncioTestCase):
    """Drive the verification-activity fixture to get real recorded state."""

    def setUp(self) -> None:
        # `configure_runtime` sets a module global, and these tests reconfigure
        # it on purpose. Restore whatever was there so a later test in the same
        # process is not handed this one's temporary state root.
        previous_runtime = activities._runtime
        self.addCleanup(setattr, activities, "_runtime", previous_runtime)

        self.fixture = verification_helpers.ReplayFinalApkVerificationActivityTests(
            methodName="runTest"
        )
        # The fixture's `setUp` registers its temporary-directory cleanup with
        # `addCleanup`, so driving it outside a runner means running those
        # cleanups by hand. `doCleanups` dispatches through `_callCleanup`,
        # which asserts an asyncio runner exists, so one is created for it.
        self.fixture._setupAsyncioRunner()
        self.addCleanup(self.fixture._tearDownAsyncioRunner)
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

        self.admitted = self.fixture.admitted
        self.run_id = self.admitted.run_spec.run_id
        self.handle = AdmittedReplayHandleV1(1, self.run_id, self.admitted.sha256)
        self.state = self.fixture.state
        self.ledger_path = self.state / "ledger.sqlite3"
        self.assertTrue(self.ledger_path.is_file(), self.ledger_path)

        # Derived here from the fixture's own objects rather than read back from
        # the Activity, so the Activity is compared against an independently
        # computed subject and not against itself.
        self.request = replay_gate.derive_verification_request(
            self.admitted, self.fixture.completed_build, self.fixture.build_receipt
        )
        self.expected_gate = ReplayVerificationGateV1(
            1,
            self.run_id,
            f"{self.run_id}-final-verification-gate",
            self.request.sha256,
            self.admitted.run_spec.allowed_actor,
            self.admitted.run_spec.policy_revision,
        )

    def read_only_runtime(self, **overrides: object) -> None:
        configure_runtime(self.state, read_only=True, **overrides)  # type: ignore[arg-type]
        self.assertIs(runtime().ledger.read_only, True)

    def later_decision(self) -> GateDecision:
        """A decision the ledger has not seen, so recording it is a real write."""

        return replace(
            self.admitted.decision,
            decision_id="decision-recorded-after-the-derivation",
            idempotency_id="request-recorded-after-the-derivation",
            rationale="recorded to prove a write is visible",
        )

    def fixture_runtime_arguments(self) -> dict[str, object]:
        """The four optional arguments the fixture passed to `configure_runtime`."""

        capability = replay_gate.derive_verification_capability(self.admitted, self.run_id)
        return {
            "attempts_root": self.fixture.attempts,
            "source_root": self.fixture.source_root,
            "executor_paths": {capability.executable_sha256: self.fixture.executable},
            "launcher": self.fixture.launcher,
        }


class VerificationGateDerivationTests(RecordedReplayFixture):
    async def test_the_activity_derives_the_gate_from_recorded_state(self) -> None:
        gate = await prepare_replay_verification_gate_activity(self.handle)

        self.assertIs(type(gate), ReplayVerificationGateV1)
        self.assertEqual(gate.schema_version, 1)
        self.assertEqual(gate.run_id, self.run_id)
        self.assertEqual(gate.gate_id, f"{self.run_id}-final-verification-gate")
        self.assertEqual(gate.allowed_actor, self.admitted.run_spec.allowed_actor)
        self.assertEqual(gate.policy_revision, self.admitted.run_spec.policy_revision)
        self.assertEqual(gate.request_sha256, self.request.sha256)
        self.assertEqual(gate, self.expected_gate)

    async def test_the_derived_subject_binds_the_recorded_build(self) -> None:
        # The reason this gate cannot be raised up front: its subject names a
        # build that does not exist when the run is admitted. Assert the
        # derivation reached that build rather than some default.
        completed_build, build_receipt = replay_gate.resolve_admitted_build(self.admitted)
        self.assertEqual(completed_build, self.fixture.completed_build)
        self.assertEqual(build_receipt, self.fixture.build_receipt)
        self.assertEqual(
            self.request.completed_patched_apk_receipt, self.fixture.completed_build
        )
        self.assertEqual(self.request.patched_apk, self.fixture.build_receipt.patched_apk)
        self.assertEqual(self.request.admitted_replay_sha256, self.admitted.sha256)

    async def test_a_handle_the_ledger_does_not_recognise_is_refused(self) -> None:
        # Negative control for the two tests above: they would look identical if
        # the Activity derived the subject from the handle it was handed rather
        # than from recorded authority. It does not -- a handle pinning a
        # different SHA cannot reach a subject at all.
        for handle, message in (
            (
                AdmittedReplayHandleV1(1, self.run_id, "0" * 64),
                "Admitted replay authority does not match handle",
            ),
            (
                AdmittedReplayHandleV1(1, UNRECORDED_RUN_ID, self.admitted.sha256),
                "Admitted replay authority is not recorded",
            ),
        ):
            with self.subTest(handle=handle):
                with self.assertRaises(ValueError) as caught:
                    await prepare_replay_verification_gate_activity(handle)
                self.assertEqual(str(caught.exception), message)

    async def test_the_activity_is_deterministic_on_unchanged_state(self) -> None:
        first = await prepare_replay_verification_gate_activity(self.handle)
        second = await prepare_replay_verification_gate_activity(self.handle)
        self.assertEqual(canonical_json(first), canonical_json(second))


class AdmittedReplayHandleRecoveryTests(RecordedReplayFixture):
    async def test_the_ledger_recovers_the_handle_from_the_run_id(self) -> None:
        recovered = Ledger.admitted_replay_handle_for_run(runtime().ledger, self.run_id)

        self.assertIs(type(recovered), AdmittedReplayHandleV1)
        self.assertEqual(recovered, self.handle)
        self.assertEqual(
            Ledger.load_admitted_replay_v3(runtime().ledger, recovered), self.admitted
        )
        # A run id is all a published gate request gives the client, so the
        # recovered handle must reach the same subject as the hand-built one.
        self.assertEqual(
            await prepare_replay_verification_gate_activity(recovered), self.expected_gate
        )

    def test_an_unrecorded_run_id_is_refused(self) -> None:
        with self.assertRaises(ValueError) as caught:
            Ledger.admitted_replay_handle_for_run(runtime().ledger, UNRECORDED_RUN_ID)
        self.assertEqual(str(caught.exception), "Admitted replay authority is not recorded")


class ReadOnlyDerivationTests(RecordedReplayFixture):
    async def test_a_read_only_runtime_derives_a_byte_identical_gate(self) -> None:
        writable = await prepare_replay_verification_gate_activity(self.handle)
        self.assertIs(runtime().ledger.read_only, False)
        before = ledger_fingerprint(self.ledger_path)

        self.read_only_runtime()
        read_only = await prepare_replay_verification_gate_activity(self.handle)

        self.assertEqual(read_only, writable)
        self.assertEqual(
            canonical_json(read_only).encode("utf-8"),
            canonical_json(writable).encode("utf-8"),
        )
        self.assertEqual(read_only, self.expected_gate)
        self.assertEqual(ledger_fingerprint(self.ledger_path), before)

    async def test_a_read_only_open_leaves_only_empty_wal_side_files(self) -> None:
        # Measured, and asserted rather than ignored: "the read-only client
        # writes nothing at all" would be false. Opening a WAL database
        # read-only makes SQLite create its shared-memory index beside the
        # ledger, and a read-only connection cannot checkpoint, so both side
        # files survive the close. None of that is a change to the authority --
        # the `-wal` is empty, the database file is byte-identical, and the next
        # writable open removes them -- but a test that claimed an untouched
        # directory would be asserting something that is not true.
        decisions = Ledger.decision_count(runtime().ledger)
        before = ledger_fingerprint(self.ledger_path)

        self.read_only_runtime()
        await prepare_replay_verification_gate_activity(self.handle)

        appeared = {entry.name for entry in self.state.iterdir()} - {"cas", "ledger.sqlite3"}
        self.assertEqual(appeared, {"ledger.sqlite3-shm", "ledger.sqlite3-wal"})
        self.assertEqual((self.state / "ledger.sqlite3-wal").stat().st_size, 0)
        self.assertEqual(ledger_fingerprint(self.ledger_path), before)

        configure_runtime(self.state)
        self.assertEqual(
            sorted(entry.name for entry in self.state.iterdir()), ["cas", "ledger.sqlite3"]
        )
        self.assertEqual(Ledger.decision_count(runtime().ledger), decisions)
        self.assertEqual(ledger_fingerprint(self.ledger_path), before)

    async def test_the_unchanged_check_would_notice_a_real_write(self) -> None:
        # Positive control for the assertion above. Without it, "the fingerprint
        # did not change" could be true because the fingerprint cannot change.
        before = ledger_fingerprint(self.ledger_path)
        Ledger.record_decision(runtime().ledger, self.later_decision())
        self.assertNotEqual(ledger_fingerprint(self.ledger_path), before)

    async def test_the_derivation_needs_only_the_state_root(self) -> None:
        # Measured, not assumed: the derivation reads the ledger and the content
        # store, both of which hang off `state_root`. It never launches a
        # process, never opens the attempts tree and never touches the source
        # repository, so none of the four optional arguments reach it. Each
        # configuration below must produce the same bytes.
        expected = canonical_json(self.expected_gate).encode("utf-8")
        arguments = self.fixture_runtime_arguments()
        configurations: list[tuple[str, dict[str, object]]] = [
            ("everything the fixture passed", dict(arguments))
        ]
        for omitted in arguments:
            configurations.append(
                (
                    f"without {omitted}",
                    {name: value for name, value in arguments.items() if name != omitted},
                )
            )
        configurations.append(("state root only", {}))

        before = ledger_fingerprint(self.ledger_path)
        for label, overrides in configurations:
            with self.subTest(configuration=label):
                self.read_only_runtime(**overrides)
                gate = await prepare_replay_verification_gate_activity(self.handle)
                self.assertEqual(canonical_json(gate).encode("utf-8"), expected)
        self.assertEqual(ledger_fingerprint(self.ledger_path), before)


class ReadOnlyRuntimeWriteRefusalTests(RecordedReplayFixture):
    def test_a_read_only_runtime_refuses_to_record_a_decision(self) -> None:
        decision = self.later_decision()
        self.read_only_runtime()
        before = Ledger.decision_count(runtime().ledger)

        with self.assertRaises(RuntimeError) as caught:
            Ledger.record_decision(runtime().ledger, decision)

        self.assertEqual(str(caught.exception), READ_ONLY_MESSAGE)
        self.assertIs(Ledger.has_decision(runtime().ledger, decision), False)
        self.assertEqual(Ledger.decision_count(runtime().ledger), before)

    def test_the_same_call_succeeds_on_a_writable_runtime(self) -> None:
        # Positive control: the refusal above is about the mode, not about the
        # decision being invalid or already recorded.
        decision = self.later_decision()
        self.assertIs(runtime().ledger.read_only, False)
        before = Ledger.decision_count(runtime().ledger)

        Ledger.record_decision(runtime().ledger, decision)

        self.assertIs(Ledger.has_decision(runtime().ledger, decision), True)
        self.assertEqual(Ledger.decision_count(runtime().ledger), before + 1)


class ClientResolverAgreesWithTheActivityTests(RecordedReplayFixture):
    """The client's resolver must compute what the preparing Activity computed.

    This is the claim the whole submission client rests on: it re-derives the
    gate subject rather than copying the one the Workflow published, and refuses
    when the two disagree. That claim is only worth anything if the client's
    derivation actually reproduces the Activity's on real recorded state --
    otherwise the client would refuse every genuine gate, and whoever is on call
    would learn to work around it.

    Run through a read-only runtime, because that is how the client runs.
    """

    async def test_the_resolver_reproduces_the_activity_subject(self) -> None:
        activity_gate = await prepare_replay_verification_gate_activity(self.handle)
        self.read_only_runtime()

        subject = submission._resolve_replay_verification(self.run_id)

        self.assertIs(type(subject), submission.DerivedSubject)
        self.assertEqual(subject.run_id, activity_gate.run_id)
        self.assertEqual(subject.gate_id, activity_gate.gate_id)
        self.assertEqual(subject.policy_revision, activity_gate.policy_revision)
        self.assertEqual(subject.allowed_actor, activity_gate.allowed_actor)
        # `ReplayRunWorkflow` binds the decision to the request hash three times
        # over, so all three of the client's hashes must be that one hash.
        self.assertEqual(subject.subject_sha256, activity_gate.request_sha256)
        self.assertEqual(subject.admission_sha256, activity_gate.request_sha256)
        self.assertEqual(subject.prepared_sha256, activity_gate.request_sha256)

    async def test_the_resolver_passes_the_clients_own_verification(self) -> None:
        """End to end on real state: derive, publish, verify, and it agrees.

        The Workflow builds its `GateRequest` from the Activity's gate; this
        reproduces that construction exactly and then runs the client's own
        `verify_published_gate` over the pair. A passing result here is the only
        evidence that the two halves of the design meet.
        """

        activity_gate = await prepare_replay_verification_gate_activity(self.handle)
        issued_at = datetime(2026, 8, 2, tzinfo=timezone.utc)
        published = GateRequest(
            schema_version=1,
            run_id=activity_gate.run_id,
            gate_id=activity_gate.gate_id,
            subject_sha256=activity_gate.request_sha256,
            admission_sha256=activity_gate.request_sha256,
            prepared_sha256=activity_gate.request_sha256,
            policy_revision=activity_gate.policy_revision,
            issued_at=issued_at.isoformat(),
            expires_at=(issued_at + timedelta(days=7)).isoformat(),
        )
        self.read_only_runtime()
        subject = submission._resolve_replay_verification(self.run_id)

        submission.verify_published_gate(
            published, subject, now=issued_at + timedelta(minutes=5)
        )

        # Positive control: the verification is capable of failing, so the pass
        # above is about agreement rather than about a check that never fires.
        with self.assertRaises(submission.SubmissionRefused):
            submission.verify_published_gate(
                replace(published, subject_sha256="9" * 64),
                subject,
                now=issued_at + timedelta(minutes=5),
            )

    async def test_the_shipped_registry_selects_this_resolver_for_this_gate(self) -> None:
        """The resolver is reachable by the real gate id, not just callable.

        A correct derivation registered under a gate id the producer never mints
        would leave every real gate unanswerable, and the failure would look like
        "no resolver registered" rather than like a wiring mistake.
        """

        activity_gate = await prepare_replay_verification_gate_activity(self.handle)
        kind = submission.select_gate_kind(activity_gate.gate_id, activity_gate.run_id)

        self.assertIs(kind, submission.REPLAY_VERIFICATION_GATE)
        self.assertIs(kind.resolve, submission._resolve_replay_verification)
        self.assertEqual(kind.update_name, "submit_verification_decision")


if __name__ == "__main__":
    unittest.main()

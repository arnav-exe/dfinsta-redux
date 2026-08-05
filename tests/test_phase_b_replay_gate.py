import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from unittest import mock

from dfinsta_pipeline import replay_gate
from dfinsta_pipeline.contracts import ArtifactRef, GateDecision, canonical_json, canonical_sha256
from dfinsta_pipeline.executor import ExecutorCapability
from dfinsta_pipeline.ledger import Ledger
from dfinsta_pipeline.replay_contracts import (
    ReplayVerificationGrantRequestV1,
    admit_replay_verification_grant_v1,
)
from dfinsta_pipeline.replay_gate import (
    BUILD_OPERATION_KIND,
    derive_verification_capability,
    derive_verification_request,
    derived_identifier,
    resolve_admitted_build,
    resolve_completed_build,
)
from tests.test_phase_b_replay_contracts import admit_v3, fixture_v3
from tests.test_phase_b_verification_grant import ref, synthetic_build_receipt


class Forbidden:
    """Any attribute access is a purity violation."""

    def __init__(self, label: str) -> None:
        self._label = label

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"{self._label}.{name} accessed during pure derivation")

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"{self._label} called during pure derivation")


class GateFixture:
    """Admitted replay plus the completed build the gate subject binds."""

    def __init__(self, *, final_bytes: bytes = b"synthetic verified final APK") -> None:
        self.admitted = admit_v3(fixture_v3())
        self.final_bytes = final_bytes
        self.receipt = synthetic_build_receipt(self.admitted, final_bytes)
        self.receipt_bytes = canonical_json(self.receipt).encode("utf-8")
        self.completed_build = ref(
            "replay-patched-apk-receipt-v1",
            self.receipt_bytes,
            self.receipt.operation_key,
            self.receipt.receipt_input_hashes,
        )
        self.payloads = {
            canonical_sha256(self.completed_build): self.receipt_bytes,
            canonical_sha256(self.receipt.patched_apk): final_bytes,
        }

    def resolve(self, artifact: ArtifactRef) -> bytes:
        return self.payloads[canonical_sha256(artifact)]

    def derive(self) -> ReplayVerificationGrantRequestV1:
        return derive_verification_request(
            self.admitted, self.completed_build, self.receipt
        )

    def approval(self, request: ReplayVerificationGrantRequestV1) -> GateDecision:
        return GateDecision(
            1,
            f"{request.run_id}-final-verification-decision",
            f"{request.run_id}-final-verification-decision-attempt",
            request.allowed_actor,
            request.run_id,
            request.gate_id,
            request.sha256,
            request.sha256,
            request.sha256,
            request.policy_revision,
            "approve",
            "Approved final decode verification",
            "2026-07-31T00:00:00Z",
        )

    def record_build(self, ledger: Ledger) -> None:
        Ledger.begin_operation(
            ledger,
            self.receipt.operation_key,
            BUILD_OPERATION_KIND,
            self.receipt.expected_operation_input_sha256,
            "replay-gate-fixture",
            retry_safe=False,
        )
        Ledger.record_effect(
            ledger, self.receipt.operation_key, "replay-gate-fixture", self.completed_build
        )
        Ledger.complete_operation(ledger, self.receipt.operation_key, self.completed_build)


class DeriveVerificationRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = GateFixture()

    def test_derivation_is_deterministic_and_byte_identical(self) -> None:
        first = self.case.derive()
        second = self.case.derive()
        self.assertIsNot(first, second)
        self.assertEqual(canonical_json(first), canonical_json(second))
        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual(first, second)

    def test_derivation_is_deterministic_across_equal_inputs(self) -> None:
        other = GateFixture()
        self.assertIsNot(other.admitted, self.case.admitted)
        self.assertEqual(other.admitted, self.case.admitted)
        self.assertEqual(
            canonical_json(self.case.derive()), canonical_json(other.derive())
        )

    def test_derived_request_satisfies_contract_validation(self) -> None:
        request = self.case.derive()
        self.assertEqual(
            ReplayVerificationGrantRequestV1.from_dict(asdict(request)), request
        )
        self.assertEqual(request.sha256, canonical_sha256(request))

        capability = request.executor_capability
        self.assertIs(type(capability), ExecutorCapability)
        self.assertEqual(capability.input_kinds, ("final-apk",))
        self.assertEqual(capability.output_kind, "decoded-tree")
        self.assertEqual(capability.allowed_mutation_paths, ("framework", "output"))
        self.assertEqual(capability.allowed_environment, ())
        self.assertEqual(capability.fixed_environment, ())
        self.assertEqual(
            capability.executable_sha256,
            self.case.admitted.capability("decode").executable_sha256,
        )

    def test_derived_request_admits_a_verification_grant(self) -> None:
        request = self.case.derive()
        decision = self.case.approval(request)
        grant = admit_replay_verification_grant_v1(
            request,
            decision,
            self.case.admitted,
            self.case.receipt,
            lambda candidate: candidate == decision,
            self.case.resolve,
        )
        self.assertEqual(grant.request, request)
        self.assertEqual(grant.patched_apk_receipt, self.case.receipt)

    def test_fields_are_derived_from_the_admitted_authority(self) -> None:
        request = self.case.derive()
        admitted = self.case.admitted
        self.assertEqual(request.schema_version, 1)
        self.assertEqual(request.run_id, admitted.run_spec.run_id)
        self.assertEqual(request.allowed_actor, admitted.run_spec.allowed_actor)
        self.assertEqual(request.policy_revision, admitted.run_spec.policy_revision)
        self.assertEqual(request.admitted_replay_sha256, admitted.sha256)
        self.assertEqual(request.decoder_profile_id, admitted.profile.profile_id)
        self.assertEqual(
            request.tool_artifact_sha256,
            admitted.profile.tool_for_role("decode").artifact_sha256,
        )
        self.assertEqual(request.timeout_seconds, admitted.plan("decode").timeout_seconds)
        self.assertEqual(request.completed_patched_apk_receipt, self.case.completed_build)
        self.assertEqual(request.patched_apk, self.case.receipt.patched_apk)
        self.assertEqual(
            request.grant_id, f"{admitted.run_spec.run_id}-final-verification-grant"
        )
        self.assertEqual(
            request.gate_id, f"{admitted.run_spec.run_id}-final-verification-gate"
        )

    def test_derivation_touches_no_ledger_or_store(self) -> None:
        from dfinsta_pipeline import activities

        expected = canonical_json(self.case.derive())
        self.assertNotIn("activities", vars(replay_gate))

        with mock.patch.object(
            replay_gate, "Ledger", Forbidden("Ledger")
        ), mock.patch.object(
            activities, "runtime", Forbidden("runtime")
        ), mock.patch.object(
            Ledger, "require_completed_operation", Forbidden("require_completed_operation")
        ):
            observed = canonical_json(self.case.derive())
        self.assertEqual(observed, expected)
        self.assertNotIn("activities", vars(replay_gate))

    def test_changed_build_receipt_changes_the_request_hash(self) -> None:
        baseline = self.case.derive()
        other = GateFixture(final_bytes=b"a different verified final APK")
        self.assertNotEqual(other.receipt, self.case.receipt)
        self.assertNotEqual(other.derive().sha256, baseline.sha256)

        restated = replace(
            self.case.completed_build, size=self.case.completed_build.size + 1
        )
        rederived = derive_verification_request(
            self.case.admitted, restated, self.case.receipt
        )
        self.assertNotEqual(rederived.sha256, baseline.sha256)

    def test_arguments_must_be_exact_types(self) -> None:
        case = self.case
        for arguments in (
            (None, case.completed_build, case.receipt),
            (case.admitted, asdict(case.completed_build), case.receipt),
            (case.admitted, case.completed_build, asdict(case.receipt)),
        ):
            with self.subTest(arguments=arguments), self.assertRaises(TypeError):
                derive_verification_request(*arguments)

    def test_derived_identifiers_must_stay_valid(self) -> None:
        self.assertEqual(derived_identifier("run", "-suffix", "label"), "run-suffix")
        with self.assertRaises(ValueError):
            derived_identifier("r" * 128, "-final-verification-grant", "grant id")
        with self.assertRaises(ValueError):
            derived_identifier("-leading-dash", "-suffix", "label")
        with self.assertRaises(TypeError):
            derived_identifier(None, "-suffix", "label")

    def test_capability_identity_is_run_scoped(self) -> None:
        capability = derive_verification_capability(
            self.case.admitted, self.case.admitted.run_spec.run_id
        )
        self.assertEqual(
            capability.capability_id,
            f"{self.case.admitted.run_spec.run_id}-final-verification-decode",
        )
        self.assertEqual(
            capability, self.case.derive().executor_capability
        )
        self.assertNotEqual(
            capability.canonical_identity,
            self.case.admitted.capability("decode").canonical_identity,
        )

    def test_module_names_no_port_target_or_apk_file(self) -> None:
        source = Path(replay_gate.__file__).read_text(encoding="utf-8")
        for literal in ("340", "430", ".apk"):
            with self.subTest(literal=literal):
                self.assertNotIn(literal, source)


class ResolveCompletedBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = GateFixture()
        temporary = tempfile.TemporaryDirectory(prefix="replay-gate-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ledger = Ledger(self.root / "ledger.sqlite3")
        self.case.record_build(self.ledger)

    def test_returns_the_exact_completed_build(self) -> None:
        resolved = resolve_completed_build(self.ledger, self.case.receipt)
        self.assertEqual(resolved, self.case.completed_build)
        self.assertIsNot(resolved, self.case.completed_build)
        self.assertIs(type(resolved), ArtifactRef)

    def test_ignores_instance_level_method_shadowing(self) -> None:
        self.ledger.require_completed_operation = Forbidden("shadowed ledger method")
        self.assertEqual(
            resolve_completed_build(self.ledger, self.case.receipt),
            self.case.completed_build,
        )

    def test_unrecorded_build_is_refused(self) -> None:
        empty = Ledger(self.root / "empty.sqlite3")
        with self.assertRaises(ValueError):
            resolve_completed_build(empty, self.case.receipt)

    def test_build_recorded_under_another_input_is_refused(self) -> None:
        divergent = Ledger(self.root / "divergent.sqlite3")
        key = self.case.receipt.operation_key
        Ledger.begin_operation(
            divergent, key, BUILD_OPERATION_KIND, "0" * 64, "replay-gate-fixture",
            retry_safe=False,
        )
        Ledger.record_effect(divergent, key, "replay-gate-fixture", self.case.completed_build)
        Ledger.complete_operation(divergent, key, self.case.completed_build)
        with self.assertRaises(ValueError):
            resolve_completed_build(divergent, self.case.receipt)

    def test_receipt_must_be_an_exact_type(self) -> None:
        with self.assertRaises(TypeError):
            resolve_completed_build(self.ledger, asdict(self.case.receipt))

    def test_kind_is_the_build_operation(self) -> None:
        self.assertEqual(BUILD_OPERATION_KIND, "replay_build_patched_apk_v1")


class ResolveAdmittedBuildTests(unittest.TestCase):
    """`resolve_admitted_build` is a call to the one implementation.

    It used to restate `activities._replay_verification_predecessors` line for
    line — reconstruct the predecessors, rebuild the operation identity, require
    the completed claim, validate the receipt — through three public aliases. Two
    copies of a chain that long agree until one is edited, and only a
    signature-drift test stood between them.

    So what is checked here is no longer "does this module still call four
    helpers with the arguments they declare" but "do both callers reach the same
    function". The chain itself is exercised end to end by the build and
    verification activity suites, and by two real ports through the registered
    Workflow.
    """

    def setUp(self) -> None:
        self.case = GateFixture()

    def test_both_callers_share_one_implementation(self) -> None:
        """The property the extraction exists for, checked on both sides.

        `activities._replay_verification_predecessors` is the verify Activity's
        proven path and this module derives the gate subject; if they ever stop
        naming the same function, the gate can be derived from one chain and the
        verification admitted against another.
        """
        import inspect

        from dfinsta_pipeline import activities

        self.assertEqual(
            tuple(inspect.signature(activities.resolve_replay_build).parameters),
            ("admitted",),
        )
        for name in (
            "replay_build_predecessors",
            "replay_build_operation_identity",
            "validate_replay_patched_apk_receipt",
        ):
            self.assertFalse(hasattr(activities, name), f"{name} alias survived")
        for source, label in (
            (inspect.getsource(resolve_admitted_build), "replay_gate"),
            (
                inspect.getsource(activities._replay_verification_predecessors),
                "activities",
            ),
        ):
            self.assertIn("resolve_replay_build(", source, label)
            # And neither reconstructs the chain for itself any more.
            for helper in ("_replay_build_predecessors(", "_replay_build_operation_identity("):
                self.assertNotIn(helper, source, f"{label} still inlines {helper}")

    def test_delegates_and_returns_the_completed_build_and_receipt(self) -> None:
        """Five values come back; exactly two are this caller's answer."""
        from dfinsta_pipeline import activities

        seen: dict[str, Any] = {}

        def resolve(admitted: Any) -> tuple[Any, ...]:
            seen["admitted"] = admitted
            return (
                "completed-framework",
                "framework-receipt",
                self.case.completed_build,
                self.case.receipt,
                "compiled",
            )

        with mock.patch.object(activities, "resolve_replay_build", resolve):
            resolved = resolve_admitted_build(self.case.admitted)

        self.assertIs(seen["admitted"], self.case.admitted)
        self.assertEqual(resolved, (self.case.completed_build, self.case.receipt))

    def test_a_refusal_from_the_shared_chain_is_not_swallowed(self) -> None:
        """The chain refuses by raising; this module must not turn that into a
        value, which is what a bare `except` around a five-tuple unpack would do.
        """
        from dfinsta_pipeline import activities

        def refuse(admitted: Any) -> tuple[Any, ...]:
            raise ValueError("recorded build does not match its predecessors")

        with mock.patch.object(activities, "resolve_replay_build", refuse):
            with self.assertRaisesRegex(ValueError, "does not match its predecessors"):
                resolve_admitted_build(self.case.admitted)

    def test_activities_import_is_deferred(self) -> None:
        self.assertNotIn("activities", vars(replay_gate))
        self.assertTrue(callable(resolve_admitted_build))


if __name__ == "__main__":
    unittest.main()

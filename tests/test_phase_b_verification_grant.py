import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, fields, replace
from pathlib import Path

from dfinsta_pipeline.contracts import ArtifactRef, GateDecision, canonical_json, canonical_sha256
from dfinsta_pipeline.executor import ExecutorCapability
from dfinsta_pipeline.ledger import Ledger
from dfinsta_pipeline.replay_contracts import (
    AdmittedReplayVerificationGrantV1,
    ReplayBackendCompositionV1,
    ReplayPatchedApkReceiptV1,
    ReplayVerificationGrantRequestV1,
    admit_replay_verification_grant_v1,
)
from tests.test_phase_b_build_contracts import receipt as base_build_receipt
from tests.test_phase_b_replay_contracts import admit_v3, fixture_v3


def ref(
    kind: str,
    payload: bytes,
    producer: str,
    inputs: tuple[str, ...] = (),
) -> ArtifactRef:
    digest = canonical_bytes_sha256(payload)
    return ArtifactRef(
        1,
        kind,
        digest,
        len(payload),
        f"cas://sha256/{digest}",
        producer,
        inputs,
    )


def canonical_bytes_sha256(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()


def synthetic_build_receipt(admitted, final_bytes: bytes) -> ReplayPatchedApkReceiptV1:
    base = base_build_receipt()
    operation_key = base.operation_key
    final_sha256 = canonical_bytes_sha256(final_bytes)
    composition = ReplayBackendCompositionV1(
        1,
        "apktool_full_rebuild",
        admitted.profile.profile_id,
        base.composition.backend_sha256,
        admitted.request.stock_apk.sha256,
        final_sha256,
        final_sha256,
        base.composition.final_dex_entries,
        (),
        (),
        0,
        (),
        True,
    )
    execution_inputs = (
        admitted.sha256,
        canonical_sha256(base.completed_patched_tree_receipt),
        canonical_sha256(base.patched_tree_manifest),
        base.patched_tree_semantic_sha256,
        base.target_port_spec_sha256,
        canonical_sha256(admitted.request.stock_apk),
        admitted.profile.sha256,
        base.execution_plan_sha256,
        base.executor_capability_sha256,
        base.tool_artifact_sha256,
        base.execution_request_sha256,
    )
    intermediate = ArtifactRef(
        1,
        "intermediate-apk",
        final_sha256,
        len(final_bytes),
        f"cas://sha256/{final_sha256}",
        operation_key,
        execution_inputs,
    )
    patched_inputs = (
        *execution_inputs,
        canonical_sha256(intermediate),
        composition.sha256,
    )
    patched = ArtifactRef(
        1,
        "final-apk",
        final_sha256,
        len(final_bytes),
        f"cas://sha256/{final_sha256}",
        operation_key,
        patched_inputs,
    )
    receipt = ReplayPatchedApkReceiptV1(
        1,
        admitted.sha256,
        base.completed_patched_tree_receipt,
        base.patched_tree_manifest,
        base.patched_tree_semantic_sha256,
        base.target_port_spec_sha256,
        admitted.request.stock_apk,
        admitted.profile.profile_id,
        admitted.profile.sha256,
        "build",
        base.execution_plan_sha256,
        base.executor_capability_sha256,
        base.tool_artifact_sha256,
        base.execution_request_sha256,
        None,
        None,
        None,
        intermediate,
        composition,
        patched,
        operation_key,
        True,
    )
    operation_key = receipt.expected_operation_key
    intermediate = replace(intermediate, producer_operation_id=operation_key)
    patched = replace(
        patched,
        producer_operation_id=operation_key,
        input_hashes=(
            *execution_inputs,
            canonical_sha256(intermediate),
            composition.sha256,
        ),
    )
    return replace(
        receipt,
        intermediate_apk=intermediate,
        patched_apk=patched,
        operation_key=operation_key,
    )


def verification_capability() -> ExecutorCapability:
    return ExecutorCapability(
        1,
        "final-decode-v1",
        "e" * 64,
        (
            "-jar",
            "{tool}",
            "d",
            "-f",
            "{input_apk}",
            "-o",
            "{decoded_tree}",
            "-p",
            "{framework_dir}",
        ),
        ("decoded_tree", "framework_dir", "input_apk", "tool"),
        ("final-apk",),
        "decoded-tree",
        (),
        (),
        ("framework", "output"),
    )


class VerificationFixture:
    def __init__(self) -> None:
        self.admitted = admit_v3(fixture_v3())
        self.final_bytes = b"synthetic verified final APK"
        self.receipt = synthetic_build_receipt(self.admitted, self.final_bytes)
        receipt_bytes = canonical_json(self.receipt).encode("utf-8")
        self.completed_receipt = ref(
            "replay-patched-apk-receipt-v1",
            receipt_bytes,
            self.receipt.operation_key,
            self.receipt.receipt_input_hashes,
        )
        self.request = ReplayVerificationGrantRequestV1(
            1,
            "verification-grant-1",
            self.admitted.run_spec.run_id,
            "final-verification-gate",
            self.admitted.run_spec.allowed_actor,
            self.admitted.run_spec.policy_revision,
            self.admitted.sha256,
            self.completed_receipt,
            self.receipt.patched_apk,
            self.admitted.profile.profile_id,
            self.admitted.profile.tool_for_role("decode").artifact_sha256,
            300,
            verification_capability(),
        )
        self.decision = GateDecision(
            1,
            "verification-decision-1",
            "verification-decision-attempt-1",
            self.request.allowed_actor,
            self.request.run_id,
            self.request.gate_id,
            self.request.sha256,
            self.request.sha256,
            self.request.sha256,
            self.request.policy_revision,
            "approve",
            "Approved final decode verification",
            "2026-07-31T00:00:00Z",
        )
        self.payloads = {
            canonical_sha256(self.completed_receipt): receipt_bytes,
            canonical_sha256(self.receipt.patched_apk): self.final_bytes,
        }

    def resolve(self, artifact: ArtifactRef) -> bytes:
        return self.payloads[canonical_sha256(artifact)]

    def admit(
        self,
        *,
        request=None,
        decision=None,
        admitted_replay=None,
        receipt=None,
        recorded=None,
        resolver=None,
    ) -> AdmittedReplayVerificationGrantV1:
        return admit_replay_verification_grant_v1(
            request or self.request,
            decision or self.decision,
            admitted_replay or self.admitted,
            receipt or self.receipt,
            recorded or (lambda candidate: candidate == self.decision),
            resolver or self.resolve,
        )


class ReplayVerificationGrantContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = VerificationFixture()

    def test_roundtrip_hash_and_admission(self) -> None:
        request = ReplayVerificationGrantRequestV1.from_dict(asdict(self.case.request))
        grant = self.case.admit()
        self.assertEqual(request, self.case.request)
        self.assertEqual(request.sha256, canonical_sha256(request))
        self.assertEqual(
            AdmittedReplayVerificationGrantV1.from_dict(asdict(grant)), grant
        )
        self.assertEqual(grant.sha256, canonical_sha256(grant))

        value = asdict(grant)
        for mutation in (
            {key: item for key, item in value.items() if key != "decision"},
            {**value, "unknown": 1},
            {**value, "schema_version": True},
        ):
            with self.subTest(mutation=mutation), self.assertRaises((TypeError, ValueError)):
                AdmittedReplayVerificationGrantV1.from_dict(mutation)

    def test_request_fields_types_and_subclasses_are_strict(self) -> None:
        value = asdict(self.case.request)
        mutations = (
            {key: item for key, item in value.items() if key != "grant_id"},
            {**value, "unknown": 1},
            {**value, "schema_version": True},
            {**value, "timeout_seconds": True},
            {**value, "timeout_seconds": 0},
            {**value, "timeout_seconds": 3601},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises((TypeError, ValueError)):
                ReplayVerificationGrantRequestV1.from_dict(mutation)

        class CapabilitySubclass(ExecutorCapability):
            pass

        capability = CapabilitySubclass(
            *(getattr(self.case.request.executor_capability, field.name) for field in fields(ExecutorCapability))
        )
        with self.assertRaises(TypeError):
            replace(self.case.request, executor_capability=capability)

    def test_capability_is_exactly_final_decode_only(self) -> None:
        capability = self.case.request.executor_capability
        mutations = (
            {"argv_template": (*capability.argv_template, "--extra")},
            {"path_arguments": ("framework_dir", "input_apk", "tool")},
            {"input_kinds": ("stock-apk",)},
            {"output_kind": "other"},
            {"allowed_environment": ("HOME",)},
            {"fixed_environment": (("MODE", "unsafe"),)},
            {"allowed_mutation_paths": ("output",)},
        )
        for changes in mutations:
            with self.subTest(changes=changes), self.assertRaises((TypeError, ValueError)):
                replace(self.case.request, executor_capability=replace(capability, **changes))

    def test_all_request_and_decision_relationship_substitutions_fail(self) -> None:
        request_changes = (
            {"run_id": "other-run"},
            {"allowed_actor": "other-actor"},
            {"policy_revision": "other-policy"},
            {"admitted_replay_sha256": "0" * 64},
            {"decoder_profile_id": "other-profile"},
            {"tool_artifact_sha256": "0" * 64},
        )
        for changes in request_changes:
            request = replace(self.case.request, **changes)
            decision = replace(
                self.case.decision,
                actor=request.allowed_actor,
                run_id=request.run_id,
                subject_sha256=request.sha256,
                admission_sha256=request.sha256,
                prepared_sha256=request.sha256,
                policy_revision=request.policy_revision,
            )
            with self.subTest(request=changes), self.assertRaises(ValueError):
                self.case.admit(request=request, decision=decision, recorded=lambda _: True)

        decision_changes = (
            {"decision": "reject"},
            {"run_id": "other-run"},
            {"gate_id": "other-gate"},
            {"actor": "other-actor"},
            {"policy_revision": "other-policy"},
            {"subject_sha256": "0" * 64},
            {"admission_sha256": "0" * 64},
            {"prepared_sha256": "0" * 64},
        )
        for changes in decision_changes:
            with self.subTest(decision=changes), self.assertRaises(ValueError):
                self.case.admit(
                    decision=replace(self.case.decision, **changes),
                    recorded=lambda _: True,
                )

    def test_receipt_and_apk_relationship_substitutions_fail(self) -> None:
        with self.assertRaises(ValueError):
            self.case.admit(
                receipt=replace(self.case.receipt, admitted_replay_sha256="0" * 64)
            )
        with self.assertRaises(ValueError):
            self.case.admit(
                receipt=replace(
                    self.case.receipt,
                    toolchain_profile_sha256="0" * 64,
                )
            )
        changed_ref = replace(self.case.completed_receipt, sha256="0" * 64, uri=f"cas://sha256/{'0' * 64}")
        with self.assertRaises(ValueError):
            self.case.admit(request=replace(self.case.request, completed_patched_apk_receipt=changed_ref))

    def test_admission_checks_predicate_and_artifact_bytes(self) -> None:
        with self.assertRaises(ValueError):
            self.case.admit(recorded=lambda _: False)
        with self.assertRaises(TypeError):
            self.case.admit(recorded=lambda _: 1)

        def raises(_: GateDecision) -> bool:
            raise RuntimeError("unavailable")

        with self.assertRaises(ValueError):
            self.case.admit(recorded=raises)
        with self.assertRaises(TypeError):
            self.case.admit(resolver=lambda _: "not bytes")

        payloads = dict(self.case.payloads)
        payloads[canonical_sha256(self.case.completed_receipt)] = b"tampered"
        with self.assertRaises(ValueError):
            self.case.admit(resolver=lambda artifact: payloads[canonical_sha256(artifact)])
        payloads = dict(self.case.payloads)
        payloads[canonical_sha256(self.case.receipt.patched_apk)] = b"tampered"
        with self.assertRaises(ValueError):
            self.case.admit(resolver=lambda artifact: payloads[canonical_sha256(artifact)])

    def test_admission_and_grant_reject_subclasses(self) -> None:
        grant = self.case.admit()

        class RequestSubclass(ReplayVerificationGrantRequestV1):
            pass

        class GrantSubclass(AdmittedReplayVerificationGrantV1):
            pass

        class DecisionSubclass(GateDecision):
            pass

        class ReceiptSubclass(ReplayPatchedApkReceiptV1):
            pass

        request = RequestSubclass(
            *(getattr(self.case.request, field.name) for field in fields(ReplayVerificationGrantRequestV1))
        )
        with self.assertRaises(TypeError):
            self.case.admit(request=request)
        decision = DecisionSubclass(
            *(getattr(self.case.decision, field.name) for field in fields(GateDecision))
        )
        with self.assertRaises(TypeError):
            self.case.admit(decision=decision)
        receipt = ReceiptSubclass(
            *(getattr(self.case.receipt, field.name) for field in fields(ReplayPatchedApkReceiptV1))
        )
        with self.assertRaises(TypeError):
            self.case.admit(receipt=receipt)
        with self.assertRaises(TypeError):
            admit_replay_verification_grant_v1(
                self.case.request,
                self.case.decision,
                self.case.admitted,
                self.case.receipt,
                object(),  # type: ignore[arg-type]
                self.case.resolve,
            )
        with self.assertRaises(TypeError):
            admit_replay_verification_grant_v1(
                self.case.request,
                self.case.decision,
                self.case.admitted,
                self.case.receipt,
                lambda _: True,
                object(),  # type: ignore[arg-type]
            )
        subclass = GrantSubclass(
            *(getattr(grant, field.name) for field in fields(AdmittedReplayVerificationGrantV1))
        )
        self.assertIsInstance(subclass, AdmittedReplayVerificationGrantV1)


class ReplayVerificationGrantLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "ledger.sqlite3"
        self.ledger = Ledger(self.path)
        self.case = VerificationFixture()
        self.grant = self.case.admit()

    def record_replay(self) -> None:
        self.ledger.record_decision(self.case.admitted.decision)
        self.ledger.record_admitted_replay_v3(self.case.admitted)

    def record_build(self, *, output: ArtifactRef | None = None) -> None:
        completed = output or self.case.completed_receipt
        key = self.case.receipt.operation_key
        self.ledger.begin_operation(
            key,
            "replay_build_patched_apk_v1",
            self.case.receipt.expected_operation_input_sha256,
            "verification-fixture",
            retry_safe=False,
        )
        self.ledger.record_effect(key, "verification-fixture", completed)
        self.ledger.complete_operation(key, completed)

    def record_all_dependencies(self) -> None:
        self.record_replay()
        self.record_build()
        self.ledger.record_decision(self.case.decision)

    def test_unrecorded_decision_replay_and_incomplete_build_fail(self) -> None:
        with self.assertRaisesRegex(ValueError, "decision"):
            self.ledger.record_admitted_replay_verification_grant_v1(self.grant)
        self.ledger.record_decision(self.case.decision)
        with self.assertRaisesRegex(ValueError, "replay authority"):
            self.ledger.record_admitted_replay_verification_grant_v1(self.grant)
        self.record_replay()
        with self.assertRaisesRegex(ValueError, "build claim"):
            self.ledger.record_admitted_replay_verification_grant_v1(self.grant)
        self.ledger.begin_operation(
            self.case.receipt.operation_key,
            "replay_build_patched_apk_v1",
            self.case.receipt.expected_operation_input_sha256,
            "verification-fixture",
            retry_safe=False,
        )
        with self.assertRaisesRegex(ValueError, "not completed"):
            self.ledger.record_admitted_replay_verification_grant_v1(self.grant)

    def test_wrong_completed_build_output_fails(self) -> None:
        self.record_replay()
        self.ledger.record_decision(self.case.decision)
        wrong = replace(
            self.case.completed_receipt,
            size=self.case.completed_receipt.size + 1,
        )
        self.record_build(output=wrong)
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.ledger.record_admitted_replay_verification_grant_v1(self.grant)

    def test_fabricated_build_input_and_event_projection_fail(self) -> None:
        self.record_replay()
        self.ledger.record_decision(self.case.decision)
        key = self.case.receipt.operation_key
        self.ledger.begin_operation(
            key,
            "replay_build_patched_apk_v1",
            "7" * 64,
            "verification-fixture",
            retry_safe=False,
        )
        self.ledger.record_effect(
            key, "verification-fixture", self.case.completed_receipt
        )
        self.ledger.complete_operation(key, self.case.completed_receipt)
        with self.assertRaisesRegex(ValueError, "input does not match"):
            Ledger.record_admitted_replay_verification_grant_v1(
                self.ledger, self.grant
            )

        with self.ledger._connection() as connection:
            connection.execute(
                "UPDATE operation_claims SET input_sha256 = ? WHERE operation_key = ?",
                (self.case.receipt.expected_operation_input_sha256, key),
            )
        with self.assertRaisesRegex(ValueError, "append-only events"):
            Ledger.record_admitted_replay_verification_grant_v1(
                self.ledger, self.grant
            )

    def test_internal_helper_shadowing_cannot_bypass_authority(self) -> None:
        self.record_all_dependencies()
        for name in (
            "_require_decision_row",
            "_require_admitted_replay_v3_row",
            "_require_completed_build_claim",
            "_verification_grant_values",
        ):
            setattr(self.ledger, name, lambda *args, **kwargs: None)
        Ledger.record_admitted_replay_verification_grant_v1(
            self.ledger, self.grant
        )
        self.assertEqual(
            Ledger.require_admitted_replay_verification_grant_v1(
                self.ledger, self.grant
            ),
            self.grant,
        )

    def test_released_pending_build_retry_is_valid_authority(self) -> None:
        self.record_replay()
        self.ledger.record_decision(self.case.decision)
        key = self.case.receipt.operation_key
        input_sha256 = self.case.receipt.expected_operation_input_sha256
        self.ledger.begin_operation(
            key,
            "replay_build_patched_apk_v1",
            input_sha256,
            "first-owner",
            retry_safe=False,
        )
        self.ledger.release_pending_operation(key, "first-owner")
        self.ledger.begin_operation(
            key,
            "replay_build_patched_apk_v1",
            input_sha256,
            "second-owner",
            retry_safe=False,
        )
        self.ledger.record_effect(
            key, "second-owner", self.case.completed_receipt
        )
        self.ledger.complete_operation(key, self.case.completed_receipt)
        Ledger.record_admitted_replay_verification_grant_v1(
            self.ledger, self.grant
        )
        self.assertEqual(
            Ledger.require_admitted_replay_verification_grant_v1(
                self.ledger, self.grant
            ),
            self.grant,
        )

    def test_idempotent_restart_and_distinct_normalized_result(self) -> None:
        self.record_all_dependencies()
        self.ledger.record_admitted_replay_verification_grant_v1(self.grant)
        self.ledger.record_admitted_replay_verification_grant_v1(self.grant)
        restarted = Ledger(self.path)
        required = restarted.require_admitted_replay_verification_grant_v1(self.grant)
        self.assertEqual(required, self.grant)
        self.assertIsNot(required, self.grant)
        self.assertIsNot(required.request, self.grant.request)
        with restarted._connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM admitted_replay_verification_grants_v1"
                ).fetchone()[0],
                1,
            )

    def test_collision_subclasses_and_append_only_triggers(self) -> None:
        self.record_all_dependencies()
        self.ledger.record_admitted_replay_verification_grant_v1(self.grant)
        competing_request = replace(
            self.case.request,
            grant_id="verification-grant-2",
            gate_id="final-verification-gate-2",
        )
        competing_decision = replace(
            self.case.decision,
            decision_id="verification-decision-2",
            idempotency_id="verification-decision-attempt-2",
            gate_id=competing_request.gate_id,
            subject_sha256=competing_request.sha256,
            admission_sha256=competing_request.sha256,
            prepared_sha256=competing_request.sha256,
        )
        competing = AdmittedReplayVerificationGrantV1(
            1,
            competing_request,
            competing_decision,
            self.case.admitted,
            self.case.receipt,
        )
        self.ledger.record_decision(competing_decision)
        with self.assertRaisesRegex(ValueError, "identity collision"):
            self.ledger.record_admitted_replay_verification_grant_v1(competing)

        class GrantSubclass(AdmittedReplayVerificationGrantV1):
            pass

        subclass = GrantSubclass(
            *(getattr(self.grant, field.name) for field in fields(AdmittedReplayVerificationGrantV1))
        )
        with self.assertRaises(TypeError):
            self.ledger.record_admitted_replay_verification_grant_v1(subclass)
        with self.assertRaises(TypeError):
            self.ledger.require_admitted_replay_verification_grant_v1(subclass)

        with self.ledger._connection() as connection:
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute(
                    "UPDATE admitted_replay_verification_grants_v1 SET grant_json = '{}'"
                )
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("DELETE FROM admitted_replay_verification_grants_v1")

    def test_stored_json_tamper_fails_closed(self) -> None:
        self.record_all_dependencies()
        self.ledger.record_admitted_replay_verification_grant_v1(self.grant)
        with self.ledger._connection() as connection:
            connection.execute(
                "DROP TRIGGER admitted_replay_verification_grants_v1_no_update"
            )
            connection.execute(
                "UPDATE admitted_replay_verification_grants_v1 SET grant_json = '{}'"
            )
        with self.assertRaisesRegex(ValueError, "corrupt"):
            self.ledger.require_admitted_replay_verification_grant_v1(self.grant)

    def test_concurrent_identical_writers_adopt_one_row(self) -> None:
        self.record_all_dependencies()
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(
                executor.map(
                    lambda _: self.ledger.record_admitted_replay_verification_grant_v1(
                        self.grant
                    ),
                    range(2),
                )
            )
        self.assertEqual(
            self.ledger.require_admitted_replay_verification_grant_v1(self.grant),
            self.grant,
        )


if __name__ == "__main__":
    unittest.main()

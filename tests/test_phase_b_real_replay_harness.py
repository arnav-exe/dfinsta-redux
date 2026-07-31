import os
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from dfinsta_pipeline.contracts import canonical_sha256
from dfinsta_pipeline.replay_contracts import (
    ToolchainProfileV3,
    admit_replay_verification_grant_v1,
)
from tests.integration import test_real_replay_harness as harness
from tests.integration.test_real_replay_harness import (
    API36_FRAMEWORK_SHA256,
    APKTOOL_SHA256,
    JAVA_SHA256,
    REPOSITORY_ROOT,
    TARGET_EVIDENCE_KEYS,
    TARGETS,
    _put,
    _verification_request_and_decision,
    admit_and_record,
    build_profile,
    final_decode_capability,
    process_stage_order,
    select_targets,
    stage_launch_expectations,
    stage_order,
    validate_run_root,
)
from tests.test_phase_b_replay_contracts import fixture_v3
from tests.test_phase_b_verification_grant import VerificationFixture


class RealReplayHarnessFastTests(unittest.TestCase):
    def test_target_table_exact_hashes_and_counts(self) -> None:
        self.assertEqual(
            TARGETS[340].stock_sha256,
            "68f4546f8cb597a668d6033916200ef99191a9006350fcd986fd33392aea5113",
        )
        self.assertEqual(
            TARGETS[430].stock_sha256,
            "38ae9861b9ca89f60f41767324e1c3d54a4e3a00ed5555b92660a08e6db14754",
        )
        self.assertEqual(APKTOOL_SHA256, "7956eb04194300ce0d0a84ad18771eebc94b89fb8d1ddcce8ea4c056818646f4")
        self.assertEqual(JAVA_SHA256, "1a86d087fa5a5be1ed3e8a531ae891da85fc80aad15ab6fa98060763f2eb7000")
        self.assertEqual(API36_FRAMEWORK_SHA256, "1f95cd4676f3e16e0432a0f19c01026593101fd26d8190233c70803de8453473")
        self.assertEqual((TARGETS[340].source_file_count, TARGETS[430].source_file_count), (112, 5))
        self.assertEqual((TARGETS[340].operation_count, TARGETS[430].operation_count), (59, 7))

    def test_profiles_and_capabilities_are_sorted_and_valid(self) -> None:
        for target in (340, 430):
            with self.subTest(target=target):
                profile, capabilities = build_profile(TARGETS[target])
                self.assertIsInstance(ToolchainProfileV3.from_dict(asdict(profile)), ToolchainProfileV3)
                roles = tuple(binding.role for binding in profile.capability_bindings)
                self.assertEqual(roles, tuple(sorted(roles)))
                self.assertEqual(tuple(plan.role for plan in profile.execution_plans), roles)
                self.assertEqual(profile.plan("decode").timeout_seconds, 600)
                self.assertEqual(profile.plan("build").timeout_seconds, 600)
                self.assertEqual(profile.plan("decode").arguments, (
                    ("decoded_tree", "decoded_tree"),
                    ("framework_dir", "framework_dir"),
                    ("input_apk", "input_apk"),
                    ("tool", "tool"),
                ))
                for binding, capability in zip(profile.capability_bindings, capabilities, strict=True):
                    profile.validate_capability(binding.role, capability)
                    if binding.role == "build":
                        self.assertEqual(
                            capability.allowed_mutation_paths,
                            (
                                ("intermediate.apk", "patched-tree/build")
                                if target == 430
                                else (
                                    "framework/1.apk",
                                    "intermediate.apk",
                                    "patched-tree/build",
                                )
                            ),
                        )
                if target == 430:
                    self.assertEqual(profile.frameworks[0].package_id, 1)
                    self.assertEqual(profile.plan("install_framework").timeout_seconds, 300)
                else:
                    self.assertEqual(profile.frameworks, ())

    def test_final_decode_capability_is_exact_for_both_targets(self) -> None:
        for target in (340, 430):
            with self.subTest(target=target):
                capability = final_decode_capability(JAVA_SHA256, target)
                self.assertEqual(
                    capability.capability_id,
                    f"real-replay-{target}-final-apk-decode-java",
                )
                self.assertEqual(capability.executable_sha256, JAVA_SHA256)
                self.assertEqual(
                    capability.argv_template,
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
                )
                self.assertEqual(
                    capability.path_arguments,
                    ("decoded_tree", "framework_dir", "input_apk", "tool"),
                )
                self.assertEqual(capability.input_kinds, ("final-apk",))
                self.assertEqual(capability.output_kind, "decoded-tree")
                self.assertEqual(capability.allowed_environment, ())
                self.assertEqual(capability.fixed_environment, ())
                self.assertEqual(
                    capability.allowed_mutation_paths, ("framework", "output")
                )

    def test_verification_decision_and_grant_bind_exact_completed_build(self) -> None:
        case = VerificationFixture()
        for target in (340, 430):
            with self.subTest(target=target):
                request, decision = _verification_request_and_decision(
                    TARGETS[target],
                    case.admitted,
                    case.completed_receipt,
                    case.receipt,
                )
                grant = admit_replay_verification_grant_v1(
                    request,
                    decision,
                    case.admitted,
                    case.receipt,
                    lambda candidate: candidate == decision,
                    case.resolve,
                )
                self.assertEqual(request.admitted_replay_sha256, case.admitted.sha256)
                self.assertEqual(
                    request.completed_patched_apk_receipt, case.completed_receipt
                )
                self.assertEqual(request.patched_apk, case.receipt.patched_apk)
                self.assertEqual(
                    request.tool_artifact_sha256,
                    case.admitted.profile.tool_for_role("decode").artifact_sha256,
                )
                self.assertEqual(request.timeout_seconds, 300)
                self.assertNotEqual(request.gate_id, case.admitted.run_spec.gate_id)
                self.assertNotEqual(decision.decision_id, case.admitted.decision.decision_id)
                self.assertEqual(decision.gate_id, request.gate_id)
                self.assertEqual(
                    (decision.subject_sha256, decision.admission_sha256, decision.prepared_sha256),
                    (request.sha256, request.sha256, request.sha256),
                )
                self.assertEqual(grant.request, request)
                self.assertEqual(grant.decision, decision)
                self.assertEqual(grant.admitted_replay, case.admitted)
                self.assertEqual(grant.patched_apk_receipt, case.receipt)
                self.assertEqual(grant.sha256, canonical_sha256(grant))

    def test_stage_order_launch_expectations_and_evidence_schema(self) -> None:
        self.assertEqual(stage_order(TARGETS[340]), ("decode", "apply", "build", "verify"))
        self.assertEqual(
            stage_order(TARGETS[430]),
            ("framework", "decode", "apply", "build", "verify"),
        )
        self.assertEqual(process_stage_order(TARGETS[340]), ("decode", "build", "verify"))
        self.assertEqual(
            process_stage_order(TARGETS[430]),
            ("framework", "decode", "build", "verify"),
        )
        self.assertEqual(
            stage_launch_expectations(TARGETS[340]),
            {"decode": 1, "apply": 0, "build": 1, "verify": 1},
        )
        self.assertEqual(
            stage_launch_expectations(TARGETS[430]),
            {"framework": 1, "decode": 1, "apply": 0, "build": 1, "verify": 1},
        )
        self.assertEqual(
            TARGET_EVIDENCE_KEYS,
            frozenset(
                {
                    "target",
                    "artifact_identities",
                    "semantic_hashes",
                    "profile",
                    "capabilities",
                    "build_capability_status",
                    "verification_capability_status",
                    "admission_scope",
                    "verification_admission_scope",
                    "admitted_replay_sha256",
                    "self_issued_test_authority_refs",
                    "verification_authority",
                    "receipt_refs",
                    "manifests",
                    "source_evidence",
                    "operation_results",
                    "patched_apk",
                    "final_verification",
                    "ordered_outcomes",
                    "adoption_proof",
                    "ledger",
                    "verification_operation_claim",
                    "referenced_artifact_producer_claims",
                    "referenced_manifest_cas_children",
                    "process_records",
                }
            ),
        )

    def test_authority_round_trip_records_admit_replay_v3_result(self) -> None:
        case = fixture_v3(with_framework=True, framework_package_ids=(1,))
        with tempfile.TemporaryDirectory() as temporary:
            from dfinsta_pipeline.ledger import Ledger

            ledger = Ledger(Path(temporary) / "ledger.sqlite3")
            admitted = admit_and_record(
                case.run_spec,
                case.request,
                case.decision,
                ledger,
                case.resolve,
                case.resolve_capability,
            )
            self.assertEqual(ledger.require_admitted_replay_v3(admitted), admitted)
            self.assertTrue(ledger.has_decision(case.decision))

    def test_imported_artifact_has_completed_ledger_producer(self) -> None:
        from dfinsta_pipeline.activities import configure_runtime, runtime

        with tempfile.TemporaryDirectory() as temporary:
            configure_runtime(Path(temporary) / "state")
            reference = _put("fixture", b"exact bytes", "fast-test-import")
            with runtime().ledger._connection() as connection:
                row = connection.execute(
                    "SELECT status, input_sha256 FROM operation_claims "
                    "WHERE operation_key = ?",
                    (reference.producer_operation_id,),
                ).fetchone()
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row[0], "completed")
            self.assertEqual(
                reference,
                runtime().ledger.require_completed_operation(
                    reference.producer_operation_id,
                    "real_replay_import_v1",
                    row[1],
                ),
            )

    def test_target_selector_and_root_refusals(self) -> None:
        self.assertEqual(select_targets(None), (340, 430))
        self.assertEqual(select_targets("430"), (430,))
        for invalid in ("", "340,340", "300", "340,300", "word"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                select_targets(invalid)
        with self.assertRaises(ValueError):
            validate_run_root("relative/path")
        with self.assertRaises(ValueError):
            validate_run_root(str(REPOSITORY_ROOT / "real-replay-output"))
        with self.assertRaises(ValueError):
            validate_run_root(str(REPOSITORY_ROOT.parent))
        with tempfile.TemporaryDirectory() as temporary:
            existing = Path(temporary) / "existing"
            existing.mkdir()
            with self.assertRaises(FileExistsError):
                validate_run_root(str(existing))
            absent = Path(temporary) / "absent"
            self.assertEqual(validate_run_root(str(absent)), absent.resolve())

    def test_real_test_is_skipped_by_default(self) -> None:
        self.assertEqual(
            getattr(harness.RealReplayIntegrationTests, "__unittest_skip__", False),
            os.environ.get("DFINSTA_RUN_REAL_REPLAY") != "1",
        )

    def test_outcome_markers_are_atomic_and_mutually_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary)
            success = harness._publish_outcome(run_root, "success.json", {"ok": True})
            with self.assertRaises(FileExistsError):
                harness._publish_outcome(run_root, "failure.json", {"ok": False})
            self.assertTrue(success.is_file())
            self.assertFalse((run_root / "failure.json").exists())
            self.assertEqual(
                tuple(run_root.glob(".*.tmp")),
                (),
            )

    def test_integration_module_alias_prevents_duplicate_unittest_discovery(self) -> None:
        self.assertIs(
            sys.modules["integration.test_real_replay_harness"],
            sys.modules["tests.integration.test_real_replay_harness"],
        )
        self.assertIs(
            sys.modules["test_real_replay_harness"],
            sys.modules["tests.integration.test_real_replay_harness"],
        )
        suite = unittest.defaultTestLoader.loadTestsFromModule(harness)
        self.assertEqual(suite.countTestCases(), 1)


if __name__ == "__main__":
    unittest.main()

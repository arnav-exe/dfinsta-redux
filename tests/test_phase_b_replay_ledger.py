import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
from pathlib import Path

from dfinsta_pipeline.ledger import Ledger
from dfinsta_pipeline.replay_contracts import (
    AdmittedReplayHandleV1,
    AdmittedReplayV3,
    ReplayRunSpecV2,
    ReplayVerificationGrantHandleV1,
)
from tests.test_phase_b_replay_contracts import admit_v3, fixture_v3
from tests.test_phase_b_verification_grant import VerificationFixture


class ReplayLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "ledger.sqlite3"
        self.ledger = Ledger(self.path)
        self.admitted = admit_v3(fixture_v3())

    def record_authorities(self, admitted: AdmittedReplayV3 | None = None) -> None:
        admitted = admitted or self.admitted
        self.ledger.record_decision(admitted.decision)
        self.ledger.record_admitted_replay_v3(admitted)

    def competing_admission(self) -> AdmittedReplayV3:
        decision = replace(
            self.admitted.decision,
            decision_id="decision-competing",
            idempotency_id="request-competing",
            rationale="independently approved for replay",
        )
        return replace(self.admitted, decision=decision)

    def test_missing_decision_and_missing_replay_authority_fail(self) -> None:
        with self.assertRaisesRegex(ValueError, "decision"):
            self.ledger.record_admitted_replay_v3(self.admitted)
        with self.assertRaisesRegex(ValueError, "authority"):
            self.ledger.require_admitted_replay_v3(self.admitted)

    def test_exact_record_is_idempotent_and_survives_restart(self) -> None:
        self.record_authorities()
        self.ledger.record_admitted_replay_v3(self.admitted)
        restarted = Ledger(self.path)
        self.assertEqual(restarted.require_admitted_replay_v3(self.admitted), self.admitted)
        with restarted._connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM admitted_replays_v3").fetchone()[0],
                1,
            )
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_require_returns_exact_normalized_distinct_object(self) -> None:
        self.record_authorities()
        required = self.ledger.require_admitted_replay_v3(self.admitted)
        self.assertEqual(required, self.admitted)
        self.assertIsNot(required, self.admitted)
        self.assertIsNot(required.run_spec, self.admitted.run_spec)

    def test_same_run_with_different_valid_decision_collides(self) -> None:
        competing = self.competing_admission()
        self.ledger.record_decision(self.admitted.decision)
        self.ledger.record_decision(competing.decision)
        self.ledger.record_admitted_replay_v3(self.admitted)
        with self.assertRaisesRegex(ValueError, "identity collision"):
            self.ledger.record_admitted_replay_v3(competing)

    def test_wrong_concrete_type_and_subclass_are_rejected(self) -> None:
        class ReplaySubclass(AdmittedReplayV3):
            pass

        subclass = ReplaySubclass(
            *(getattr(self.admitted, field.name) for field in fields(AdmittedReplayV3))
        )
        for candidate in (object(), subclass):
            with self.subTest(type=type(candidate).__name__):
                with self.assertRaises(TypeError):
                    self.ledger.record_admitted_replay_v3(candidate)  # type: ignore[arg-type]
                with self.assertRaises(TypeError):
                    self.ledger.require_admitted_replay_v3(candidate)  # type: ignore[arg-type]

    def test_nested_subclass_is_rejected_before_authority_is_recorded(self) -> None:
        class RunSpecSubclass(ReplayRunSpecV2):
            pass

        nested = replace(
            self.admitted,
            run_spec=RunSpecSubclass(
                *(
                    getattr(self.admitted.run_spec, field.name)
                    for field in fields(ReplayRunSpecV2)
                )
            ),
        )
        self.ledger.record_decision(nested.decision)
        with self.assertRaisesRegex(ValueError, "exact canonical"):
            self.ledger.record_admitted_replay_v3(nested)
        with self.ledger._connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM admitted_replays_v3").fetchone()[0],
                0,
            )

    def test_admitted_replay_rows_are_append_only(self) -> None:
        self.record_authorities()
        with self.ledger._connection() as connection:
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute(
                    "UPDATE admitted_replays_v3 SET decision_sha256 = ? WHERE run_id = ?",
                    ("0" * 64, self.admitted.run_spec.run_id),
                )
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute(
                    "DELETE FROM admitted_replays_v3 WHERE run_id = ?",
                    (self.admitted.run_spec.run_id,),
                )

    def test_stored_json_tamper_fails_closed(self) -> None:
        self.record_authorities()
        with self.ledger._connection() as connection:
            connection.execute("DROP TRIGGER admitted_replays_v3_no_update")
            connection.execute(
                "UPDATE admitted_replays_v3 SET admitted_json = ? WHERE run_id = ?",
                ("{}", self.admitted.run_spec.run_id),
            )
        with self.assertRaisesRegex(ValueError, "corrupt"):
            self.ledger.require_admitted_replay_v3(self.admitted)

    def test_concurrent_identical_writers_adopt_one_row(self) -> None:
        self.ledger.record_decision(self.admitted.decision)
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(
                executor.map(
                    lambda _: self.ledger.record_admitted_replay_v3(self.admitted),
                    range(2),
                )
            )
        self.assertEqual(self.ledger.require_admitted_replay_v3(self.admitted), self.admitted)

    def test_concurrent_competing_admissions_have_one_winner(self) -> None:
        competing = self.competing_admission()
        self.ledger.record_decision(self.admitted.decision)
        self.ledger.record_decision(competing.decision)

        def record(candidate: AdmittedReplayV3) -> AdmittedReplayV3 | None:
            try:
                self.ledger.record_admitted_replay_v3(candidate)
                return candidate
            except ValueError as error:
                self.assertIn("identity collision", str(error))
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(record, (self.admitted, competing)))
        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.ledger.require_admitted_replay_v3(winners[0]), winners[0])


class AdmittedReplayHandleLoadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "ledger.sqlite3"
        self.ledger = Ledger(self.path)
        self.admitted = admit_v3(fixture_v3())
        self.ledger.record_decision(self.admitted.decision)
        self.ledger.record_admitted_replay_v3(self.admitted)
        self.handle = AdmittedReplayHandleV1(
            1, self.admitted.run_spec.run_id, self.admitted.sha256
        )

    def test_load_returns_recorded_authority_not_the_callers_object(self) -> None:
        loaded = self.ledger.load_admitted_replay_v3(self.handle)
        self.assertEqual(loaded, self.admitted)
        self.assertIsNot(loaded, self.admitted)
        self.assertIsNot(loaded.run_spec, self.admitted.run_spec)
        self.assertEqual(loaded.sha256, self.handle.admitted_replay_sha256)

    def test_load_refuses_an_unrecorded_run(self) -> None:
        unknown = replace(self.handle, run_id="run-never-admitted")
        with self.assertRaisesRegex(ValueError, "authority is not recorded"):
            self.ledger.load_admitted_replay_v3(unknown)

    def test_load_refuses_a_sha256_pin_that_does_not_match_the_row(self) -> None:
        pinned = replace(self.handle, admitted_replay_sha256="0" * 64)
        self.assertEqual(pinned.run_id, self.handle.run_id)
        with self.assertRaisesRegex(ValueError, "does not match handle"):
            self.ledger.load_admitted_replay_v3(pinned)

    def test_load_refuses_a_non_canonical_stored_row(self) -> None:
        with self.ledger._connection() as connection:
            connection.execute("DROP TRIGGER admitted_replays_v3_no_update")
            stored = connection.execute(
                "SELECT admitted_json FROM admitted_replays_v3 WHERE run_id = ?",
                (self.handle.run_id,),
            ).fetchone()[0]
            connection.execute(
                "UPDATE admitted_replays_v3 SET admitted_json = ? WHERE run_id = ?",
                (json.dumps(json.loads(stored), indent=2), self.handle.run_id),
            )
        with self.assertRaises(ValueError) as caught:
            self.ledger.load_admitted_replay_v3(self.handle)
        self.assertIn("corrupt", str(caught.exception))
        self.assertIn("not canonical", str(caught.exception.__cause__))

    def test_load_refuses_a_missing_decision_row(self) -> None:
        with self.ledger._connection() as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("DROP TRIGGER decisions_no_delete")
            deleted = connection.execute(
                "DELETE FROM decisions WHERE decision_id = ?",
                (self.admitted.decision.decision_id,),
            )
            self.assertEqual(deleted.rowcount, 1)
        with self.assertRaisesRegex(ValueError, "Gate decision is not recorded"):
            self.ledger.load_admitted_replay_v3(self.handle)

    def test_load_refuses_a_handle_of_the_wrong_type(self) -> None:
        class HandleSubclass(AdmittedReplayHandleV1):
            pass

        subclass = HandleSubclass(
            *(getattr(self.handle, field.name) for field in fields(AdmittedReplayHandleV1))
        )
        plain = {
            "schema_version": 1,
            "run_id": self.handle.run_id,
            "admitted_replay_sha256": self.handle.admitted_replay_sha256,
        }
        for candidate in (self.admitted, plain, subclass):
            with self.subTest(type=type(candidate).__name__):
                with self.assertRaises(TypeError):
                    self.ledger.load_admitted_replay_v3(candidate)  # type: ignore[arg-type]

    def test_instance_shadowing_cannot_bypass_the_handle_checks(self) -> None:
        for name in ("_connection", "_admitted_replay_v3_values", "_require_decision_row"):
            setattr(self.ledger, name, lambda *args, **kwargs: None)
        self.assertEqual(
            Ledger.load_admitted_replay_v3(self.ledger, self.handle), self.admitted
        )
        with self.assertRaisesRegex(ValueError, "does not match handle"):
            Ledger.load_admitted_replay_v3(
                self.ledger, replace(self.handle, admitted_replay_sha256="0" * 64)
            )


class ReplayVerificationGrantHandleLoadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "ledger.sqlite3"
        self.ledger = Ledger(self.path)
        self.case = VerificationFixture()
        self.grant = self.case.admit()
        self.record_dependencies()
        self.ledger.record_admitted_replay_verification_grant_v1(self.grant)
        self.handle = ReplayVerificationGrantHandleV1(
            1, self.case.request.grant_id, self.grant.sha256
        )

    def record_dependencies(self) -> None:
        self.ledger.record_decision(self.case.admitted.decision)
        self.ledger.record_admitted_replay_v3(self.case.admitted)
        key = self.case.receipt.operation_key
        self.ledger.begin_operation(
            key,
            "replay_build_patched_apk_v1",
            self.case.receipt.expected_operation_input_sha256,
            "handle-load-fixture",
            retry_safe=False,
        )
        self.ledger.record_effect(key, "handle-load-fixture", self.case.completed_receipt)
        self.ledger.complete_operation(key, self.case.completed_receipt)
        self.ledger.record_decision(self.case.decision)

    def test_load_returns_recorded_authority_not_the_callers_object(self) -> None:
        loaded = self.ledger.load_admitted_replay_verification_grant_v1(self.handle)
        self.assertEqual(loaded, self.grant)
        self.assertIsNot(loaded, self.grant)
        self.assertIsNot(loaded.request, self.grant.request)
        self.assertIsNot(loaded.admitted_replay, self.grant.admitted_replay)
        self.assertEqual(loaded.sha256, self.handle.grant_sha256)

    def test_load_refuses_an_unrecorded_grant(self) -> None:
        unknown = replace(self.handle, grant_id="verification-grant-never-recorded")
        with self.assertRaisesRegex(ValueError, "authority is not recorded"):
            self.ledger.load_admitted_replay_verification_grant_v1(unknown)

    def test_load_refuses_a_sha256_pin_that_does_not_match_the_row(self) -> None:
        pinned = replace(self.handle, grant_sha256="0" * 64)
        self.assertEqual(pinned.grant_id, self.handle.grant_id)
        with self.assertRaisesRegex(ValueError, "does not match handle"):
            self.ledger.load_admitted_replay_verification_grant_v1(pinned)

    def test_load_refuses_a_non_canonical_stored_row(self) -> None:
        with self.ledger._connection() as connection:
            connection.execute(
                "DROP TRIGGER admitted_replay_verification_grants_v1_no_update"
            )
            stored = connection.execute(
                "SELECT grant_json FROM admitted_replay_verification_grants_v1 "
                "WHERE grant_id = ?",
                (self.handle.grant_id,),
            ).fetchone()[0]
            connection.execute(
                "UPDATE admitted_replay_verification_grants_v1 SET grant_json = ? "
                "WHERE grant_id = ?",
                (json.dumps(json.loads(stored), indent=2), self.handle.grant_id),
            )
        with self.assertRaises(ValueError) as caught:
            self.ledger.load_admitted_replay_verification_grant_v1(self.handle)
        self.assertIn("corrupt", str(caught.exception))
        self.assertIn("not canonical", str(caught.exception.__cause__))

    def test_load_refuses_a_missing_decision_row(self) -> None:
        with self.ledger._connection() as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("DROP TRIGGER decisions_no_delete")
            deleted = connection.execute(
                "DELETE FROM decisions WHERE decision_id = ?",
                (self.case.decision.decision_id,),
            )
            self.assertEqual(deleted.rowcount, 1)
        with self.assertRaisesRegex(ValueError, "Gate decision is not recorded"):
            self.ledger.load_admitted_replay_verification_grant_v1(self.handle)

    def test_load_refuses_an_incomplete_build_claim(self) -> None:
        with self.ledger._connection() as connection:
            updated = connection.execute(
                "UPDATE operation_claims SET status = 'pending', output_json = NULL "
                "WHERE operation_key = ?",
                (self.case.receipt.operation_key,),
            )
            self.assertEqual(updated.rowcount, 1)
        with self.assertRaisesRegex(ValueError, "build claim is not completed"):
            self.ledger.load_admitted_replay_verification_grant_v1(self.handle)

    def test_load_refuses_a_handle_of_the_wrong_type(self) -> None:
        class HandleSubclass(ReplayVerificationGrantHandleV1):
            pass

        subclass = HandleSubclass(
            *(
                getattr(self.handle, field.name)
                for field in fields(ReplayVerificationGrantHandleV1)
            )
        )
        plain = {
            "schema_version": 1,
            "grant_id": self.handle.grant_id,
            "grant_sha256": self.handle.grant_sha256,
        }
        for candidate in (self.grant, self.case.admitted, plain, subclass):
            with self.subTest(type=type(candidate).__name__):
                with self.assertRaises(TypeError):
                    self.ledger.load_admitted_replay_verification_grant_v1(candidate)  # type: ignore[arg-type]

    def test_instance_shadowing_cannot_bypass_the_handle_checks(self) -> None:
        for name in (
            "_connection",
            "_require_decision_row",
            "_require_admitted_replay_v3_row",
            "_require_completed_build_claim",
            "_verification_grant_values",
        ):
            setattr(self.ledger, name, lambda *args, **kwargs: None)
        self.assertEqual(
            Ledger.load_admitted_replay_verification_grant_v1(self.ledger, self.handle),
            self.grant,
        )
        with self.assertRaisesRegex(ValueError, "does not match handle"):
            Ledger.load_admitted_replay_verification_grant_v1(
                self.ledger, replace(self.handle, grant_sha256="0" * 64)
            )


if __name__ == "__main__":
    unittest.main()

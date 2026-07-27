import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
from pathlib import Path

from dfinsta_pipeline.ledger import Ledger
from dfinsta_pipeline.replay_contracts import AdmittedReplayV3, ReplayRunSpecV2
from tests.test_phase_b_replay_contracts import admit_v3, fixture_v3


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


if __name__ == "__main__":
    unittest.main()

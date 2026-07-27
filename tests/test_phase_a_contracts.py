import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path

from dfinsta_pipeline.contracts import (
    ArtifactRef,
    GateDecision,
    IntentSpec,
    ResolutionSpec,
    RunResult,
    RunSpec,
    StageInput,
    canonical_json,
    canonical_sha256,
)
from dfinsta_pipeline.ledger import Ledger
from dfinsta_pipeline.activities import operation_key
from dfinsta_pipeline.store import ContentStore


class ContractTests(unittest.TestCase):
    def test_canonical_hash_is_stable(self) -> None:
        spec = RunSpec(
            1,
            "run-1",
            "a" * 64,
            "b" * 64,
            "c" * 64,
            "d" * 64,
            "policy-1",
            "operator",
            60,
            "monolithic",
        )
        self.assertEqual(canonical_sha256(spec), canonical_sha256(asdict(spec)))

    def test_envelopes_reject_unknown_fields_and_versions(self) -> None:
        values = (
            (IntentSpec, {"schema_version": 1, "policy_revision": "policy-1", "intent_ids": []}),
            (
                ResolutionSpec,
                {"schema_version": 1, "target_sha256": "a" * 64, "operation_ids": []},
            ),
            (
                RunSpec,
                {
                    "schema_version": 1,
                    "run_id": "run-1",
                    "subject_sha256": "a" * 64,
                    "intent_sha256": "b" * 64,
                    "resolution_sha256": "c" * 64,
                    "executor_capability_sha256": "d" * 64,
                    "policy_revision": "policy-1",
                    "allowed_actor": "operator",
                    "gate_timeout_seconds": 60,
                    "apk_composition": "monolithic",
                    "crash_after_effect": False,
                    "apply_delay_seconds": 0,
                },
            ),
            (
                RunResult,
                {
                    "schema_version": 1,
                    "run_id": "run-1",
                    "state": "blocked",
                    "prepared": None,
                    "output": None,
                    "decision_id": None,
                },
            ),
        )
        for contract, data in values:
            with self.subTest(contract=contract.__name__, condition="unknown"):
                with self.assertRaises(ValueError):
                    contract.from_dict({**data, "unexpected": True})
            with self.subTest(contract=contract.__name__, condition="version"):
                with self.assertRaises(ValueError):
                    contract.from_dict({**data, "schema_version": 2})

    def test_contract_decoders_reject_ambiguous_json_types(self) -> None:
        artifact_data = {
            "schema_version": 1,
            "kind": "test",
            "sha256": "a" * 64,
            "size": 1,
            "uri": f"cas://sha256/{'a' * 64}",
            "producer_operation_id": "operation-1",
            "input_hashes": [],
        }
        with self.assertRaises((TypeError, ValueError)):
            ArtifactRef.from_dict({**artifact_data, "schema_version": True})
        with self.assertRaises((TypeError, ValueError)):
            ArtifactRef.from_dict({**artifact_data, "size": False})
        with self.assertRaises(TypeError):
            ResolutionSpec.from_dict(
                {"schema_version": 1, "target_sha256": "a" * 64, "operation_ids": "ab"}
            )

        run_data = {
            "schema_version": 1,
            "run_id": "run-1",
            "subject_sha256": "a" * 64,
            "intent_sha256": "b" * 64,
            "resolution_sha256": "c" * 64,
            "executor_capability_sha256": "d" * 64,
            "policy_revision": "policy-1",
            "allowed_actor": "operator",
            "gate_timeout_seconds": 60,
            "apk_composition": "monolithic",
            "crash_after_effect": False,
            "apply_delay_seconds": 0,
        }
        with self.assertRaises(TypeError):
            RunSpec.from_dict({**run_data, "crash_after_effect": 1})
        with self.assertRaises(ValueError):
            RunResult.from_dict(
                {
                    "schema_version": 1,
                    "run_id": "run-1",
                    "state": "unknown",
                    "prepared": None,
                    "output": None,
                    "decision_id": None,
                }
            )
        with self.assertRaises((TypeError, ValueError)):
            RunResult.from_dict(
                {
                    "schema_version": 1,
                    "run_id": "run-1",
                    "state": "blocked",
                    "prepared": None,
                    "output": None,
                    "decision_id": 7,
                }
            )
        with self.assertRaises(ValueError):
            RunResult.from_dict(
                {
                    "schema_version": 1,
                    "run_id": "run-1",
                    "state": "rejected",
                    "prepared": None,
                    "output": None,
                    "decision_id": None,
                }
            )

    def test_artifact_rejects_unknown_fields(self) -> None:
        data = {
            "schema_version": 1,
            "kind": "test",
            "sha256": "a" * 64,
            "size": 0,
            "uri": f"cas://sha256/{'a' * 64}",
            "producer_operation_id": "operation-1",
            "input_hashes": [],
            "unexpected": True,
        }
        with self.assertRaises(ValueError):
            ArtifactRef.from_dict(data)

    def test_stage_identity_changes_with_every_upstream_hash(self) -> None:
        spec = RunSpec(
            1,
            "run-chain",
            "a" * 64,
            "b" * 64,
            "c" * 64,
            "d" * 64,
            "policy-1",
            "operator",
            60,
            "monolithic",
        )
        admission = ArtifactRef(
            1, "phase-a-admission", "e" * 64, 1, f"cas://sha256/{'e' * 64}", "admit", ()
        )
        prepared = ArtifactRef(
            1, "phase-a-prepared", "f" * 64, 1, f"cas://sha256/{'f' * 64}", "prepare", ()
        )
        decision = GateDecision(
            1,
            "decision-1",
            "request-1",
            "operator",
            "run-chain",
            "phase-a-approval",
            canonical_sha256(spec),
            admission.sha256,
            prepared.sha256,
            "policy-1",
            "approve",
            "approved",
            "2026-07-26T00:00:00+00:00",
        )
        stage = StageInput(1, spec, (admission, prepared), decision)
        original = operation_key("phase_a_apply", stage)
        changes = (
            replace(stage, spec=replace(spec, intent_sha256="1" * 64)),
            replace(stage, upstream=(replace(admission, sha256="2" * 64, uri=f"cas://sha256/{'2' * 64}"), prepared)),
            replace(stage, upstream=(admission, replace(prepared, sha256="3" * 64, uri=f"cas://sha256/{'3' * 64}"))),
            replace(stage, decision=replace(decision, rationale="changed")),
        )
        for changed in changes:
            self.assertNotEqual(original, operation_key("phase_a_apply", changed))


class StoreAndLedgerTests(unittest.TestCase):
    def test_store_adopts_identical_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ContentStore(Path(directory) / "cas")
            first = store.put_bytes(
                kind="test", data=b"value", producer_operation_id="operation-1", input_hashes=()
            )
            second = store.put_bytes(
                kind="test", data=b"value", producer_operation_id="operation-1", input_hashes=()
            )
            self.assertEqual(first, second)
            self.assertEqual(store.read_bytes(first), b"value")

    def test_store_rejects_corrupted_adopted_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ContentStore(Path(directory) / "cas")
            reference = store.put_bytes(
                kind="test", data=b"value", producer_operation_id="operation-1", input_hashes=()
            )
            path = store.root / "sha256" / reference.sha256[:2] / reference.sha256
            path.chmod(0o644)
            path.write_bytes(b"corrupt")
            with self.assertRaises(ValueError):
                store.read_bytes(reference)

    def test_ledger_adopts_operation_and_rejects_decision_collision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ContentStore(root / "cas")
            ledger = Ledger(root / "ledger.sqlite3")
            self.assertIsNone(
                ledger.begin_operation(
                    "operation-1", "test", "a" * 64, "owner-1", retry_safe=True
                )
            )
            output = store.put_bytes(
                kind="test", data=b"value", producer_operation_id="operation-1", input_hashes=()
            )
            ledger.record_effect("operation-1", "owner-1", output)
            ledger.complete_operation("operation-1", output)
            self.assertEqual(
                ledger.begin_operation(
                    "operation-1", "test", "a" * 64, "owner-2", retry_safe=True
                ),
                output,
            )

            decision = GateDecision(
                1,
                "decision-1",
                "request-1",
                "operator",
                "run-1",
                "phase-a-approval",
                "a" * 64,
                "b" * 64,
                "c" * 64,
                "policy-1",
                "approve",
                "approved for test",
                "2026-07-26T00:00:00Z",
            )
            ledger.record_decision(decision)
            ledger.record_decision(decision)
            self.assertEqual(ledger.decision_count(), 1)
            changed = GateDecision(
                **{**asdict(decision), "decision": "reject"}
            )
            with self.assertRaises(ValueError):
                ledger.record_decision(changed)
            self.assertEqual(ledger.operation_event_count("operation-1"), 3)
            self.assertEqual(ledger.operation_event_count("operation-1", "effect"), 1)
            with ledger._connection() as connection:
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(
                        "UPDATE operation_events SET status = 'pending' WHERE operation_key = ?",
                        ("operation-1",),
                    )

    def test_ledger_allows_only_one_pending_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "ledger.sqlite3")

            def claim(owner: str) -> str:
                try:
                    result = ledger.begin_operation(
                        "operation-1", "test", "a" * 64, owner, retry_safe=False
                    )
                    return "claimed" if result is None else "adopted"
                except ValueError as error:
                    return str(error)

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = set(executor.map(claim, ("owner-1", "owner-2")))
            self.assertEqual(results, {"claimed", "Operation is already claimed"})

    def test_retry_safe_claim_fences_superseded_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "ledger.sqlite3")
            self.assertIsNone(
                ledger.begin_operation(
                    "operation-1", "test", "a" * 64, "owner-1", retry_safe=True
                )
            )
            with self.assertRaisesRegex(ValueError, "already claimed"):
                ledger.begin_operation(
                    "operation-1", "test", "a" * 64, "owner-2", retry_safe=False
                )
            self.assertIsNone(
                ledger.begin_operation(
                    "operation-1", "test", "a" * 64, "new-run-owner", retry_safe=True
                )
            )
            output = ArtifactRef(
                1,
                "test",
                "b" * 64,
                1,
                f"cas://sha256/{'b' * 64}",
                "operation-1",
                (),
            )
            with self.assertRaisesRegex(ValueError, "owner"):
                ledger.record_effect("operation-1", "owner-1", output)
            ledger.record_effect("operation-1", "new-run-owner", output)
            self.assertEqual(ledger.operation_event_count("operation-1", "effect"), 1)

    def test_legacy_events_backfill_current_claims(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.sqlite3"
            output = ArtifactRef(
                1,
                "test",
                "b" * 64,
                1,
                f"cas://sha256/{'b' * 64}",
                "operation-1",
                (),
            )
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    "CREATE TABLE operation_events ("
                    "event_id INTEGER PRIMARY KEY AUTOINCREMENT, operation_key TEXT NOT NULL, "
                    "kind TEXT NOT NULL, input_sha256 TEXT NOT NULL, status TEXT NOT NULL, "
                    "output_json TEXT)"
                )
                connection.execute(
                    "INSERT INTO operation_events "
                    "(operation_key, kind, input_sha256, status, output_json) "
                    "VALUES (?, ?, ?, 'completed', ?)",
                    ("operation-1", "test", "a" * 64, canonical_json(output)),
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                ledgers = list(executor.map(lambda _: Ledger(path), range(2)))
            ledger = ledgers[0]
            self.assertEqual(
                ledger.begin_operation(
                    "operation-1", "test", "a" * 64, "owner-2", retry_safe=True
                ),
                output,
            )
            self.assertEqual(ledger.operation_event_count("operation-1"), 1)


if __name__ == "__main__":
    unittest.main()

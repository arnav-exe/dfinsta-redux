from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .contracts import ArtifactRef, GateDecision, canonical_json


class Ledger:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS operation_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    input_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'effect', 'completed', 'quarantined')),
                    output_json TEXT
                );
                CREATE INDEX IF NOT EXISTS operation_events_key
                    ON operation_events(operation_key, event_id);
                CREATE TABLE IF NOT EXISTS operation_claims (
                    operation_key TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    input_sha256 TEXT NOT NULL,
                    owner_token TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'effect', 'completed', 'quarantined')),
                    output_json TEXT
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    decision_id TEXT PRIMARY KEY,
                    idempotency_id TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL,
                    gate_id TEXT NOT NULL,
                    subject_sha256 TEXT NOT NULL,
                    admission_sha256 TEXT NOT NULL,
                    prepared_sha256 TEXT NOT NULL,
                    policy_revision TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    issued_at TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS operation_events_no_update
                    BEFORE UPDATE ON operation_events BEGIN SELECT RAISE(ABORT, 'operation events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS operation_events_no_delete
                    BEFORE DELETE ON operation_events BEGIN SELECT RAISE(ABORT, 'operation events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS decisions_no_update
                    BEFORE UPDATE ON decisions BEGIN SELECT RAISE(ABORT, 'decisions are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS decisions_no_delete
                    BEFORE DELETE ON decisions BEGIN SELECT RAISE(ABORT, 'decisions are append-only'); END;
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def begin_operation(
        self,
        operation_key: str,
        kind: str,
        input_sha256: str,
        owner_token: str,
    ) -> ArtifactRef | None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT kind, input_sha256, owner_token, status, output_json "
                "FROM operation_claims WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO operation_claims VALUES (?, ?, ?, ?, 'pending', NULL)",
                    (operation_key, kind, input_sha256, owner_token),
                )
                connection.execute(
                    "INSERT INTO operation_events "
                    "(operation_key, kind, input_sha256, status, output_json) "
                    "VALUES (?, ?, ?, 'pending', NULL)",
                    (operation_key, kind, input_sha256),
                )
                return None
            if row[0] != kind or row[1] != input_sha256:
                raise ValueError("Operation key collision")
            if row[3] in {"effect", "completed"}:
                return ArtifactRef.from_dict(json.loads(row[4]))
            if row[3] == "quarantined":
                raise ValueError("Operation is quarantined")
            if row[2] != owner_token:
                raise ValueError("Operation is already claimed")
            return None

    def record_effect(
        self, operation_key: str, owner_token: str, output: ArtifactRef
    ) -> ArtifactRef:
        output_json = canonical_json(output)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT kind, input_sha256, owner_token, status, output_json "
                "FROM operation_claims WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
            if row is None:
                raise ValueError("Operation was not started")
            if output.producer_operation_id != operation_key:
                raise ValueError("Effect producer does not match operation")
            if row[3] == "effect" and row[4] == output_json:
                return output
            if row[2] != owner_token:
                raise ValueError("Operation effect owner does not match claim")
            if row[3] != "pending":
                raise ValueError("Operation cannot record effect")
            connection.execute(
                "UPDATE operation_claims SET status = 'effect', output_json = ? "
                "WHERE operation_key = ?",
                (output_json, operation_key),
            )
            connection.execute(
                "INSERT INTO operation_events "
                "(operation_key, kind, input_sha256, status, output_json) "
                "VALUES (?, ?, ?, 'effect', ?)",
                (operation_key, row[0], row[1], output_json),
            )
        return output

    def complete_operation(self, operation_key: str, output: ArtifactRef) -> ArtifactRef:
        output_json = canonical_json(output)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT kind, input_sha256, status, output_json FROM operation_claims "
                "WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
            if row and row[2] == "completed" and row[3] == output_json:
                return output
            if not row or row[2] != "effect" or row[3] != output_json:
                raise ValueError("Operation cannot be completed")
            connection.execute(
                "UPDATE operation_claims SET status = 'completed' WHERE operation_key = ?",
                (operation_key,),
            )
            connection.execute(
                "INSERT INTO operation_events "
                "(operation_key, kind, input_sha256, status, output_json) "
                "VALUES (?, ?, ?, 'completed', ?)",
                (operation_key, row[0], row[1], output_json),
            )
        return output

    def quarantine_operation(self, operation_key: str, owner_token: str) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT kind, input_sha256, owner_token, status, output_json "
                "FROM operation_claims WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
            if row and row[2] == owner_token and row[3] in {"pending", "effect"}:
                connection.execute(
                    "UPDATE operation_claims SET status = 'quarantined' WHERE operation_key = ?",
                    (operation_key,),
                )
                connection.execute(
                    "INSERT INTO operation_events "
                    "(operation_key, kind, input_sha256, status, output_json) "
                    "VALUES (?, ?, ?, 'quarantined', ?)",
                    (operation_key, row[0], row[1], row[4]),
                )

    def record_decision(self, decision: GateDecision) -> None:
        values = (
            decision.decision_id,
            decision.idempotency_id,
            decision.run_id,
            decision.gate_id,
            decision.subject_sha256,
            decision.admission_sha256,
            decision.prepared_sha256,
            decision.policy_revision,
            decision.actor,
            decision.decision,
            decision.rationale,
            decision.issued_at,
        )
        with self._connection() as connection:
            try:
                connection.execute(
                    "INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", values
                )
            except sqlite3.IntegrityError:
                row = connection.execute(
                    "SELECT decision_id, idempotency_id, run_id, gate_id, subject_sha256, "
                    "admission_sha256, prepared_sha256, policy_revision, actor, decision, rationale, issued_at "
                    "FROM decisions WHERE decision_id = ? OR idempotency_id = ?",
                    (decision.decision_id, decision.idempotency_id),
                ).fetchone()
                if row != values:
                    raise ValueError("Decision identity collision") from None

    def operation_status(self, operation_key: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT status FROM operation_events WHERE operation_key = ? "
                "ORDER BY event_id DESC LIMIT 1",
                (operation_key,),
            ).fetchone()
        return row[0] if row else None

    def operation_event_count(self, operation_key: str, status: str | None = None) -> int:
        with self._connection() as connection:
            if status is None:
                row = connection.execute(
                    "SELECT COUNT(*) FROM operation_events WHERE operation_key = ?", (operation_key,)
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) FROM operation_events WHERE operation_key = ? AND status = ?",
                    (operation_key, status),
                ).fetchone()
        return row[0]

    def operation_key_for_kind(self, kind: str) -> str:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT DISTINCT operation_key FROM operation_events WHERE kind = ?", (kind,)
            ).fetchall()
        if len(rows) != 1:
            raise ValueError(f"Expected one {kind} operation, found {len(rows)}")
        return rows[0][0]

    def has_decision(self, decision: GateDecision) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT decision_id, idempotency_id, run_id, gate_id, subject_sha256, "
                "admission_sha256, prepared_sha256, policy_revision, actor, decision, rationale, issued_at "
                "FROM decisions WHERE decision_id = ?",
                (decision.decision_id,),
            ).fetchone()
        return row == (
            decision.decision_id,
            decision.idempotency_id,
            decision.run_id,
            decision.gate_id,
            decision.subject_sha256,
            decision.admission_sha256,
            decision.prepared_sha256,
            decision.policy_revision,
            decision.actor,
            decision.decision,
            decision.rationale,
            decision.issued_at,
        )

    def decision_count(self) -> int:
        with self._connection() as connection:
            return connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]

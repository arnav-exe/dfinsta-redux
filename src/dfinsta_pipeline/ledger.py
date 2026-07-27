from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from .contracts import ArtifactRef, GateDecision, canonical_json

if TYPE_CHECKING:
    from .replay_contracts import AdmittedReplayV3


class Ledger:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            statements = (
                """CREATE TABLE IF NOT EXISTS operation_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    input_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'effect', 'completed', 'quarantined')),
                    output_json TEXT
                )""",
                """CREATE INDEX IF NOT EXISTS operation_events_key
                    ON operation_events(operation_key, event_id)""",
                """CREATE TABLE IF NOT EXISTS operation_claims (
                    operation_key TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    input_sha256 TEXT NOT NULL,
                    owner_token TEXT NOT NULL,
                    owner_attempt INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'effect', 'completed', 'quarantined')),
                    output_json TEXT
                )""",
                """CREATE TABLE IF NOT EXISTS decisions (
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
                )""",
                """CREATE TABLE IF NOT EXISTS admitted_replays_v3 (
                    run_id TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL CHECK (schema_version = 3),
                    admitted_replay_sha256 TEXT NOT NULL UNIQUE,
                    run_spec_sha256 TEXT NOT NULL UNIQUE,
                    replay_request_sha256 TEXT NOT NULL UNIQUE,
                    decision_id TEXT NOT NULL UNIQUE,
                    decision_sha256 TEXT NOT NULL,
                    toolchain_profile_sha256 TEXT NOT NULL,
                    admitted_json TEXT NOT NULL,
                    FOREIGN KEY (decision_id) REFERENCES decisions(decision_id)
                )""",
                """CREATE TRIGGER IF NOT EXISTS operation_events_no_update
                    BEFORE UPDATE ON operation_events BEGIN
                    SELECT RAISE(ABORT, 'operation events are append-only'); END""",
                """CREATE TRIGGER IF NOT EXISTS operation_events_no_delete
                    BEFORE DELETE ON operation_events BEGIN
                    SELECT RAISE(ABORT, 'operation events are append-only'); END""",
                """CREATE TRIGGER IF NOT EXISTS decisions_no_update
                    BEFORE UPDATE ON decisions BEGIN
                    SELECT RAISE(ABORT, 'decisions are append-only'); END""",
                """CREATE TRIGGER IF NOT EXISTS decisions_no_delete
                    BEFORE DELETE ON decisions BEGIN
                    SELECT RAISE(ABORT, 'decisions are append-only'); END""",
                """CREATE TRIGGER IF NOT EXISTS admitted_replays_v3_no_update
                    BEFORE UPDATE ON admitted_replays_v3 BEGIN
                    SELECT RAISE(ABORT, 'admitted replays v3 are append-only'); END""",
                """CREATE TRIGGER IF NOT EXISTS admitted_replays_v3_no_delete
                    BEFORE DELETE ON admitted_replays_v3 BEGIN
                    SELECT RAISE(ABORT, 'admitted replays v3 are append-only'); END""",
            )
            for statement in statements:
                connection.execute(statement)
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(operation_claims)").fetchall()
            }
            if "owner_attempt" not in columns:
                connection.execute(
                    "ALTER TABLE operation_claims ADD COLUMN owner_attempt INTEGER NOT NULL DEFAULT 0"
                )
            self._backfill_claims(connection)

    @staticmethod
    def _backfill_claims(connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT events.operation_key, events.kind, events.input_sha256, events.status, "
            "events.output_json FROM operation_events AS events "
            "JOIN (SELECT operation_key, MAX(event_id) AS event_id FROM operation_events "
            "GROUP BY operation_key) AS latest ON latest.event_id = events.event_id "
            "LEFT JOIN operation_claims AS claims ON claims.operation_key = events.operation_key "
            "WHERE claims.operation_key IS NULL"
        ).fetchall()
        for operation_key, kind, input_sha256, status, output_json in rows:
            if status in {"effect", "completed"}:
                if output_json is None:
                    raise ValueError("Legacy operation output is missing")
                output = ArtifactRef.from_dict(json.loads(output_json))
                if output.producer_operation_id != operation_key:
                    raise ValueError("Legacy operation producer does not match operation")
                output_json = canonical_json(output)
            elif output_json is not None:
                raise ValueError("Legacy operation has unexpected output")
            connection.execute(
                "INSERT INTO operation_claims "
                "(operation_key, kind, input_sha256, owner_token, owner_attempt, status, output_json) "
                "VALUES (?, ?, ?, 'legacy-migration', 0, ?, ?)",
                (operation_key, kind, input_sha256, status, output_json),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            for attempt in range(300):
                try:
                    connection.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as error:
                    if "locked" not in str(error).lower() or attempt == 299:
                        raise
                    time.sleep(0.01)
            return connection
        except BaseException:
            connection.close()
            raise

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
        *,
        retry_safe: bool,
    ) -> ArtifactRef | None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT kind, input_sha256, owner_token, owner_attempt, status, output_json "
                "FROM operation_claims WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO operation_claims "
                    "(operation_key, kind, input_sha256, owner_token, owner_attempt, status, output_json) "
                    "VALUES (?, ?, ?, ?, 1, 'pending', NULL)",
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
            if row[4] in {"effect", "completed"}:
                return ArtifactRef.from_dict(json.loads(row[5]))
            if row[4] == "quarantined":
                raise ValueError("Operation is quarantined")
            if row[2] != owner_token:
                if not retry_safe:
                    raise ValueError("Operation is already claimed")
                updated = connection.execute(
                    "UPDATE operation_claims SET owner_token = ?, owner_attempt = owner_attempt + 1 "
                    "WHERE operation_key = ? AND owner_attempt = ? AND status = 'pending'",
                    (owner_token, operation_key, row[3]),
                )
                if updated.rowcount != 1:
                    raise ValueError("Operation claim changed during takeover")
                connection.execute(
                    "INSERT INTO operation_events "
                    "(operation_key, kind, input_sha256, status, output_json) "
                    "VALUES (?, ?, ?, 'pending', NULL)",
                    (operation_key, kind, input_sha256),
                )
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

    @staticmethod
    def _decision_values(decision: GateDecision) -> tuple[object, ...]:
        return (
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

    @staticmethod
    def _admitted_replay_v3_values(
        admitted: AdmittedReplayV3, admitted_json: str
    ) -> tuple[object, ...]:
        return (
            admitted.run_spec.run_id,
            admitted.schema_version,
            admitted.sha256,
            admitted.run_spec_sha256,
            admitted.replay_request_sha256,
            admitted.decision.decision_id,
            admitted.decision_sha256,
            admitted.toolchain_profile_sha256,
            admitted_json,
        )

    @classmethod
    def _require_decision_row(
        cls, connection: sqlite3.Connection, decision: GateDecision
    ) -> None:
        row = connection.execute(
            "SELECT decision_id, idempotency_id, run_id, gate_id, subject_sha256, "
            "admission_sha256, prepared_sha256, policy_revision, actor, decision, rationale, "
            "issued_at FROM decisions WHERE decision_id = ?",
            (decision.decision_id,),
        ).fetchone()
        if row != cls._decision_values(decision):
            raise ValueError("Gate decision is not recorded")

    def record_admitted_replay_v3(self, admitted: AdmittedReplayV3) -> None:
        from .replay_contracts import AdmittedReplayV3

        if type(admitted) is not AdmittedReplayV3:
            raise TypeError("Admitted replay must be an exact AdmittedReplayV3")
        admitted_json = canonical_json(admitted)
        try:
            normalized = AdmittedReplayV3.from_dict(json.loads(admitted_json))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("Admitted replay is not canonical") from error
        if canonical_json(normalized) != admitted_json or normalized != admitted:
            raise ValueError("Admitted replay is not an exact canonical value")
        values = self._admitted_replay_v3_values(normalized, admitted_json)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_decision_row(connection, normalized.decision)
            try:
                connection.execute(
                    "INSERT INTO admitted_replays_v3 "
                    "(run_id, schema_version, admitted_replay_sha256, run_spec_sha256, "
                    "replay_request_sha256, decision_id, decision_sha256, "
                    "toolchain_profile_sha256, admitted_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    values,
                )
            except sqlite3.IntegrityError:
                row = connection.execute(
                    "SELECT run_id, schema_version, admitted_replay_sha256, run_spec_sha256, "
                    "replay_request_sha256, decision_id, decision_sha256, "
                    "toolchain_profile_sha256, admitted_json FROM admitted_replays_v3 "
                    "WHERE run_id = ? OR admitted_replay_sha256 = ? OR run_spec_sha256 = ? "
                    "OR replay_request_sha256 = ? OR decision_id = ?",
                    (values[0], values[2], values[3], values[4], values[5]),
                ).fetchone()
                if row != values:
                    raise ValueError("Admitted replay identity collision") from None

    def require_admitted_replay_v3(
        self, candidate: AdmittedReplayV3
    ) -> AdmittedReplayV3:
        from .replay_contracts import AdmittedReplayV3

        if type(candidate) is not AdmittedReplayV3:
            raise TypeError("Admitted replay must be an exact AdmittedReplayV3")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT run_id, schema_version, admitted_replay_sha256, run_spec_sha256, "
                "replay_request_sha256, decision_id, decision_sha256, "
                "toolchain_profile_sha256, admitted_json FROM admitted_replays_v3 "
                "WHERE run_id = ?",
                (candidate.run_spec.run_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Admitted replay authority is not recorded")
            try:
                decoded = json.loads(row[8])
                reconstructed = AdmittedReplayV3.from_dict(decoded)
                if canonical_json(reconstructed) != row[8]:
                    raise ValueError("Stored admitted replay is not canonical")
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise ValueError("Stored admitted replay is corrupt") from error
            reconstructed_values = self._admitted_replay_v3_values(reconstructed, row[8])
            candidate_values = self._admitted_replay_v3_values(
                candidate, canonical_json(candidate)
            )
            if row != reconstructed_values or row != candidate_values or reconstructed != candidate:
                raise ValueError("Admitted replay authority does not match candidate")
            self._require_decision_row(connection, reconstructed.decision)
        return reconstructed

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

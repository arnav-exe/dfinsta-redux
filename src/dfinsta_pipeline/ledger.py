from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Mapping

from .contracts import ArtifactRef, GateDecision, canonical_json, canonical_sha256

if TYPE_CHECKING:
    from .replay_contracts import (
        AdmittedReplayHandleV1,
        AdmittedReplayV3,
        AdmittedReplayVerificationGrantV1,
        ReplayVerificationGrantHandleV1,
        ReplayVerificationResumptionV1,
    )


#: What makes two recorded assessments *the same* assessment. Deliberately not
#: every column. `api_surface_sha256` is the digest of a file that embeds
#: `generated_at` and an absolute `decode_path`, so re-indexing the same decode
#: changes it while changing nothing that matters — comparing on it would make a
#: re-index conflict with itself, undoing one layer up the exact property the
#: operation key was keyed on `content_hash` to get. `manifest_sha256` is already
#: inside `input_sha256`; both are stored for a reader, neither is compared.
ASSESSMENT_IDENTITY_FIELDS = (
    "run_id",
    "operation_key",
    "input_sha256",
    "document_sha256",
    "policy_revision",
    "allowed_actor",
)


def assessment_identity(record: Mapping[str, Any]) -> dict[str, str]:
    """The subset of an assessment authority row that decides sameness."""
    return {name: str(record[name]) for name in ASSESSMENT_IDENTITY_FIELDS}


class Ledger:
    """The authority for artifacts, decisions and lineage. Append-only by trigger.

    `read_only=True` opens the same database through SQLite's `mode=ro` URI and
    refuses every write method. It exists for the trusted submission client,
    which must re-derive a gate subject from recorded state in order to know
    what it is asking a human to sign, and must be structurally unable to
    *create* the state it is checking. A promise not to write would be worth
    much less: the rejected standalone replay CLI failed exactly here, by
    self-asserting the values it should have been verifying.

    Two independent defences, on purpose. `_require_writable` gives a legible
    error at the call site, and the read-only connection makes SQLite refuse
    even if a future caller reaches past the guard. `RuntimeError` is chosen
    deliberately: it is already in the stage retry policy's non-retryable list,
    so a read-only ledger reached from an Activity fails closed and loudly
    rather than retrying until the budget is gone.
    """

    def __init__(self, path: Path, *, read_only: bool = False):
        if type(read_only) is not bool:
            raise TypeError("Ledger read_only must be a boolean")
        self.path = path
        self.read_only = read_only
        if read_only:
            # No mkdir and no schema statements: creating what you are about to
            # read is the failure mode this mode exists to make impossible. A
            # missing file is a real condition the caller must see, not one to
            # paper over by creating an empty ledger that answers "no decisions".
            if not self.path.is_file():
                raise FileNotFoundError(f"Ledger does not exist: {self.path}")
            with self._connection() as connection:
                connection.execute("SELECT COUNT(*) FROM decisions").fetchone()
            return
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
                """CREATE TABLE IF NOT EXISTS admitted_replay_verification_grants_v1 (
                    grant_id TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
                    grant_sha256 TEXT NOT NULL UNIQUE,
                    request_sha256 TEXT NOT NULL UNIQUE,
                    decision_id TEXT NOT NULL UNIQUE,
                    decision_sha256 TEXT NOT NULL,
                    admitted_replay_sha256 TEXT NOT NULL UNIQUE,
                    build_operation_key TEXT NOT NULL UNIQUE,
                    build_input_sha256 TEXT NOT NULL,
                    completed_receipt_ref_sha256 TEXT NOT NULL,
                    patched_apk_ref_sha256 TEXT NOT NULL,
                    grant_json TEXT NOT NULL,
                    FOREIGN KEY (decision_id) REFERENCES decisions(decision_id),
                    FOREIGN KEY (admitted_replay_sha256)
                        REFERENCES admitted_replays_v3(admitted_replay_sha256)
                )""",
                # Stage 4a's authority, keyed by run. `operation_claims` has no
                # `run_id` column and is indexed by content hash, so a client
                # holding only a run id — which is all a published `GateRequest`
                # gives it — cannot reach the recorded assessment operation. That
                # is exactly why `PortRunWorkflow`'s `phase-a-approval` gate is
                # unanswerable and deliberately unregistered. This row is the
                # bridge, modelled on `admitted_replays_v3` for the same reason.
                """CREATE TABLE IF NOT EXISTS recorded_assessments_v1 (
                    run_id TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
                    operation_key TEXT NOT NULL UNIQUE,
                    input_sha256 TEXT NOT NULL,
                    document_sha256 TEXT NOT NULL,
                    api_surface_sha256 TEXT NOT NULL,
                    manifest_sha256 TEXT NOT NULL,
                    policy_revision TEXT NOT NULL,
                    allowed_actor TEXT NOT NULL,
                    recorded_json TEXT NOT NULL
                )""",
                # The gate's OUTPUT, keyed by run, for the same reason
                # `recorded_assessments_v1` keys its input: the Workflow returns
                # the admitted reference in its result, and a result is not a
                # place a later caller can look it up. Without this row the
                # rulings a human made are admitted and then unreachable — the
                # same disconnection the gate itself had, one link along.
                """CREATE TABLE IF NOT EXISTS admitted_dispositions_v1 (
                    run_id TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
                    decision_id TEXT NOT NULL UNIQUE,
                    dispositions_sha256 TEXT NOT NULL,
                    dispositions_size INTEGER NOT NULL,
                    assessment_sha256 TEXT NOT NULL,
                    policy_revision TEXT NOT NULL,
                    admitted_json TEXT NOT NULL
                )""",
                """CREATE TRIGGER IF NOT EXISTS admitted_dispositions_v1_no_update
                    BEFORE UPDATE ON admitted_dispositions_v1 BEGIN
                    SELECT RAISE(ABORT, 'admitted dispositions v1 are append-only'); END""",
                """CREATE TRIGGER IF NOT EXISTS admitted_dispositions_v1_no_delete
                    BEFORE DELETE ON admitted_dispositions_v1 BEGIN
                    SELECT RAISE(ABORT, 'admitted dispositions v1 are append-only'); END""",
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
                """CREATE TRIGGER IF NOT EXISTS recorded_assessments_v1_no_update
                    BEFORE UPDATE ON recorded_assessments_v1 BEGIN
                    SELECT RAISE(ABORT, 'recorded assessments v1 are append-only'); END""",
                """CREATE TRIGGER IF NOT EXISTS recorded_assessments_v1_no_delete
                    BEFORE DELETE ON recorded_assessments_v1 BEGIN
                    SELECT RAISE(ABORT, 'recorded assessments v1 are append-only'); END""",
                """CREATE TRIGGER IF NOT EXISTS admitted_replay_verification_grants_v1_no_update
                    BEFORE UPDATE ON admitted_replay_verification_grants_v1 BEGIN
                    SELECT RAISE(ABORT, 'admitted replay verification grants v1 are append-only'); END""",
                """CREATE TRIGGER IF NOT EXISTS admitted_replay_verification_grants_v1_no_delete
                    BEFORE DELETE ON admitted_replay_verification_grants_v1 BEGIN
                    SELECT RAISE(ABORT, 'admitted replay verification grants v1 are append-only'); END""",
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

    def _require_writable(self) -> None:
        if self.read_only:
            raise RuntimeError("Ledger is open read-only")

    def _connect(self) -> sqlite3.Connection:
        if self.read_only:
            # `mode=ro` is the structural half of the guarantee. `journal_mode`
            # is deliberately not set: it writes the database header, and the
            # databases this opens are already in WAL mode anyway.
            connection = sqlite3.connect(
                f"file:{self.path}?mode=ro", uri=True, timeout=30
            )
            try:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=30000")
                return connection
            except BaseException:
                connection.close()
                raise
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
        self._require_writable()
        if type(owner_token) is not str or not owner_token:
            raise ValueError("Operation owner token must be a non-empty string")
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
                if row[2] != "" and not retry_safe:
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

    def release_pending_operation(self, operation_key: str, owner_token: str) -> None:
        self._require_writable()
        if type(owner_token) is not str or not owner_token:
            raise ValueError("Operation owner token must be a non-empty string")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT owner_token, status FROM operation_claims WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
            if row is None:
                raise ValueError("Operation was not started")
            if row[0] != owner_token:
                raise ValueError("Operation release owner does not match claim")
            if row[1] != "pending":
                raise ValueError("Only a pending operation can be released")
            connection.execute(
                "UPDATE operation_claims SET owner_token = '' WHERE operation_key = ?",
                (operation_key,),
            )

    def record_effect(
        self, operation_key: str, owner_token: str, output: ArtifactRef
    ) -> ArtifactRef:
        self._require_writable()
        if type(owner_token) is not str or not owner_token:
            raise ValueError("Operation owner token must be a non-empty string")
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
        self._require_writable()
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

    def require_completed_operation(
        self, operation_key: str, kind: str, input_sha256: str
    ) -> ArtifactRef:
        for value, label in (
            (operation_key, "Operation key"),
            (kind, "Operation kind"),
            (input_sha256, "Operation input SHA-256"),
        ):
            if type(value) is not str or not value:
                raise ValueError(f"{label} must be a non-empty string")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT kind, input_sha256, status, output_json FROM operation_claims "
                "WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
        if row is None:
            raise ValueError("Required completed operation is not recorded")
        if row[0] != kind or row[1] != input_sha256:
            raise ValueError("Required completed operation does not match exact claim")
        if row[2] != "completed" or row[3] is None:
            raise ValueError("Required operation is not completed")
        try:
            output = ArtifactRef.from_dict(json.loads(row[3]))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("Completed operation output is corrupt") from error
        if canonical_json(output) != row[3]:
            raise ValueError("Completed operation output is not canonical")
        if output.producer_operation_id != operation_key:
            raise ValueError("Completed operation producer does not match operation")
        return output

    def quarantine_operation(self, operation_key: str, owner_token: str) -> None:
        self._require_writable()
        if type(owner_token) is not str or not owner_token:
            raise ValueError("Operation owner token must be a non-empty string")
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
        self._require_writable()
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
        self._require_writable()
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

    def record_assessment_authority(self, record: Mapping[str, Any]) -> None:
        """File the run-keyed row that makes a recorded assessment reachable.

        The operation itself is recorded through the ordinary
        `begin_operation`/`record_effect`/`complete_operation` path; this only
        writes down which operation belongs to which run. Two separate facts, and
        keeping them separate matters: the operation is the derivation, this is
        the index into it, and a caller that had one without the other would be
        holding either bytes nobody vouched for or a promise with nothing behind
        it.

        Append-only by trigger, and `run_id` is the primary key, so a second
        assessment for the same run is refused rather than silently replacing the
        one a human may already have been shown.
        """
        self._require_writable()
        required = (
            "run_id",
            "operation_key",
            "input_sha256",
            "document_sha256",
            "api_surface_sha256",
            "manifest_sha256",
            "policy_revision",
            "allowed_actor",
        )
        missing = [name for name in required if not record.get(name)]
        if missing:
            raise ValueError(f"Assessment authority is missing {', '.join(missing)}")
        values = tuple(str(record[name]) for name in required)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT recorded_json FROM recorded_assessments_v1 WHERE run_id = ?",
                (values[0],),
            ).fetchone()
            # Stored whole, compared by identity. Two different jobs: a reader
            # wants everything that was recorded, while *sameness* must ignore
            # the fields that move when nothing meaningful has. Storing only the
            # identity subset would make the reader silently return less than the
            # columns beside it.
            payload = canonical_json({name: record[name] for name in required})
            if existing is not None:
                # Idempotent for the same assessment — a retried Activity must
                # not fail — and a hard error for a different one, because two
                # different assessments for one run is the state where nobody can
                # say which the human saw.
                if assessment_identity(json.loads(existing[0])) != assessment_identity(record):
                    raise ValueError("A different assessment is already recorded for this run")
                connection.execute("COMMIT")
                return
            connection.execute(
                "INSERT INTO recorded_assessments_v1 (run_id, schema_version, operation_key, "
                "input_sha256, document_sha256, api_surface_sha256, manifest_sha256, "
                "policy_revision, allowed_actor, recorded_json) VALUES (?,1,?,?,?,?,?,?,?,?)",
                (*values, payload),
            )
            connection.execute("COMMIT")

    def record_admitted_dispositions(self, record: Mapping[str, Any]) -> None:
        """File the run-keyed row that makes admitted rulings reachable.

        Written by the admitting Activity once `validate_submission` has passed,
        so the row exists only for rulings that were actually authorised. Keyed
        by run and append-only, so a run cannot silently gain a second set of
        rulings — the state in which nobody could say which the human made.
        """
        self._require_writable()
        required = (
            "run_id",
            "decision_id",
            "dispositions_sha256",
            "dispositions_size",
            "assessment_sha256",
            "policy_revision",
        )
        missing = [name for name in required if record.get(name) in (None, "")]
        if missing:
            raise ValueError(f"Admitted dispositions are missing {', '.join(missing)}")
        payload = canonical_json({name: record[name] for name in required})
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT admitted_json FROM admitted_dispositions_v1 WHERE run_id = ?",
                (str(record["run_id"]),),
            ).fetchone()
            if existing is not None:
                if existing[0] != payload:
                    raise ValueError("Different dispositions are already admitted for this run")
                connection.execute("COMMIT")
                return
            connection.execute(
                "INSERT INTO admitted_dispositions_v1 (run_id, schema_version, decision_id, "
                "dispositions_sha256, dispositions_size, assessment_sha256, policy_revision, "
                "admitted_json) VALUES (?,1,?,?,?,?,?,?)",
                (
                    str(record["run_id"]),
                    str(record["decision_id"]),
                    str(record["dispositions_sha256"]),
                    int(record["dispositions_size"]),
                    str(record["assessment_sha256"]),
                    str(record["policy_revision"]),
                    payload,
                ),
            )
            connection.execute("COMMIT")

    def admitted_dispositions_for_run(self, run_id: str) -> dict[str, Any]:
        """The admitted rulings for a run, for a caller holding only a run id.

        Same role and same warning as the other two `..._for_run` accessors: it
        returns coordinates, so the caller still fetches the document from CAS by
        digest and size and the store's own verification is not bypassed.
        """
        if type(run_id) is not str:
            raise TypeError("Dispositions run id must be a string")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT admitted_json FROM admitted_dispositions_v1 WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise ValueError("No dispositions are admitted for this run")
        return json.loads(row[0])

    def recorded_assessment_for_run(self, run_id: str) -> dict[str, Any]:
        """The recorded assessment row for a run, for a caller with no prior view.

        Same role and same warning as :meth:`admitted_replay_handle_for_run`: the
        trusted submission client is handed a run id and nothing else, and the
        operation tables are keyed by content hash, so without this row the gate
        would be unanswerable in exactly the way `phase-a-approval` is. It returns
        the *coordinates* — the operation key and the input hash — so the caller
        still reaches the `ArtifactRef` through `require_completed_operation` and
        the checks there are not bypassed for anybody.

        Not for stages. A stage that reached for this would be trusting a run id
        instead of the handle its caller gave it.
        """
        if type(run_id) is not str:
            raise TypeError("Assessment run id must be a string")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT recorded_json FROM recorded_assessments_v1 WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise ValueError("Assessment authority is not recorded")
        return json.loads(row[0])

    def admitted_replay_handle_for_run(self, run_id: str) -> AdmittedReplayHandleV1:
        """Return the recorded handle for a run, for a caller with no prior view.

        Every stage receives its handle from the Workflow and loads through
        :meth:`load_admitted_replay_v3`, whose pin check preserves "the caller's
        view equals the ledger's view". The trusted submission client has no
        prior view to preserve -- it is *establishing* one in order to re-derive
        a gate subject, and the run id is all a published `GateRequest` gives
        it. So this returns a handle rather than the authority itself: the
        client still loads through the one existing path, and the pin check,
        vacuous only for this caller, is not bypassed for anybody else.

        Not for stages. A stage that reached for this would be discarding the
        handle the Workflow gave it and trusting a run id instead.
        """

        from .replay_contracts import AdmittedReplayHandleV1

        if type(run_id) is not str:
            raise TypeError("Admitted replay run id must be a string")
        with Ledger._connection(self) as connection:
            row = connection.execute(
                "SELECT run_id, admitted_replay_sha256 FROM admitted_replays_v3 "
                "WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise ValueError("Admitted replay authority is not recorded")
        return AdmittedReplayHandleV1(1, row[0], row[1])

    def load_admitted_replay_v3(
        self, handle: AdmittedReplayHandleV1
    ) -> AdmittedReplayV3:
        from .replay_contracts import AdmittedReplayHandleV1, AdmittedReplayV3

        if type(handle) is not AdmittedReplayHandleV1:
            raise TypeError("Admitted replay handle must be an exact AdmittedReplayHandleV1")
        with Ledger._connection(self) as connection:
            row = connection.execute(
                "SELECT run_id, schema_version, admitted_replay_sha256, run_spec_sha256, "
                "replay_request_sha256, decision_id, decision_sha256, "
                "toolchain_profile_sha256, admitted_json FROM admitted_replays_v3 "
                "WHERE run_id = ?",
                (handle.run_id,),
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
            reconstructed_values = Ledger._admitted_replay_v3_values(reconstructed, row[8])
            if row != reconstructed_values:
                raise ValueError("Admitted replay authority does not match its recorded row")
            if row[2] != handle.admitted_replay_sha256:
                raise ValueError("Admitted replay authority does not match handle")
            if reconstructed.sha256 != handle.admitted_replay_sha256:
                raise ValueError("Stored admitted replay does not match handle")
            Ledger._require_decision_row(connection, reconstructed.decision)
        return reconstructed

    @staticmethod
    def _verification_grant_values(
        grant: AdmittedReplayVerificationGrantV1,
        grant_json: str,
        build_input_sha256: str,
    ) -> tuple[object, ...]:
        return (
            grant.request.grant_id,
            grant.schema_version,
            grant.sha256,
            grant.request.sha256,
            grant.decision.decision_id,
            canonical_sha256(grant.decision),
            grant.admitted_replay.sha256,
            grant.request.completed_patched_apk_receipt.producer_operation_id,
            build_input_sha256,
            canonical_sha256(grant.request.completed_patched_apk_receipt),
            canonical_sha256(grant.request.patched_apk),
            grant_json,
        )

    @staticmethod
    def _require_completed_build_claim(
        connection: sqlite3.Connection,
        grant: AdmittedReplayVerificationGrantV1,
    ) -> str:
        completed_receipt = grant.request.completed_patched_apk_receipt
        receipt = grant.patched_apk_receipt
        if (
            receipt.operation_key != receipt.expected_operation_key
            or completed_receipt.producer_operation_id != receipt.expected_operation_key
        ):
            raise ValueError("Replay build receipt operation identity is invalid")
        row = connection.execute(
            "SELECT kind, input_sha256, status, output_json FROM operation_claims "
            "WHERE operation_key = ?",
            (completed_receipt.producer_operation_id,),
        ).fetchone()
        if row is None or row[0] != "replay_build_patched_apk_v1":
            raise ValueError("Completed replay build claim is not recorded")
        if row[1] != receipt.expected_operation_input_sha256:
            raise ValueError("Replay build claim input does not match receipt")
        if row[2] != "completed" or row[3] is None:
            raise ValueError("Replay build claim is not completed")
        try:
            output = ArtifactRef.from_dict(json.loads(row[3]))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("Completed replay build claim is corrupt") from error
        if canonical_json(output) != row[3] or output != completed_receipt:
            raise ValueError("Completed replay build claim output does not match grant")
        events = connection.execute(
            "SELECT kind, input_sha256, status, output_json FROM operation_events "
            "WHERE operation_key = ? ORDER BY event_id",
            (receipt.expected_operation_key,),
        ).fetchall()
        if not events or events[-1] != row:
            raise ValueError("Replay build claim does not match append-only events")
        statuses = tuple(event[2] for event in events)
        if (
            len(statuses) < 3
            or any(status != "pending" for status in statuses[:-2])
            or statuses[-2:] != ("effect", "completed")
        ):
            raise ValueError("Replay build event history is invalid")
        if any(
            event[0] != row[0]
            or event[1] != row[1]
            or (event[2] == "pending") != (event[3] is None)
            or (event[2] != "pending" and event[3] != row[3])
            for event in events
        ):
            raise ValueError("Replay build event history does not match claim")
        return row[1]

    @classmethod
    def _require_admitted_replay_v3_row(
        cls,
        connection: sqlite3.Connection,
        candidate: AdmittedReplayV3,
    ) -> None:
        admitted_json = canonical_json(candidate)
        row = connection.execute(
            "SELECT run_id, schema_version, admitted_replay_sha256, run_spec_sha256, "
            "replay_request_sha256, decision_id, decision_sha256, "
            "toolchain_profile_sha256, admitted_json FROM admitted_replays_v3 "
            "WHERE admitted_replay_sha256 = ?",
            (candidate.sha256,),
        ).fetchone()
        if row is None:
            raise ValueError("Admitted replay authority is not recorded")
        values = cls._admitted_replay_v3_values(candidate, admitted_json)
        if row != values:
            raise ValueError("Admitted replay authority does not match verification grant")
        cls._require_decision_row(connection, candidate.decision)

    def record_admitted_replay_verification_grant_v1(
        self, grant: AdmittedReplayVerificationGrantV1
    ) -> None:
        self._require_writable()
        from .replay_contracts import AdmittedReplayVerificationGrantV1

        if type(grant) is not AdmittedReplayVerificationGrantV1:
            raise TypeError(
                "Verification grant must be an exact AdmittedReplayVerificationGrantV1"
            )
        grant_json = canonical_json(grant)
        try:
            normalized = AdmittedReplayVerificationGrantV1.from_dict(
                json.loads(grant_json)
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("Verification grant is not canonical") from error
        if canonical_json(normalized) != grant_json or normalized != grant:
            raise ValueError("Verification grant is not an exact canonical value")

        with Ledger._connection(self) as connection:
            connection.execute("BEGIN IMMEDIATE")
            Ledger._require_decision_row(connection, normalized.decision)
            Ledger._require_admitted_replay_v3_row(
                connection, normalized.admitted_replay
            )
            build_input_sha256 = Ledger._require_completed_build_claim(
                connection, normalized
            )
            values = Ledger._verification_grant_values(
                normalized, grant_json, build_input_sha256
            )
            try:
                connection.execute(
                    "INSERT INTO admitted_replay_verification_grants_v1 "
                    "(grant_id, schema_version, grant_sha256, request_sha256, decision_id, "
                    "decision_sha256, admitted_replay_sha256, build_operation_key, "
                    "build_input_sha256, completed_receipt_ref_sha256, "
                    "patched_apk_ref_sha256, grant_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    values,
                )
            except sqlite3.IntegrityError:
                row = connection.execute(
                    "SELECT grant_id, schema_version, grant_sha256, request_sha256, "
                    "decision_id, decision_sha256, admitted_replay_sha256, "
                    "build_operation_key, build_input_sha256, completed_receipt_ref_sha256, "
                    "patched_apk_ref_sha256, grant_json "
                    "FROM admitted_replay_verification_grants_v1 "
                    "WHERE grant_id = ? OR grant_sha256 = ? OR request_sha256 = ? "
                    "OR decision_id = ? OR admitted_replay_sha256 = ? "
                    "OR build_operation_key = ?",
                    (values[0], values[2], values[3], values[4], values[6], values[7]),
                ).fetchone()
                if row != values:
                    raise ValueError("Verification grant identity collision") from None

    def require_admitted_replay_verification_grant_v1(
        self, candidate: AdmittedReplayVerificationGrantV1
    ) -> AdmittedReplayVerificationGrantV1:
        from .replay_contracts import AdmittedReplayVerificationGrantV1

        if type(candidate) is not AdmittedReplayVerificationGrantV1:
            raise TypeError(
                "Verification grant must be an exact AdmittedReplayVerificationGrantV1"
            )
        with Ledger._connection(self) as connection:
            row = connection.execute(
                "SELECT grant_id, schema_version, grant_sha256, request_sha256, "
                "decision_id, decision_sha256, admitted_replay_sha256, "
                "build_operation_key, build_input_sha256, completed_receipt_ref_sha256, "
                "patched_apk_ref_sha256, grant_json "
                "FROM admitted_replay_verification_grants_v1 WHERE grant_id = ?",
                (candidate.request.grant_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Replay verification grant authority is not recorded")
            try:
                reconstructed = AdmittedReplayVerificationGrantV1.from_dict(
                    json.loads(row[11])
                )
                if canonical_json(reconstructed) != row[11]:
                    raise ValueError("Stored verification grant is not canonical")
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise ValueError("Stored verification grant is corrupt") from error
            Ledger._require_decision_row(connection, reconstructed.decision)
            Ledger._require_admitted_replay_v3_row(
                connection, reconstructed.admitted_replay
            )
            build_input_sha256 = Ledger._require_completed_build_claim(
                connection, reconstructed
            )
            reconstructed_values = Ledger._verification_grant_values(
                reconstructed, row[11], build_input_sha256
            )
            candidate_values = Ledger._verification_grant_values(
                candidate, canonical_json(candidate), build_input_sha256
            )
            if (
                row != reconstructed_values
                or row != candidate_values
                or reconstructed != candidate
            ):
                raise ValueError("Replay verification grant authority does not match candidate")
        return reconstructed

    def admitted_replay_verification_resumption(
        self, grant_id: str
    ) -> ReplayVerificationResumptionV1 | None:
        """The recorded answer to a run's verification gate, or None if unanswered.

        Returning None rather than raising is the point: "no grant yet" is the
        ordinary state of every run before its gate closes, and a caller that had
        to catch an exception to learn it would be using a refusal as control
        flow. A missing row is not a refusal here.

        Takes the derived `grant_id` rather than a run id because the derivation
        lives in `replay_gate` and the ledger does not import it. Every caller
        gets the id from `replay_gate.derived_identifier`, so the ledger stays a
        store and the naming rule stays in one place.

        This exists because the grant is single-shot in a way that has no other
        exit. Once a grant is recorded, `record_admitted_replay_verification_grant_v1`
        refuses any *different* decision for the same run with an identity
        collision, and the Workflow validator refuses any decision issued before
        the gate it is answering -- so after a re-drive the journalled decision is
        too old and a fresh one collides. Both doors are shut. The way out is not
        to widen either check but to stop asking a question the ledger already
        records the answer to.
        """

        from .replay_contracts import (
            ReplayVerificationGrantHandleV1,
            ReplayVerificationResumptionV1,
        )

        if type(grant_id) is not str:
            raise TypeError("Verification grant id must be a string")
        with Ledger._connection(self) as connection:
            row = connection.execute(
                "SELECT grant_id, grant_sha256, decision_id FROM "
                "admitted_replay_verification_grants_v1 WHERE grant_id = ?",
                (grant_id,),
            ).fetchone()
        if row is None:
            return None
        return ReplayVerificationResumptionV1(
            1, ReplayVerificationGrantHandleV1(1, row[0], row[1]), row[2]
        )

    def load_admitted_replay_verification_grant_v1(
        self, handle: ReplayVerificationGrantHandleV1
    ) -> AdmittedReplayVerificationGrantV1:
        from .replay_contracts import (
            AdmittedReplayVerificationGrantV1,
            ReplayVerificationGrantHandleV1,
        )

        if type(handle) is not ReplayVerificationGrantHandleV1:
            raise TypeError(
                "Verification grant handle must be an exact ReplayVerificationGrantHandleV1"
            )
        with Ledger._connection(self) as connection:
            row = connection.execute(
                "SELECT grant_id, schema_version, grant_sha256, request_sha256, "
                "decision_id, decision_sha256, admitted_replay_sha256, "
                "build_operation_key, build_input_sha256, completed_receipt_ref_sha256, "
                "patched_apk_ref_sha256, grant_json "
                "FROM admitted_replay_verification_grants_v1 WHERE grant_id = ?",
                (handle.grant_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Replay verification grant authority is not recorded")
            try:
                reconstructed = AdmittedReplayVerificationGrantV1.from_dict(
                    json.loads(row[11])
                )
                if canonical_json(reconstructed) != row[11]:
                    raise ValueError("Stored verification grant is not canonical")
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise ValueError("Stored verification grant is corrupt") from error
            Ledger._require_decision_row(connection, reconstructed.decision)
            Ledger._require_admitted_replay_v3_row(
                connection, reconstructed.admitted_replay
            )
            build_input_sha256 = Ledger._require_completed_build_claim(
                connection, reconstructed
            )
            reconstructed_values = Ledger._verification_grant_values(
                reconstructed, row[11], build_input_sha256
            )
            if row != reconstructed_values:
                raise ValueError(
                    "Replay verification grant authority does not match its recorded row"
                )
            if row[2] != handle.grant_sha256:
                raise ValueError("Replay verification grant authority does not match handle")
            if reconstructed.sha256 != handle.grant_sha256:
                raise ValueError("Stored verification grant does not match handle")
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

import ast
import hashlib
import inspect
import multiprocessing
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from dfinsta_pipeline.contracts import ArtifactRef
from dfinsta_pipeline.ledger import Ledger
from dfinsta_pipeline.replay_contracts import AdmittedReplayHandleV1
from tests.test_phase_b_replay_contracts import admit_v3, fixture_v3
from tests.test_phase_b_verification_grant import VerificationFixture

READ_ONLY_MESSAGE = "Ledger is open read-only"

# Driven from a list on purpose: a write method added later without a guard is
# only visible if the roster is a value the tests can compare against, rather
# than a set of hand-written cases nobody remembers to extend.
WRITE_METHOD_NAMES = (
    "begin_operation",
    "release_pending_operation",
    "record_effect",
    "complete_operation",
    "quarantine_operation",
    "record_decision",
    "record_admitted_replay_v3",
    "record_admitted_replay_verification_grant_v1",
)

# `__init__` runs the schema statements and is the one method allowed to write
# before `read_only` has been honoured (it returns before them when read-only);
# `_backfill_claims` is a static helper called only from `__init__`.
GUARD_EXEMPT_METHOD_NAMES = frozenset({"__init__", "_backfill_claims"})

WRITE_SQL_MARKERS = ("INSERT INTO", "BEGIN IMMEDIATE")

OPERATION_KEY = "read-only-operation-1"
OPERATION_KIND = "read_only_probe_v1"
OPERATION_INPUT_SHA256 = "a" * 64
OWNER_TOKEN = "read-only-owner-1"


def artifact_ref(producer_operation_id: str, payload: bytes) -> ArtifactRef:
    digest = hashlib.sha256(payload).hexdigest()
    return ArtifactRef(
        1,
        "read-only-probe",
        digest,
        len(payload),
        f"cas://sha256/{digest}",
        producer_operation_id,
        (),
    )


def ledger_source_and_class_node() -> tuple[str, ast.ClassDef]:
    source_file = inspect.getsourcefile(Ledger)
    assert source_file is not None
    source = Path(source_file).read_text(encoding="utf-8")
    for node in ast.parse(source).body:
        if isinstance(node, ast.ClassDef) and node.name == "Ledger":
            return source, node
    raise AssertionError("Ledger class definition was not found in its own source")


def is_guard_statement(statement: ast.stmt) -> bool:
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and not statement.value.args
        and not statement.value.keywords
        and isinstance(statement.value.func, ast.Attribute)
        and statement.value.func.attr == "_require_writable"
        and isinstance(statement.value.func.value, ast.Name)
        and statement.value.func.value.id == "self"
    )


def body_without_docstring(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.stmt]:
    body = list(node.body)
    first = body[0]
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and type(first.value.value) is str
    ):
        return body[1:]
    return body


def decision_row(decision_id: str, idempotency_id: str, rationale: str) -> tuple:
    return (
        decision_id,
        idempotency_id,
        "run-competing",
        "gate-competing",
        "b" * 64,
        "c" * 64,
        "d" * 64,
        "policy-competing",
        "operator",
        "approve",
        rationale,
        "2026-08-01T00:00:00Z",
    )


def competing_writer(
    path: str, committed: object, proceed: object, holding: object, release: object
) -> None:  # pragma: no cover - runs in a spawned child process
    """Commit a row, keep the connection open, then sit on the write lock.

    Module level and argument driven because the "spawn" start method pickles
    this by qualified name; a closure or a bound method would not survive.

    The connection is deliberately never closed between the phases. Closing it
    would checkpoint the WAL into the main database file, which is precisely
    the state in which a stale reader still looks correct.
    """

    connection = sqlite3.connect(path, timeout=30)
    try:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            decision_row(
                "decision-committed-elsewhere",
                "request-committed-elsewhere",
                "committed in a competing process, still in the WAL",
            ),
        )
        connection.commit()
        committed.set()  # type: ignore[attr-defined]
        proceed.wait(60)  # type: ignore[attr-defined]

        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            decision_row(
                "decision-held-by-other-process",
                "request-held-by-other-process",
                "uncommitted in a competing process",
            ),
        )
        holding.set()  # type: ignore[attr-defined]
        release.wait(60)  # type: ignore[attr-defined]
        connection.rollback()
    finally:
        connection.close()


class ReadOnlyLedgerFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "ledger.sqlite3"
        self.writable = Ledger(self.path)
        self.admitted = admit_v3(fixture_v3())
        self.output = artifact_ref(OPERATION_KEY, b"read-only ledger probe output")
        self.writable.begin_operation(
            OPERATION_KEY,
            OPERATION_KIND,
            OPERATION_INPUT_SHA256,
            OWNER_TOKEN,
            retry_safe=False,
        )
        self.writable.record_effect(OPERATION_KEY, OWNER_TOKEN, self.output)
        self.writable.complete_operation(OPERATION_KEY, self.output)
        self.writable.record_decision(self.admitted.decision)
        self.writable.record_admitted_replay_v3(self.admitted)
        self.ledger = Ledger(self.path, read_only=True)


class ReadOnlyLedgerReadTests(ReadOnlyLedgerFixture):
    def test_read_only_flag_is_exposed_and_writable_is_the_default(self) -> None:
        self.assertIs(self.ledger.read_only, True)
        self.assertIs(self.writable.read_only, False)
        self.assertIs(Ledger(self.path).read_only, False)

    def test_every_read_method_answers_from_a_read_only_ledger(self) -> None:
        self.assertEqual(self.ledger.operation_status(OPERATION_KEY), "completed")
        self.assertEqual(self.ledger.operation_event_count(OPERATION_KEY), 3)
        self.assertEqual(
            self.ledger.operation_event_count(OPERATION_KEY, "completed"), 1
        )
        self.assertEqual(self.ledger.operation_key_for_kind(OPERATION_KIND), OPERATION_KEY)
        self.assertIs(self.ledger.has_decision(self.admitted.decision), True)
        self.assertEqual(self.ledger.decision_count(), 1)
        self.assertEqual(
            self.ledger.require_completed_operation(
                OPERATION_KEY, OPERATION_KIND, OPERATION_INPUT_SHA256
            ),
            self.output,
        )
        self.assertEqual(
            self.ledger.admitted_replay_handle_for_run(self.admitted.run_spec.run_id),
            AdmittedReplayHandleV1(
                1, self.admitted.run_spec.run_id, self.admitted.sha256
            ),
        )

    def test_read_only_answers_are_identical_to_the_writable_ledgers(self) -> None:
        run_id = self.admitted.run_spec.run_id
        for name, arguments in (
            ("operation_status", (OPERATION_KEY,)),
            ("operation_event_count", (OPERATION_KEY,)),
            ("operation_event_count", (OPERATION_KEY, "pending")),
            ("operation_key_for_kind", (OPERATION_KIND,)),
            ("has_decision", (self.admitted.decision,)),
            ("decision_count", ()),
            (
                "require_completed_operation",
                (OPERATION_KEY, OPERATION_KIND, OPERATION_INPUT_SHA256),
            ),
            ("admitted_replay_handle_for_run", (run_id,)),
            ("require_admitted_replay_v3", (self.admitted,)),
        ):
            with self.subTest(method=name, arguments=arguments):
                self.assertEqual(
                    getattr(self.ledger, name)(*arguments),
                    getattr(self.writable, name)(*arguments),
                )

    def test_read_failures_are_the_same_failures_as_when_writable(self) -> None:
        for name, arguments, error, message in (
            ("operation_key_for_kind", ("kind-that-was-never-run",), ValueError, "found 0"),
            (
                "require_completed_operation",
                ("operation-never-started", OPERATION_KIND, OPERATION_INPUT_SHA256),
                ValueError,
                "is not recorded",
            ),
            (
                "admitted_replay_handle_for_run",
                ("run-never-admitted",),
                ValueError,
                "Admitted replay authority is not recorded",
            ),
            ("admitted_replay_handle_for_run", (None,), TypeError, "must be a string"),
        ):
            with self.subTest(method=name, arguments=arguments):
                with self.assertRaisesRegex(error, message):
                    getattr(self.ledger, name)(*arguments)
                with self.assertRaisesRegex(error, message):
                    getattr(self.writable, name)(*arguments)

    def test_reads_see_rows_a_writer_added_after_the_read_only_open(self) -> None:
        self.assertEqual(self.ledger.decision_count(), 1)
        later = replace(
            self.admitted.decision,
            decision_id="decision-recorded-later",
            idempotency_id="request-recorded-later",
        )
        self.writable.record_decision(later)
        self.assertEqual(self.ledger.decision_count(), 2)
        self.assertIs(self.ledger.has_decision(later), True)


class ReadOnlyLedgerWriteGuardTests(ReadOnlyLedgerFixture):
    def write_call_arguments(self) -> dict[str, tuple[tuple, dict]]:
        grant = VerificationFixture().admit()
        return {
            "begin_operation": (
                ("operation-2", OPERATION_KIND, "b" * 64, OWNER_TOKEN),
                {"retry_safe": False},
            ),
            "release_pending_operation": ((OPERATION_KEY, OWNER_TOKEN), {}),
            "record_effect": ((OPERATION_KEY, OWNER_TOKEN, self.output), {}),
            "complete_operation": ((OPERATION_KEY, self.output), {}),
            "quarantine_operation": ((OPERATION_KEY, OWNER_TOKEN), {}),
            "record_decision": ((self.admitted.decision,), {}),
            "record_admitted_replay_v3": ((self.admitted,), {}),
            "record_admitted_replay_verification_grant_v1": ((grant,), {}),
        }

    def test_require_writable_raises_only_when_read_only(self) -> None:
        self.assertIsNone(self.writable._require_writable())
        with self.assertRaises(RuntimeError) as caught:
            self.ledger._require_writable()
        self.assertEqual(str(caught.exception), READ_ONLY_MESSAGE)

    def test_every_write_method_refuses_on_a_read_only_ledger(self) -> None:
        arguments = self.write_call_arguments()
        self.assertEqual(sorted(arguments), sorted(WRITE_METHOD_NAMES))
        for name in WRITE_METHOD_NAMES:
            with self.subTest(method=name):
                self.assertTrue(callable(getattr(Ledger, name, None)), name)
                positional, keyword = arguments[name]
                with self.assertRaises(RuntimeError) as caught:
                    getattr(self.ledger, name)(*positional, **keyword)
                self.assertEqual(str(caught.exception), READ_ONLY_MESSAGE)

    def test_the_same_write_calls_succeed_on_a_writable_ledger(self) -> None:
        # Positive control for the test above: if these arguments were rejected
        # for some unrelated reason, "raises RuntimeError" would be measuring
        # nothing. Every call here reaches real ledger state.
        arguments = self.write_call_arguments()
        for name in ("begin_operation", "record_decision", "record_admitted_replay_v3"):
            with self.subTest(method=name):
                positional, keyword = arguments[name]
                getattr(self.writable, name)(*positional, **keyword)
        self.assertEqual(self.writable.operation_status("operation-2"), "pending")
        self.writable.release_pending_operation("operation-2", OWNER_TOKEN)
        self.writable.quarantine_operation(OPERATION_KEY, OWNER_TOKEN)

    def test_no_write_method_in_the_source_is_missing_its_guard(self) -> None:
        source, class_node = ledger_source_and_class_node()
        writers: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        for node in class_node.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            segment = ast.get_source_segment(source, node)
            self.assertIsNotNone(segment, node.name)
            if any(marker in segment for marker in WRITE_SQL_MARKERS):
                writers[node.name] = node

        # The marker scan is the whole test; assert it actually found the known
        # writers rather than silently matching nothing.
        self.assertTrue(GUARD_EXEMPT_METHOD_NAMES <= set(writers), sorted(writers))
        self.assertEqual(
            sorted(set(writers) - GUARD_EXEMPT_METHOD_NAMES), sorted(WRITE_METHOD_NAMES)
        )

        for name in sorted(set(writers) - GUARD_EXEMPT_METHOD_NAMES):
            with self.subTest(method=name):
                body = body_without_docstring(writers[name])
                self.assertTrue(
                    is_guard_statement(body[0]),
                    f"{name} must begin with self._require_writable(); "
                    f"it begins with {ast.dump(body[0])[:120]}",
                )

    def test_the_guard_scan_would_notice_an_unguarded_writer(self) -> None:
        # Positive control for the scan above: the same predicates applied to a
        # deliberately unguarded class must fail it.
        source = (
            "class Ledger:\n"
            "    def record_something(self):\n"
            "        with self._connection() as connection:\n"
            "            connection.execute('INSERT INTO decisions VALUES (1)')\n"
        )
        class_node = ast.parse(source).body[0]
        assert isinstance(class_node, ast.ClassDef)
        node = class_node.body[0]
        assert isinstance(node, ast.FunctionDef)
        segment = ast.get_source_segment(source, node)
        assert segment is not None
        self.assertTrue(any(marker in segment for marker in WRITE_SQL_MARKERS))
        self.assertFalse(is_guard_statement(body_without_docstring(node)[0]))


class ReadOnlyLedgerSqliteBackstopTests(ReadOnlyLedgerFixture):
    """The Python guard is legible; `mode=ro` is what makes it structural.

    These tests reach past `_require_writable` on purpose. If any of them stops
    failing, the second defence is gone and the class is back to a promise.
    """

    def assert_nothing_was_written(self) -> None:
        auditor = Ledger(self.path)
        self.assertEqual(auditor.decision_count(), 1)
        self.assertIs(auditor.has_decision(self.admitted.decision), True)
        with auditor._connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM operation_events").fetchone()[0],
                3,
            )

    def test_sqlite_refuses_a_direct_insert_on_the_read_only_connection(self) -> None:
        with self.assertRaises(sqlite3.OperationalError) as caught:
            with Ledger._connection(self.ledger) as connection:
                connection.execute(
                    "INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "decision-smuggled",
                        "request-smuggled",
                        "run-smuggled",
                        "gate-smuggled",
                        "b" * 64,
                        "c" * 64,
                        "d" * 64,
                        "policy-smuggled",
                        "operator",
                        "approve",
                        "written past the guard",
                        "2026-08-01T00:00:00Z",
                    ),
                )
        self.assertIn("readonly", str(caught.exception))
        self.assert_nothing_was_written()

    def test_sqlite_refuses_a_direct_update_and_delete_too(self) -> None:
        for statement, parameters in (
            (
                "UPDATE operation_claims SET status = 'pending' WHERE operation_key = ?",
                (OPERATION_KEY,),
            ),
            ("DELETE FROM operation_events WHERE operation_key = ?", (OPERATION_KEY,)),
            ("DROP TRIGGER decisions_no_update", ()),
        ):
            with self.subTest(statement=statement):
                with self.assertRaises(sqlite3.OperationalError) as caught:
                    with Ledger._connection(self.ledger) as connection:
                        connection.execute(statement, parameters)
                self.assertIn("readonly", str(caught.exception))
        self.assert_nothing_was_written()

    def test_sqlite_refuses_record_decision_with_the_guard_stubbed_out(self) -> None:
        # The bypass a future caller is most likely to reach for: the guard is
        # an ordinary instance-resolved method, so shadowing it costs one line.
        self.ledger._require_writable = lambda: None
        later = replace(
            self.admitted.decision,
            decision_id="decision-past-the-guard",
            idempotency_id="request-past-the-guard",
            rationale="recorded past a stubbed guard",
        )
        self.assertIsNone(self.ledger._require_writable())
        with self.assertRaises(sqlite3.OperationalError) as caught:
            self.ledger.record_decision(later)
        self.assertIn("readonly", str(caught.exception))
        self.assertIs(self.ledger.has_decision(later), False)
        self.assert_nothing_was_written()

    def test_sqlite_refuses_a_transactional_writer_with_the_guard_stubbed_out(self) -> None:
        # `record_admitted_replay_v3` opens with BEGIN IMMEDIATE. Note that
        # BEGIN IMMEDIATE itself *succeeds* on a `mode=ro` connection (SQLite
        # defers taking the write lock), so the refusal must come from the
        # INSERT. A test that only asserted "BEGIN IMMEDIATE fails" would pass
        # for the wrong reason on a writable connection too.
        self.ledger._require_writable = lambda: None
        second = admit_v3(fixture_v3())
        with self.assertRaises(sqlite3.OperationalError) as caught:
            self.ledger.record_admitted_replay_v3(second)
        self.assertIn("readonly", str(caught.exception))
        self.assert_nothing_was_written()

    def test_the_same_smuggled_write_succeeds_on_a_writable_ledger(self) -> None:
        # Positive control: the INSERT the read-only ledger refuses is a valid
        # statement, so the refusal above is about the mode and not the SQL.
        with self.writable._connection() as connection:
            connection.execute(
                "INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "decision-smuggled",
                    "request-smuggled",
                    "run-smuggled",
                    "gate-smuggled",
                    "b" * 64,
                    "c" * 64,
                    "d" * 64,
                    "policy-smuggled",
                    "operator",
                    "approve",
                    "written past the guard",
                    "2026-08-01T00:00:00Z",
                ),
            )
        self.assertEqual(self.ledger.decision_count(), 2)


class ReadOnlyLedgerOpenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    def test_a_missing_ledger_is_refused_and_nothing_is_created(self) -> None:
        path = self.root / "absent" / "ledger.sqlite3"
        with self.assertRaises(FileNotFoundError) as caught:
            Ledger(path, read_only=True)
        self.assertIn(str(path), str(caught.exception))
        self.assertFalse(path.exists())
        self.assertFalse(path.parent.exists())
        self.assertEqual(sorted(entry.name for entry in self.root.iterdir()), [])

    def test_a_directory_in_place_of_a_ledger_is_refused(self) -> None:
        path = self.root / "ledger.sqlite3"
        path.mkdir()
        with self.assertRaises(FileNotFoundError):
            Ledger(path, read_only=True)
        self.assertTrue(path.is_dir())

    def test_read_only_must_be_a_bool(self) -> None:
        path = self.root / "ledger.sqlite3"
        Ledger(path)
        for candidate in (1, 0, "true", None, (), 1.0):
            with self.subTest(read_only=candidate):
                with self.assertRaises(TypeError) as caught:
                    Ledger(path, read_only=candidate)  # type: ignore[arg-type]
                self.assertEqual(
                    str(caught.exception), "Ledger read_only must be a boolean"
                )

    def test_read_only_is_keyword_only(self) -> None:
        path = self.root / "ledger.sqlite3"
        Ledger(path)
        with self.assertRaises(TypeError):
            Ledger(path, True)  # type: ignore[misc]

    def test_opening_read_only_runs_no_schema_statements(self) -> None:
        # An empty file is a database with no tables. A read-only open must not
        # create them: the probe has to fail rather than manufacture the very
        # state the caller came to check.
        path = self.root / "empty.sqlite3"
        path.write_bytes(b"")
        with self.assertRaises(sqlite3.DatabaseError):
            Ledger(path, read_only=True)
        self.assertEqual(path.read_bytes(), b"")

    def test_opening_read_only_does_not_touch_the_database_header(self) -> None:
        # `_connect` deliberately skips `PRAGMA journal_mode` in read-only mode,
        # because setting it rewrites bytes 18-19 of the header. Proven on a
        # non-WAL database, where the write would actually be visible.
        path = self.root / "delete-mode.sqlite3"
        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("CREATE TABLE decisions (decision_id TEXT PRIMARY KEY)")
            connection.commit()
        finally:
            connection.close()
        before = path.read_bytes()
        self.assertEqual(before[18:20], b"\x01\x01")

        ledger = Ledger(path, read_only=True)
        self.assertEqual(ledger.decision_count(), 0)

        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(
            sorted(entry.name for entry in self.root.iterdir()), ["delete-mode.sqlite3"]
        )


class ReadOnlyLedgerCompetingWriterTests(unittest.TestCase):
    def test_reads_track_a_live_writer_in_another_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.sqlite3"
            writable = Ledger(path)
            admitted = admit_v3(fixture_v3())
            writable.record_decision(admitted.decision)
            writable.record_admitted_replay_v3(admitted)

            context = multiprocessing.get_context("spawn")
            committed = context.Event()
            proceed = context.Event()
            holding = context.Event()
            release = context.Event()
            process = context.Process(
                target=competing_writer,
                args=(str(path), committed, proceed, holding, release),
            )
            process.start()
            try:
                self.assertTrue(
                    committed.wait(60), "competing writer never committed its row"
                )
                # The competing connection is still open, so this row exists
                # only in the -wal file. A reader that skipped the WAL would
                # answer 1 here and look perfectly healthy while being wrong.
                self.assertGreater(
                    path.with_name(path.name + "-wal").stat().st_size, 0
                )
                ledger = Ledger(path, read_only=True)
                self.assertEqual(ledger.decision_count(), 2)

                proceed.set()
                self.assertTrue(
                    holding.wait(60), "competing writer never took the write lock"
                )
                self.assertTrue(process.is_alive())

                # Reads keep working while another process holds the write lock.
                self.assertEqual(ledger.decision_count(), 2)
                self.assertIs(ledger.has_decision(admitted.decision), True)
                handle = ledger.admitted_replay_handle_for_run(admitted.run_spec.run_id)
                self.assertEqual(
                    handle,
                    AdmittedReplayHandleV1(1, admitted.run_spec.run_id, admitted.sha256),
                )
                self.assertEqual(ledger.load_admitted_replay_v3(handle), admitted)
                self.assertEqual(
                    Ledger(path, read_only=True).require_admitted_replay_v3(admitted),
                    admitted,
                )
            finally:
                # Both, so a failed assertion before `proceed` does not leave
                # the child blocked for its full wait.
                proceed.set()
                release.set()
                process.join(60)
            self.assertEqual(process.exitcode, 0)
            # The held transaction was rolled back, so its row was never real.
            self.assertEqual(Ledger(path, read_only=True).decision_count(), 2)

    def test_reads_see_a_committed_row_that_still_lives_only_in_the_wal(self) -> None:
        # The same invariant without the process machinery, so it is not hostage
        # to it. `immutable=1` on the read-only URI would silently pass every
        # other test in this module and fail exactly here.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.sqlite3"
            Ledger(path)
            self.assertEqual(
                sorted(entry.name for entry in Path(directory).iterdir()),
                ["ledger.sqlite3"],
            )
            writer = sqlite3.connect(path, timeout=30)
            self.addCleanup(writer.close)
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute(
                "INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                decision_row(
                    "decision-in-the-wal",
                    "request-in-the-wal",
                    "committed but not checkpointed",
                ),
            )
            writer.commit()
            self.assertGreater(path.with_name(path.name + "-wal").stat().st_size, 0)

            ledger = Ledger(path, read_only=True)
            self.assertEqual(ledger.decision_count(), 1)


class AdmittedReplayHandleForRunTests(ReadOnlyLedgerFixture):
    def test_handle_pins_the_recorded_sha_and_loads(self) -> None:
        run_id = self.admitted.run_spec.run_id
        handle = self.ledger.admitted_replay_handle_for_run(run_id)
        self.assertIs(type(handle), AdmittedReplayHandleV1)
        self.assertEqual(handle.schema_version, 1)
        self.assertEqual(handle.run_id, run_id)
        self.assertEqual(handle.admitted_replay_sha256, self.admitted.sha256)
        with self.writable._connection() as connection:
            recorded = connection.execute(
                "SELECT admitted_replay_sha256 FROM admitted_replays_v3 WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
        self.assertEqual(handle.admitted_replay_sha256, recorded)

        loaded = self.ledger.load_admitted_replay_v3(handle)
        self.assertEqual(loaded, self.admitted)
        self.assertIsNot(loaded, self.admitted)

    def test_handle_for_an_unrecorded_run_is_refused(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.ledger.admitted_replay_handle_for_run("run-never-admitted")
        self.assertEqual(
            str(caught.exception), "Admitted replay authority is not recorded"
        )

    def test_handle_refuses_a_run_id_that_is_not_a_string(self) -> None:
        for candidate in (None, 1, b"run-1", AdmittedReplayHandleV1(1, "run-1", "0" * 64)):
            with self.subTest(type=type(candidate).__name__):
                with self.assertRaises(TypeError) as caught:
                    self.ledger.admitted_replay_handle_for_run(candidate)  # type: ignore[arg-type]
                self.assertEqual(
                    str(caught.exception), "Admitted replay run id must be a string"
                )


if __name__ == "__main__":
    unittest.main()

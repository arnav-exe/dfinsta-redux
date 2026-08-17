"""The nine mechanical steps of a port, and the two properties that matter.

Until 2026-08-14 the sequence lived in a paragraph of a design document. That is
where `run_corpus.py` and `record_corpus.py` lived the day before, and the two
device corpora they produced were reproducible by nobody — so the failure mode is
not hypothetical, it is the most recent one.

The two properties: **it is resumable**, because two device walks are about a
hundred minutes and a runbook that restarted from the top would be one nobody
uses; and **it refuses rather than continuing**, because every step's output is
the next step's input.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import importlib.util

REPOSITORY = Path(__file__).resolve().parent.parent
_SIGN_ENV = ("DFINSTA_KEYSTORE", "DFINSTA_KEY_ALIAS", "DFINSTA_KEYSTORE_PASSWORD")


def load():
    """Load `tools/port.py` as a module.

    Registered in `sys.modules` before it is executed, because `@dataclass`
    resolves its annotations through `sys.modules[cls.__module__]` and a module
    that is not there yet raises inside the decorator — a failure that reads as a
    bug in the tool rather than in how the test loaded it.
    """
    import sys

    spec = importlib.util.spec_from_file_location("port", REPOSITORY / "tools" / "port.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["port"] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop("port", None)
    return module


class ResumeTests(unittest.TestCase):
    """Done-ness is read from artefacts, never from a state file.

    A state file is a second thing that can be wrong about the work, and the
    artefacts are what the next step reads anyway.
    """

    def setUp(self) -> None:
        self.port_module = load()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.apk = self.root / "stock.apk"
        self.apk.write_bytes(b"PK\x03\x04")
        self.port = self.port_module.Port(
            apk=self.apk, version="442",
            out=self.root / "out", captures=self.root / "captures",
        )
        self.port.captures.mkdir(parents=True)

    def test_a_reused_index_counts_as_indexed(self) -> None:
        """`--reuse-index` points at an earlier run's surface, and the steps that
        read it do not care which run produced it. Reporting `todo` for work that
        was done is the failure a resumable tool exists to avoid."""
        self.assertEqual(self.root / "out" / "index", self.port.index_dir)
        reused = self.root / "elsewhere" / "index"
        reused.mkdir(parents=True)
        self.port.reuse_index = reused
        self.assertEqual(reused, self.port.index_dir)

    def test_the_build_digest_comes_from_the_release_report(self) -> None:
        """The recorded sessions carry the APK's digest, and it has to be the one
        that was signed — not recomputed here, where a different file could be
        hashed and nobody would know."""
        self.assertEqual("", self.port.build_sha256, "no report is no digest")
        self.port.signed.parent.mkdir(parents=True, exist_ok=True)
        self.port.signed.with_suffix(".release.json").write_text(
            json.dumps({"outputs": {"apk_sha256": "d" * 64}}), encoding="utf-8"
        )
        self.assertEqual("d" * 64, self.port.build_sha256)

    def test_a_partly_walked_corpus_is_not_done(self) -> None:
        """Eleven of twelve sessions is not a corpus: `grouping` needs both
        running orders of every state, and calls a state walked once unreadable."""
        for index in range(11):
            (self.port.captures / f"442-on-isolate-{index}.log").write_text("x", encoding="utf-8")
        self.assertEqual(11, self.port_module.walked(self.port, "one-pass-v1"))
        step = next(s for s in self.port_module.STEPS if s.name == "walk-one-pass-v1")
        self.assertFalse(step.done(self.port))
        (self.port.captures / "442-on-isolate-11.log").write_text("x", encoding="utf-8")
        self.assertTrue(step.done(self.port))

    def test_recording_is_counted_against_this_build_not_any_build(self) -> None:
        """A version's store can hold sessions from several builds — 439 and 440
        each held two builds' worth until the superseded rows were withdrawn. A
        count that ignored the digest would call this port recorded on the
        strength of a previous one's sessions."""
        step = next(s for s in self.port_module.STEPS if s.name == "record-one-pass-v1")
        self.assertFalse(step.done(self.port), "no release report, so no digest, so not done")


class ReEnteringTheRunDirectoryTests(unittest.TestCase):
    """Nine steps share one `--out`, and the driver refuses to overwrite itself.

    Found on 2026-08-15, the first time this tool was run against a version
    nobody had ported. `index` succeeded and `observe-build` — the next driver
    invocation, into the same directory — died on "refusing to overwrite
    analysis-decode". Reusing the decode moved the same failure one line down to
    the index. Every earlier port had been driven by hand with `--reuse-decode`,
    so the runbook's own first three steps had never once run in sequence, and
    `tools/port_430/build.py` carries a comment about the identical discovery at
    its own level: the refusals are deliberate, and it is re-entry that has to
    be arranged around them.
    """

    def setUp(self) -> None:
        self.port_module = load()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.apk = self.root / "stock.apk"
        self.apk.write_bytes(b"PK\x03\x04")
        self.port = self.port_module.Port(
            apk=self.apk, version="442",
            out=self.root / "out", captures=self.root / "captures",
        )

    def after_index(self) -> None:
        """Exactly what the `index` step leaves behind.

        Both directories, because the first fix here set up only the decode and
        so did not catch the index colliding one line later.
        """
        for name in ("analysis-decode", "index", "framework"):
            (self.port.out / name).mkdir(parents=True)

    def driver_commands(self):
        import sys

        head = [sys.executable, "-m", "dfinsta_pipeline.driver"]
        return [
            (step.name, step.command(self.port))
            for step in self.port_module.STEPS
            if step.command(self.port)[:3] == head
        ]

    def named(self, command, flag):
        return command[command.index(flag) + 1] if flag in command else None

    def test_a_later_driver_step_reuses_what_index_produced(self) -> None:
        self.after_index()
        commands = self.driver_commands()
        self.assertTrue(commands, "no driver steps found; the walk is vacuous")
        for name, command in commands:
            with self.subTest(step=name):
                self.assertEqual(
                    str(self.port.out / "analysis-decode"),
                    self.named(command, "--reuse-decode"),
                )
                self.assertEqual(
                    str(self.port.out / "index"),
                    self.named(command, "--reuse-index"),
                )

    def test_a_fresh_port_still_extracts_and_indexes(self) -> None:
        """The control: with nothing on disk, nothing claims anything exists."""
        for name, command in self.driver_commands():
            with self.subTest(step=name):
                self.assertIsNone(self.named(command, "--reuse-decode"))
                self.assertIsNone(self.named(command, "--reuse-index"))

    def test_the_driver_really_refuses_both(self) -> None:
        """Why the reuse is needed, asserted against the driver itself.

        If it ever stopped refusing, this fails and says the arrangement has
        outlived its cause — which a test of the composed command alone could
        never notice.
        """
        from dfinsta_pipeline.driver import DriverError, build_index, extract

        occupied = self.root / "already-there"
        occupied.mkdir()
        with self.assertRaises(DriverError) as decode:
            extract(self.apk, occupied, Path("apktool.jar"), self.root, None)
        self.assertIn("refusing to overwrite", str(decode.exception))
        with self.assertRaises(DriverError) as index:
            build_index(self.root, occupied)
        self.assertIn("refusing to overwrite", str(index.exception))

    def test_a_named_decode_that_is_not_there_is_refused(self) -> None:
        """Otherwise the run extracts into the default instead, and succeeds
        while measuring a decode nobody chose."""
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            code = self.port_module.main([
                "--apk", str(self.apk), "--version", "442",
                "--out", str(self.port.out), "--captures", str(self.port.captures),
                "--reuse-decode", str(self.root / "nowhere"),
            ])
        self.assertEqual(2, code)
        self.assertIn("nowhere", errors.getvalue())

    def test_a_half_finished_build_is_cleared_and_the_costly_work_is_not(self) -> None:
        self.after_index()
        for name in ("patch-source", "build-decode", "work-tree"):
            (self.port.out / name).mkdir()
        (self.port.out / "dfinsta.build.json").write_text("{}", encoding="utf-8")
        step = next(s for s in self.port_module.STEPS if s.name == "observe-build")
        leavings = set(step.stale(self.port))
        self.assertEqual(
            {self.port.out / n for n in
             ("patch-source", "build-decode", "work-tree", "dfinsta.build.json")},
            leavings,
        )
        for keep in ("analysis-decode", "index", "framework"):
            self.assertNotIn(self.port.out / keep, leavings)

    def test_a_first_attempt_removes_nothing(self) -> None:
        self.after_index()
        step = next(s for s in self.port_module.STEPS if s.name == "observe-build")
        self.assertEqual([], step.stale(self.port))

    def test_the_run_clears_the_leavings_before_it_runs_the_command(self) -> None:
        """The behaviour, not the list: at the moment the driver is invoked, the
        half-finished tree is gone and the decode and index are still there."""
        self.after_index()
        (self.port.out / "patch-source").mkdir()
        seen = {}

        class Result:
            returncode = 0
            stdout = ""

            def __init__(self, command, **kwargs):
                assert "--observe" in command, f"expected the build, got {command}"
                seen["patch-source"] = (self.port_outer / "patch-source").exists()
                seen["analysis-decode"] = (self.port_outer / "analysis-decode").is_dir()
                seen["index"] = (self.port_outer / "index").is_dir()
                raise SystemExit(0)

        Result.port_outer = self.port.out
        patcher = mock.patch.object(self.port_module.subprocess, "run", Result)
        patcher.start()
        self.addCleanup(patcher.stop)
        # `watch` runs first and has no leavings of its own; with nothing left to
        # watch it is skipped, exactly as it was on 442, so the driver is the
        # first command and the probe sees the state it is given.
        watch = mock.patch.object(self.port_module, "_nothing_left_to_watch", lambda port: True)
        watch.start()
        self.addCleanup(watch.stop)
        # No phone, so the probe is not handed `adb devices` before the build.
        device = mock.patch.object(self.port_module, "device_attached", lambda: False)
        device.start()
        self.addCleanup(device.stop)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            self.port_module.main([
                "--apk", str(self.apk), "--version", "442",
                "--out", str(self.port.out), "--captures", str(self.port.captures),
                "--run",
            ])
        self.assertEqual(
            {"patch-source": False, "analysis-decode": True, "index": True}, seen
        )


class RefusalTests(unittest.TestCase):
    """It stops, and it says which command to run by hand."""

    def setUp(self) -> None:
        self.port_module = load()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.apk = self.root / "stock.apk"
        self.apk.write_bytes(b"PK\x03\x04")

    def stub_run(self, replacement) -> None:
        """Replace `subprocess.run` for one test, and put it back.

        `tools/port.py` does `import subprocess`, so the module attribute **is**
        the global module — assigning `port_module.subprocess.run` patches it
        process-wide and permanently. It did, and it broke two unrelated tests in
        `test_reproducible_from_clone` that shell out to git, but only when the
        whole suite ran and only in that order. `patch.object` restores it.
        """
        patcher = mock.patch.object(self.port_module.subprocess, "run", replacement)
        patcher.start()
        self.addCleanup(patcher.stop)

    def stub_device(self, attached: bool) -> None:
        patcher = mock.patch.object(self.port_module, "device_attached", lambda: attached)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_main(self, *extra: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        argv = ["--apk", str(self.apk), "--version", "442",
                "--out", str(self.root / "out"),
                "--captures", str(self.root / "captures"), *extra]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.port_module.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_without_run_it_executes_nothing_and_writes_nothing(self) -> None:
        """A tool that changes the tree as a side effect of being asked a question
        is one you cannot use to ask."""
        calls = []

        class Probe:
            """A `subprocess.run` that records and answers.

            It must answer, because the done-predicates shell out too — `install`
            asks `dumpsys` what version is on the phone. A stub returning `None`
            makes the *predicate* crash, which reads as the tool being broken.
            """

            returncode = 0
            stdout = ""

            def __init__(self, *args, **kwargs):
                calls.append(args)

        self.stub_run(Probe)
        self.stub_device(True)
        code, page, _ = self.run_main()
        self.assertEqual(0, code)
        self.assertEqual(
            [], [item for item in calls if "dumpsys" not in str(item)],
            "reporting must not execute a step — asking the phone what is installed "
            "is a question, not a step",
        )
        self.assertIn("minutes of work outstanding", page)
        self.assertFalse((self.root / "out").exists())

    def test_a_missing_apk_is_refused_before_anything(self) -> None:
        self.apk.unlink()
        code, _, err = self.run_main()
        self.assertEqual(2, code)
        self.assertIn("is not a file", err)

    def test_no_device_stops_at_the_first_step_that_needs_one(self) -> None:
        """And says so rather than failing inside adb, because the repair is to
        plug the phone in and everything after it needs one too."""
        self.stub_device(False)
        code, page, _ = self.run_main()
        self.assertEqual(1, code)
        self.assertIn("BLOCKED", page)
        self.assertIn("attach the phone", page)

    def test_every_signing_step_refuses_without_the_secrets(self) -> None:
        """Not only the step called `sign`. `ship-sign` signs too, was added
        later, and under a name-equality check would have run the signer with no
        credentials — failing inside a tool this one exists to refuse in front of.
        """
        module = self.port_module
        signing = [
            step.name for step in module.STEPS if step.env is not module._DEFAULT_ENV
        ]
        self.assertEqual(["sign", "ship-sign"], signing)
        # And the refusal is keyed on the same thing, so the two cannot drift.
        source = (REPOSITORY / "tools" / "port.py").read_text(encoding="utf-8")
        self.assertNotIn('if step.name == "sign"', source)
        self.assertIn("step.env is not _DEFAULT_ENV", source)

    def probe_port(self):
        return self.port_module.Port(
            apk=self.apk, version="442",
            out=self.root / "out", captures=self.root / "captures",
        )

    def test_the_signing_secrets_are_never_read_defaulted_or_printed(self) -> None:
        """It refuses by *name* and never suggests a value. The keystore password
        is the one thing in this repository that must not reach a transcript."""
        source = (REPOSITORY / "tools" / "port.py").read_text(encoding="utf-8")
        self.assertNotIn("keystore-password", source)
        self.assertNotIn("dfinsta-release.keystore", source)
        # It passes the variables through from the caller's environment and reads
        # none of them itself.
        self.assertIn("os.environ[name] for name in _SIGNING if name in os.environ", source)

    def test_a_step_that_exits_zero_without_its_output_is_refused(self) -> None:
        """"Succeeded and produced nothing" is the shape that lets a later step
        run against an input that is not there."""
        class Result:
            returncode = 0
            stdout = ""

            def __init__(self, *args, **kwargs):
                pass

        self.stub_device(True)
        self.stub_run(Result)
        code, _, err = self.run_main("--run")
        self.assertEqual(1, code)
        self.assertIn("reported success without doing the work", err)

    def test_a_failed_step_stops_the_run_and_names_the_cost(self) -> None:
        class Result:
            returncode = 3
            stdout = ""

            def __init__(self, *args, **kwargs):
                pass

        self.stub_device(True)
        self.stub_run(Result)
        code, _, err = self.run_main("--run")
        self.assertEqual(3, code)
        self.assertIn("refusing to continue", err)
        self.assertIn("unknown state", err)


class ShippingTests(unittest.TestCase):
    """The last three steps: the build you would actually use, and getting it on.

    Added 2026-08-15. Until then the runbook ended at the recorded corpus and the
    shippable build was a command somebody typed by hand — so the tool that exists
    to make a port one command stopped one step short of the thing the project is
    for.
    """

    def setUp(self) -> None:
        self.port_module = load()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.apk = self.root / "stock.apk"
        self.apk.write_bytes(b"PK\x03\x04")
        self.port = self.port_module.Port(
            apk=self.apk, version="442",
            out=self.root / "work" / "442-port",
            captures=self.root / "captures",
        )

    def step(self, name: str):
        return next(s for s in self.port_module.STEPS if s.name == name)

    def test_the_shippable_build_gets_its_own_run_directory(self) -> None:
        """Not the port's: the builder refuses to overwrite seven of its own
        outputs and the observing build already wrote them there."""
        self.assertNotEqual(self.port.out, self.port.ship_out)
        command = self.step("ship-build").command(self.port)
        self.assertIn(str(self.port.ship_out), command)
        self.assertNotIn("--observe", command)

    def test_the_shipped_build_reuses_the_decode_the_port_already_made(self) -> None:
        (self.port.out / "analysis-decode").mkdir(parents=True)
        (self.port.out / "index").mkdir(parents=True)
        command = self.step("ship-build").command(self.port)
        self.assertIn(str(self.port.out / "analysis-decode"), command)
        self.assertIn(str(self.port.out / "index"), command)

    def test_a_half_finished_ship_build_is_cleared_but_the_framework_is_kept(self) -> None:
        self.port.ship_out.mkdir(parents=True)
        for name in ("framework", "build-decode", "work-tree"):
            (self.port.ship_out / name).mkdir()
        leavings = set(self.step("ship-build").stale(self.port))
        self.assertEqual(
            {self.port.ship_out / "build-decode", self.port.ship_out / "work-tree"},
            leavings,
        )

    def test_installed_is_false_when_it_cannot_tell(self) -> None:
        """The safety property, and the reason this is not a version check.

        Both builds report the same versionName, are signed by the same key, and
        on 442 came out the same number of bytes. Answering "yes" for the
        observing build would silently skip the step that replaces it, so every
        way of not knowing must answer no.
        """
        checked = self.port_module._shipped_is_installed
        # No signed APK at all.
        self.assertFalse(checked(self.port))
        # A signed APK with no release report to name its digest.
        self.port.ship_out.mkdir(parents=True)
        self.port.ship_signed.write_bytes(b"PK\x03\x04")
        self.assertFalse(checked(self.port))
        # A report that does not carry one.
        report = self.port.ship_signed.with_suffix(".release.json")
        report.write_text(json.dumps({"outputs": {}}), encoding="utf-8")
        self.assertFalse(checked(self.port))
        # A report that is not JSON at all.
        report.write_text("{ not json", encoding="utf-8")
        self.assertFalse(checked(self.port))
        # And the case the file checks exist for: a perfectly good report naming a
        # digest the phone really is carrying, while the APK it describes is not
        # on disk. "Installed" would be true and useless — there is nothing here
        # to install, so the run must not report the step done.
        report.write_text(
            json.dumps({"outputs": {"apk_sha256": "abc123"}}), encoding="utf-8"
        )
        self.port.ship_signed.unlink()

        class Present:
            def __init__(self, command, **kwargs):
                self.stdout = (
                    "package:/data/app/~~x==/base.apk\n" if "pm" in command
                    else "abc123  /data/app/~~x==/base.apk\n"
                )

        patcher = mock.patch.object(self.port_module.subprocess, "run", Present)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertFalse(checked(self.port))

    def test_installed_compares_the_digest_the_device_reports(self) -> None:
        self.port.ship_out.mkdir(parents=True)
        self.port.ship_signed.write_bytes(b"PK\x03\x04")
        self.port.ship_signed.with_suffix(".release.json").write_text(
            json.dumps({"outputs": {"apk_sha256": "abc123"}}), encoding="utf-8"
        )

        class Result:
            def __init__(self, command, **kwargs):
                if "pm" in command:
                    self.stdout = "package:/data/app/~~x==/base.apk\n"
                else:
                    self.stdout = f"{Result.digest}  /data/app/~~x==/base.apk\n"

        patcher = mock.patch.object(self.port_module.subprocess, "run", Result)
        patcher.start()
        self.addCleanup(patcher.stop)

        Result.digest = "abc123"
        self.assertTrue(self.port_module._shipped_is_installed(self.port))
        Result.digest = "deadbeef"  # the observing build, or anything else
        self.assertFalse(self.port_module._shipped_is_installed(self.port))


class RulingBlocksTheShippedBuildTests(unittest.TestCase):
    """A candidate nobody ruled on must not be shipped past.

    The shippable build renders `url_block_rules`. Building it before the human
    has ruled ships a decision nobody took — silently, and with every static check
    passing, which is this project's most repeated failure shape.

    Driven through a temporary repository rather than by mocking the dynamic
    import: the loading is part of what is being tested, and a stub in front of it
    would pass whether or not the real path works.
    """

    def setUp(self) -> None:
        self.port_module = load()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        apk = self.root / "stock.apk"
        apk.write_bytes(b"PK\x03\x04")
        self.port = self.port_module.Port(
            apk=apk, version="442", out=self.root / "out", captures=self.root / "captures",
        )
        (self.port.out / "index").mkdir(parents=True)
        (self.root / "tools").mkdir()
        (self.root / "manifest").mkdir()
        (self.root / "manifest" / "hooks.json").write_text("{}", encoding="utf-8")
        patcher = mock.patch.object(self.port_module, "REPOSITORY", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def given(self, found, ruled=()):
        (self.root / "tools" / "watch_candidates.py").write_text(
            f"def candidates(index, manifest):\n    return {list(found)!r}\n",
            encoding="utf-8",
        )
        if ruled:
            (self.root / "manifest" / "rulings.jsonl").write_text(
                "".join(
                    json.dumps({"record": {"candidate_id": f"gap:{item}"}}) + "\n"
                    for item in ruled
                ),
                encoding="utf-8",
            )

    def test_an_unruled_candidate_blocks_and_names_it(self) -> None:
        self.given(["feed/reels_media/"])
        reason = self.port_module._unruled_candidates(self.port)
        self.assertIn("feed/reels_media/", reason)
        self.assertIn("no ruling", reason)

    def test_no_candidates_blocks_nothing(self) -> None:
        self.given([])
        self.assertEqual("", self.port_module._unruled_candidates(self.port))

    def test_a_candidate_that_was_ruled_no_longer_blocks(self) -> None:
        """ANY verdict answers it. An endpoint ruled `ignore` is still an
        uncovered gap and stays a candidate for ever — waiting for it to stop
        being one would block every future port."""
        self.given(["feed/reels_media/"], ruled=["feed/reels_media/"])
        self.assertEqual("", self.port_module._unruled_candidates(self.port))

    def test_one_ruled_and_one_not_still_blocks_on_the_one(self) -> None:
        self.given(["a/", "b/"], ruled=["a/"])
        reason = self.port_module._unruled_candidates(self.port)
        self.assertIn("b/", reason)
        self.assertNotIn("a/,", reason)

    def test_no_index_yet_is_not_a_block(self) -> None:
        """Nothing has been indexed, so nothing has been proposed to rule on.
        Blocking here would stop a port before it started."""
        self.given(["a/"])
        fresh = self.port_module.Port(
            apk=self.port.apk, version="442",
            out=self.root / "elsewhere", captures=self.port.captures,
        )
        self.assertEqual("", self.port_module._unruled_candidates(fresh))

    def test_it_blocks_the_ship_build_and_nothing_earlier(self) -> None:
        """Only the build that renders the rules waits; the measurement does not."""
        default = self.port_module.Step.__dataclass_fields__["blocked_by"].default
        blocked = [
            step.name for step in self.port_module.STEPS if step.blocked_by is not default
        ]
        self.assertEqual(["ship-build"], blocked)


class ItStopsAtTheJudgementTests(unittest.TestCase):
    """Ruling is the human's, and the build that renders a ruling waits for it."""

    def test_the_steps_are_mechanical_only(self) -> None:
        module = load()
        names = [step.name for step in module.STEPS]
        for judgement in ("submit", "rule", "apply-rulings", "gate"):
            self.assertNotIn(judgement, names)
        self.assertEqual(
            ["index", "watch", "observe-build", "sign", "install", "warm",
             "walk-one-pass-v1", "walk-three-round-v2",
             "record-one-pass-v1", "record-three-round-v2",
             "ship-build", "ship-sign", "ship-install"],
            names,
        )

    def test_the_shipped_build_comes_after_both_corpora_are_recorded(self) -> None:
        """It replaces the observing build on the phone, and that build is what
        every recorded session was measured against."""
        module = load()
        names = [step.name for step in module.STEPS]
        for corpus in ("record-one-pass-v1", "record-three-round-v2"):
            self.assertLess(names.index(corpus), names.index("ship-build"))
        self.assertLess(names.index("ship-build"), names.index("ship-install"))

    def test_the_warm_up_is_before_the_walks_and_after_the_install(self) -> None:
        module = load()
        names = [step.name for step in module.STEPS]
        self.assertLess(names.index("install"), names.index("warm"))
        self.assertLess(names.index("warm"), names.index("walk-one-pass-v1"))

    def test_it_says_what_is_left_for_the_human(self) -> None:
        source = (REPOSITORY / "tools" / "port.py").read_text(encoding="utf-8")
        self.assertIn("rule on the candidates", source)
        self.assertIn("not derivable", source)

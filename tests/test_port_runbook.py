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
import subprocess
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
    `tools/build/build.py` carries a comment about the identical discovery at
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
        so did not catch the index colliding one line later. And `header.json`,
        because the index step is finished by the marker the indexer writes last
        rather than by the directory it makes first — a fixture stopping at the
        directory describes a *crashed* index, which the step now correctly
        wants to rebuild.
        """
        for name in ("analysis-decode", "index", "framework"):
            (self.port.out / name).mkdir(parents=True)
        (self.port.out / "index" / "header.json").write_text("{}", encoding="utf-8")

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

    def test_it_blocks_the_ship_build_and_nothing_that_measures(self) -> None:
        """Nothing that measures the phone ever waits on a human.

        Three steps may block and all three are downstream of the walk: the two
        that raise the gate, when this run was not given what raising one needs,
        and the build that renders the rules, until they are ruled. A corpus is
        never held up by a judgement — that ordering is why the shipped build is
        last and why `assess` sits after both walks.
        """
        default = self.port_module.Step.__dataclass_fields__["blocked_by"].default
        names = [step.name for step in self.port_module.STEPS]
        blocked = [
            step.name for step in self.port_module.STEPS if step.blocked_by is not default
        ]
        self.assertEqual(["assess", "raise-gate", "ship-build"], blocked)
        for name in blocked:
            with self.subTest(step=name):
                self.assertGreater(
                    names.index(name), names.index("walk-three-round-v2"),
                    "a step that can wait on a human must come after the walks",
                )


class ItStopsAtTheJudgementTests(unittest.TestCase):
    """Ruling is the human's, and the build that renders a ruling waits for it."""

    def test_the_steps_are_mechanical_only(self) -> None:
        """Asking is mechanical; answering is not, and only answering is barred.

        `raise-gate` starts the Workflow that puts the question to a human and
        then waits — it holds no authority, decides nothing, and cannot admit its
        own answer. What must never appear here is a step that *rules*: submits
        a disposition, applies one, or writes the `url_block_rules` entry that
        follows from it.
        """
        module = load()
        names = [step.name for step in module.STEPS]
        for judgement in ("submit", "rule", "apply-rulings", "dispositions"):
            self.assertNotIn(judgement, names)
        self.assertEqual(
            ["index", "watch", "observe-build", "sign", "install", "warm",
             "walk-one-pass-v1", "walk-three-round-v2",
             "record-one-pass-v1", "record-three-round-v2",
             "assess", "raise-gate",
             "ship-build", "ship-sign", "ship-install"],
            names,
        )
        self.assertLess(names.index("raise-gate"), names.index("ship-build"))
        self.assertGreater(names.index("assess"), names.index("record-three-round-v2"))

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


class EveryStepCanFinishItsOwnStepTests(unittest.TestCase):
    """A step's done-check must be satisfiable by that step's own command.

    **The defect this was written for.** `warm`'s done-check was
    `walked(port, WALKS[0]) > 0` — the number of captures produced by the *next*
    step. Warm launches the app and reads the nav; it produces no captures. So
    the step could never both run and pass: the driver refuses a step that exits
    zero with its check still false, and warm's check could only become true once
    the walk it protects had already started.

    It went unnoticed for four ports because it fails *safe-looking*. Every
    earlier port reached warm with captures already on disk from a partial walk,
    so the check read true, warm was skipped, and the seam was routed around
    rather than exercised — the same shape as the extract-and-build seam
    `--reuse-decode` walked past every time. 443 was the first port to reach warm
    on a genuinely empty corpus, and it stopped the run twice.

    `RefusalTests.test_a_step_that_exits_zero_without_its_output_is_refused`
    proves the guard *bites*. Nothing proved a real step could get *past* it,
    which is the other half and the half that was broken.

    **What this covers and what it does not.** The table below says what each
    step leaves behind, and `test_the_table_covers_every_step` fails if a step is
    added without an entry — so a new step must be classified rather than
    silently untested. Three predicates are not filesystem facts and are stubbed
    here: `install` and `ship-install` ask the phone, and `watch` asks whether the
    manifest already covers the index. They are marked in the table and their
    stubs assert only that the predicate consults the thing named, not what it
    answers.
    """

    def setUp(self) -> None:
        self.port_module = load()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.apk = self.root / "stock.apk"
        self.apk.write_bytes(b"PK\x03\x04")
        self.port = self.port_module.Port(
            apk=self.apk, version="443",
            out=self.root / "out", captures=self.root / "captures",
        )
        self.port.captures.mkdir(parents=True)
        self.port.out.mkdir(parents=True)

    # ---- what each step leaves behind, and nothing later than it -------------

    def satisfy_index(self) -> None:
        # `header.json` and not merely the directory: the indexer makes the
        # directory before it reads a file and writes this after the last one,
        # so the marker is what distinguishes a finished index from a crashed
        # one. A fixture that only made the directory was asserting the bug.
        self.port.index_dir.mkdir(parents=True)
        (self.port.index_dir / "header.json").write_text("{}", encoding="utf-8")

    def satisfy_observe_build(self) -> None:
        self.port.unsigned.parent.mkdir(parents=True, exist_ok=True)
        self.port.unsigned.write_bytes(b"PK\x03\x04")

    def satisfy_sign(self) -> None:
        self.port.signed.parent.mkdir(parents=True, exist_ok=True)
        self.port.signed.write_bytes(b"PK\x03\x04")

    def satisfy_warm(self) -> None:
        self.port.warm_marker.parent.mkdir(parents=True, exist_ok=True)
        self.port.warm_marker.write_text('{"nav": []}', encoding="utf-8")

    def satisfy_walk(self, walk: str) -> None:
        for index in range(12):
            (self.port.captures / f"443-{walk[:2]}-{index}.log").write_text("x", encoding="utf-8")

    def satisfy_ship_build(self) -> None:
        self.port.ship_unsigned.parent.mkdir(parents=True, exist_ok=True)
        self.port.ship_unsigned.write_bytes(b"PK\x03\x04")

    def satisfy_ship_sign(self) -> None:
        self.port.ship_signed.parent.mkdir(parents=True, exist_ok=True)
        self.port.ship_signed.write_bytes(b"PK\x03\x04")

    #: `None` marks a predicate that is not a filesystem fact — see the class
    #: docstring. Everything else is a callable that creates exactly what that
    #: step produces, and nothing a later step produces.
    def table(self) -> dict:
        return {
            "index": self.satisfy_index,
            "watch": None,                       # asks the manifest and the index
            "observe-build": self.satisfy_observe_build,
            "sign": self.satisfy_sign,
            "install": None,                     # asks the phone
            "warm": self.satisfy_warm,
            "walk-one-pass-v1": lambda: self.satisfy_walk("one-pass-v1"),
            "walk-three-round-v2": lambda: self.satisfy_walk("three-round-v2"),
            "record-one-pass-v1": None,          # asks the committed store
            "record-three-round-v2": None,       # asks the committed store
            "assess": None,                      # asks the ledger, or has nothing to do
            "raise-gate": None,                  # asks the ledger, or has nothing to do
            "ship-build": self.satisfy_ship_build,
            "ship-sign": self.satisfy_ship_sign,
            "ship-install": None,                # asks the phone
        }

    def test_the_table_covers_every_step(self) -> None:
        """A step added without an entry fails here rather than going untested."""
        self.assertEqual(
            [step.name for step in self.port_module.STEPS], list(self.table())
        )

    def test_each_step_is_not_done_before_it_runs(self) -> None:
        """The control. Without it the test below could pass on a predicate that
        is simply always true, which is the other way to be unfalsifiable."""
        table = self.table()
        for step in self.port_module.STEPS:
            if table[step.name] is None:
                continue
            with self.subTest(step=step.name):
                self.assertFalse(
                    step.done(self.port),
                    f"{step.name} reads as done on an empty port",
                )

    def test_each_steps_own_artefact_makes_it_done(self) -> None:
        """The one that was broken. Each step is satisfied in isolation, on a
        port where **nothing after it has run**, so a predicate reading a later
        step's output cannot pass.
        """
        for step in self.port_module.STEPS:
            satisfy = self.table()[step.name]
            if satisfy is None:
                continue
            with self.subTest(step=step.name):
                self.setUp()
                satisfy = self.table()[step.name]
                self.assertFalse(step.done(self.port))
                satisfy()
                self.assertTrue(
                    step.done(self.port),
                    f"{step.name} ran and still does not read as done, so the run "
                    "refuses it as 'exited 0 and its output is not there'",
                )

    def test_warm_specifically_does_not_wait_on_the_walk_it_protects(self) -> None:
        """Named, because this is the instance and it is worth failing loudly.

        The walk cannot start until warm passes, and warm used to require the
        walk to have started. Whatever else changes, these two must not become
        each other's precondition again.
        """
        warm = next(step for step in self.port_module.STEPS if step.name == "warm")
        self.assertFalse(warm.done(self.port))
        self.satisfy_warm()
        self.assertTrue(
            warm.done(self.port),
            "warm is satisfied only by a capture, which only the walk after it makes",
        )
        self.assertEqual(
            0, self.port_module.walked(self.port, self.port_module.WALKS[0]),
            "and it must pass with no capture on disk at all",
        )

    def test_an_existing_corpus_still_short_circuits_the_warm_up(self) -> None:
        """The half of the old check worth keeping: a corpus already in progress
        means something stricter than warm has read the nav, and re-warming would
        force-stop the app mid-corpus for no reason."""
        warm = next(step for step in self.port_module.STEPS if step.name == "warm")
        self.assertFalse(warm.done(self.port))
        self.satisfy_walk("one-pass-v1")
        self.assertFalse(self.port.warm_marker.is_file())
        self.assertTrue(warm.done(self.port))

    def test_the_warm_command_asks_for_the_artefact_it_is_checked_on(self) -> None:
        """A predicate and a command that disagree is the defect one level up:
        the check would be sound and nothing would ever write the file."""
        warm = next(step for step in self.port_module.STEPS if step.name == "warm")
        command = [str(part) for part in warm.command(self.port)]
        self.assertIn("--warm", command)
        self.assertIn("--out", command)
        self.assertIn(str(self.port.warm_marker), command)


class OneInstallCheckForBothBuildsTests(unittest.TestCase):
    """`install` and `ship-install` ask the same question of the same phone.

    They were two functions and the weaker one guarded the corpus. `_installed`
    matched `versionName=<version>.` out of `dumpsys`, reasoning that dumpsys
    reports no digest — true of dumpsys, and not true of the problem: the `pm
    path` + `sha256sum` pair in `_shipped_is_installed` sat beside it the whole
    time doing exactly that.

    **The failure it allowed.** Both this project's builds of one version report
    the same `versionName`, are signed by the same key, and on 442 came out the
    same number of bytes. So `install`'s check was satisfiable by the *shipped*
    build — the artefact of the last step of the same run. Delete the captures to
    re-walk a finished port and `install` reads done, the walk measures a build
    with **no observer**, and `record-*` files the resulting silence under the
    observing build's `build_sha256`, because that is what the runbook passes.
    Twelve well-formed captures, every check green, and a corpus that says the
    app requested nothing.

    That is the `warm` defect inverted — a predicate satisfied by a later step's
    artefact rather than by none — and it is why
    `EveryStepCanFinishItsOwnStepTests` states the property per step.
    """

    def setUp(self) -> None:
        self.port_module = load()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        apk = self.root / "stock.apk"
        apk.write_bytes(b"PK\x03\x04")
        self.port = self.port_module.Port(
            apk=apk, version="443",
            out=self.root / "out", captures=self.root / "captures",
        )

    def build(self, signed: Path, digest: str) -> None:
        signed.parent.mkdir(parents=True, exist_ok=True)
        signed.write_bytes(b"PK\x03\x04")
        signed.with_suffix(".release.json").write_text(
            json.dumps({"outputs": {"apk_sha256": digest}}), encoding="utf-8"
        )

    def stub_device(self, digest: str) -> list:
        """A phone holding one APK with `digest`. Records what it was asked."""
        asked: list = []

        def fake(command, *a, **k):
            asked.append(command)
            text = ""
            if "path" in command:
                text = "package:/data/app/base.apk\n"
            elif "sha256sum" in command:
                text = f"{digest}  /data/app/base.apk\n"
            return subprocess.CompletedProcess(command, 0, text, "")

        patcher = mock.patch.object(self.port_module.subprocess, "run", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return asked

    def test_the_shipped_build_does_not_satisfy_the_observing_install(self) -> None:
        """The defect, stated directly. Both builds exist and the phone holds the
        shipped one; the step that installs the *observing* build must run."""
        self.build(self.port.signed, "a" * 64)
        self.build(self.port.ship_signed, "b" * 64)
        self.stub_device("b" * 64)
        self.assertFalse(
            self.port_module._installed(self.port),
            "the shipped build satisfied the observing install",
        )
        self.assertTrue(self.port_module._shipped_is_installed(self.port))

    def test_the_observing_build_satisfies_its_own_install(self) -> None:
        """The control, without which the test above passes on a function that
        always says no."""
        self.build(self.port.signed, "a" * 64)
        self.build(self.port.ship_signed, "b" * 64)
        self.stub_device("a" * 64)
        self.assertTrue(self.port_module._installed(self.port))
        self.assertFalse(self.port_module._shipped_is_installed(self.port))

    def test_a_stock_apk_of_the_same_version_satisfies_neither(self) -> None:
        """`versionName` is what the stock APK also reports. It was the whole
        check."""
        self.build(self.port.signed, "a" * 64)
        self.build(self.port.ship_signed, "b" * 64)
        self.stub_device("c" * 64)
        self.assertFalse(self.port_module._installed(self.port))
        self.assertFalse(self.port_module._shipped_is_installed(self.port))

    def test_it_does_not_reach_for_the_phone_before_the_build_exists(self) -> None:
        """Report mode is a question, and must be answerable with no phone.

        `done()` is evaluated for every step before the `needs_device` check, so
        a predicate that shelled out unconditionally would make `tools/port.py`
        with no `--run` touch a device — and, in the suite, touch the real one.
        """
        asked = self.stub_device("a" * 64)
        self.assertFalse(self.port_module._installed(self.port))
        self.assertFalse(self.port_module._shipped_is_installed(self.port))
        self.assertEqual([], asked, "it asked the phone with nothing built yet")

    def test_a_build_with_no_release_report_is_not_installed(self) -> None:
        """False whenever it cannot tell. Re-running `install -r` costs twenty
        seconds; a wrong "already done" costs the corpus."""
        self.port.signed.parent.mkdir(parents=True, exist_ok=True)
        self.port.signed.write_bytes(b"PK\x03\x04")
        asked = self.stub_device("a" * 64)
        self.assertFalse(self.port_module._installed(self.port))
        self.assertEqual([], asked)

    def test_both_predicates_are_the_same_mechanism(self) -> None:
        """Named, so the two cannot drift apart again. What differs between them
        is which APK they are asked about and nothing else."""
        import inspect

        for name in ("_installed", "_shipped_is_installed"):
            body = inspect.getsource(getattr(self.port_module, name))
            with self.subTest(function=name):
                self.assertIn("_on_device_is(", body)
                self.assertNotIn("dumpsys", body)


class AHalfBuiltIndexIsNotAnIndexTests(unittest.TestCase):
    """The third of the family, found by auditing for the shape of the first two.

    `build_index` makes its output directory before it reads a single smali file
    and writes `header.json` after the last one. The step's check was
    `index_dir.is_dir()`, so an index that died partway — killed, out of disk,
    out of memory — left a directory the runbook called finished. It is also the
    only expensive step with no `stale`, so it could not heal itself: the step
    read done forever, and the failure surfaced one step later as `IndexError_`
    raised out of `watch`'s predicate, four frames into a module the operator
    never invoked, with no mention of the step or the fix.

    Reproduced before it was fixed: `index.done` returned True on a directory
    holding a single empty `structural.jsonl`, and `watch.done` raised.
    """

    def setUp(self) -> None:
        self.port_module = load()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        apk = self.root / "stock.apk"
        apk.write_bytes(b"PK\x03\x04")
        self.port = self.port_module.Port(
            apk=apk, version="444",
            out=self.root / "out", captures=self.root / "captures",
        )
        self.step = next(s for s in self.port_module.STEPS if s.name == "index")

    def half_built(self) -> None:
        self.port.index_dir.mkdir(parents=True)
        (self.port.index_dir / "structural.jsonl").write_text("", encoding="utf-8")

    def finished(self) -> None:
        self.half_built()
        (self.port.index_dir / "header.json").write_text("{}", encoding="utf-8")

    def test_a_directory_alone_is_not_a_finished_index(self) -> None:
        self.half_built()
        self.assertFalse(
            self.step.done(self.port),
            "a crashed index read as finished and was never rebuilt",
        )

    def test_the_marker_the_indexer_writes_last_is_what_finishes_it(self) -> None:
        self.finished()
        self.assertTrue(self.step.done(self.port))

    def test_a_half_built_index_is_cleared_so_the_step_can_heal(self) -> None:
        """Without this the step reruns into a directory the builder refuses to
        overwrite, which is a different failure with the same cost."""
        self.half_built()
        self.assertEqual([self.port.index_dir], self.step.stale(self.port))

    def test_a_reused_index_is_never_deleted(self) -> None:
        """`--reuse-index` points at an earlier run's expensive artefact. This
        run wanting to rebuild is not a licence to remove somebody else's."""
        other = self.root / "elsewhere" / "index"
        other.mkdir(parents=True)
        port = self.port_module.Port(
            apk=self.port.apk, version="444", out=self.root / "out2",
            captures=self.root / "captures", reuse_index=other,
        )
        (port.out / "index").mkdir(parents=True)
        self.assertEqual([], self.step.stale(port))

    def test_the_real_committed_index_reads_as_finished(self) -> None:
        """The control, against the artefact a real port produced: the marker
        this now keys on has to be a thing the indexer actually writes."""
        real = REPOSITORY / "work" / "443-port" / "index"
        if not real.is_dir():
            self.skipTest("no 443 index on disk")
        self.assertTrue((real / "header.json").is_file())


class APredicateThatCannotAnswerIsNamedTests(unittest.TestCase):
    """A done-check that raises must refuse by name, not kill the run.

    Every predicate is evaluated for every step at the top of the loop, before
    the device gate and before anything runs. One of them raising took the whole
    run down with a traceback out of a module the operator never called. It now
    names the step, says the artefact is unreadable rather than absent, and says
    what to do.

    **It is not treated as "not done".** A predicate that cannot answer means a
    broken artefact, and running the step on top of one is how a corrupt input
    becomes a corrupt output.
    """

    def setUp(self) -> None:
        self.port_module = load()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.apk = self.root / "stock.apk"
        self.apk.write_bytes(b"PK\x03\x04")

    def run_main(self, *extra: str) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        argv = ["--apk", str(self.apk), "--version", "444",
                "--out", str(self.root / "out"),
                "--captures", str(self.root / "captures"), *extra]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.port_module.main(argv)
        return code, err.getvalue()

    def test_a_raising_predicate_refuses_and_names_the_step(self) -> None:
        class Unreadable(Exception):
            pass

        def explode(port):
            raise Unreadable("index at /somewhere is not readable")

        with mock.patch.object(self.port_module, "_nothing_left_to_watch", explode):
            code, err = self.run_main()
        self.assertEqual(1, code)
        self.assertIn("watch", err, "the step must be named")
        self.assertIn("Unreadable", err, "and the cause carried")
        self.assertIn("unreadable rather than absent", err)

    def test_it_does_not_escape_as_a_traceback(self) -> None:
        """What it did before: `IndexError_` four frames deep, no step named."""
        def explode(port):
            raise RuntimeError("boom")

        with mock.patch.object(self.port_module, "_nothing_left_to_watch", explode):
            try:
                code, _ = self.run_main()
            except RuntimeError:  # pragma: no cover - the regression
                self.fail("the predicate's exception escaped main()")
        self.assertEqual(1, code)

    def test_a_sound_predicate_is_untouched(self) -> None:
        """The control: the refusal must be about raising, not about running.

        The device is stubbed **present**. Without that this passes only while a
        phone happens to be plugged into the machine running the suite: `install`
        blocks without one, `main` returns 1, and the control fails for a reason
        that has nothing to do with predicates. It really did, the first time the
        phone was unplugged after this was written.
        """
        with mock.patch.object(self.port_module, "device_attached", lambda: True):
            code, err = self.run_main()
        self.assertEqual(0, code)
        self.assertNotIn("cannot tell whether it is already done", err)


class RaisingItsOwnGateTests(unittest.TestCase):
    """The run can park in Temporal instead of ending at a printed instruction.

    A gate waits for a human, and a human may take days. Until 2026-08-19 the
    runbook stopped at the judgement and printed five commands: the first two —
    record the assessment, raise the gate — are mechanism, but they need a run
    id, an actor and an owner token, and the tool would not invent those because
    a tool that did would be signing a document on somebody's behalf.

    Given them, it does exactly those two. The run then ends with a Workflow
    parked on `wait_condition`, durable for a week, answerable from another
    machine by someone holding nothing but a run id — and an unanswered gate
    expires to `blocked`, which is never an implicit approval.

    **What it still will not do is answer.** `raise-gate` asks; `submit` is
    absent from this runbook and `ItStopsAtTheJudgementTests` keeps it that way.
    """

    def setUp(self) -> None:
        self.port_module = load()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.apk = self.root / "stock.apk"
        self.apk.write_bytes(b"PK\x03\x04")

    def port(self, **overrides):
        fields = dict(
            apk=self.apk, version="444",
            out=self.root / "out", captures=self.root / "captures",
        )
        fields.update(overrides)
        return self.port_module.Port(**fields)

    def configured(self, **overrides):
        fields = dict(
            state_root=self.root / "state", assessment_run_id="feat-444",
            actor="sam.operator", owner_token="owner-1", build_id="worker-1",
        )
        fields.update(overrides)
        return self.port(**fields)

    # ---- all or nothing ------------------------------------------------------

    def test_a_run_given_nothing_is_not_configured(self) -> None:
        self.assertFalse(self.port().gate_configured)

    def test_every_one_of_the_five_is_required(self) -> None:
        """A half-configured gate would record an assessment nobody can rule on."""
        for absent in ("state_root", "assessment_run_id", "actor",
                       "owner_token", "build_id"):
            with self.subTest(missing=absent):
                port = self.configured(**{absent: None if absent == "state_root" else ""})
                self.assertFalse(port.gate_configured)
        self.assertTrue(self.configured().gate_configured, "the control")

    def test_the_refusal_names_what_is_missing(self) -> None:
        """`--build-id` especially: without it the Workflow is accepted by the
        server, dispatched to nobody, and every query times out with nothing
        naming the cause."""
        with mock.patch.object(self.port_module, "_unruled_candidates",
                               lambda port: "one candidate has no ruling"):
            reason = self.port_module._gate_not_configured(self.configured(build_id=""))
        self.assertIn("--build-id", reason)
        self.assertNotIn("--actor", reason, "and only what is actually missing")

    def test_nothing_to_rule_is_not_something_to_be_blocked_from(self) -> None:
        """The default path. Every run before this existed, and every port whose
        version raised no candidates — 443 raised none — must be untouched."""
        with mock.patch.object(self.port_module, "_unruled_candidates", lambda port: ""):
            self.assertEqual("", self.port_module._gate_not_configured(self.port()))

    # ---- the steps -----------------------------------------------------------

    def test_both_gate_steps_are_no_ops_when_there_is_nothing_to_rule(self) -> None:
        steps = [s for s in self.port_module.STEPS if s.name in ("assess", "raise-gate")]
        self.assertEqual(2, len(steps))
        with mock.patch.object(self.port_module, "_unruled_candidates", lambda port: ""):
            for step in steps:
                with self.subTest(step=step.name):
                    self.assertTrue(step.done(self.port()))

    def test_the_gate_steps_have_work_when_a_candidate_is_unruled(self) -> None:
        """The control for the test above."""
        steps = [s for s in self.port_module.STEPS if s.name in ("assess", "raise-gate")]
        with mock.patch.object(self.port_module, "_unruled_candidates",
                               lambda port: "one candidate has no ruling"):
            for step in steps:
                with self.subTest(step=step.name):
                    self.assertFalse(step.done(self.configured()))

    def test_a_raised_gate_is_not_raised_twice(self) -> None:
        """Raising again starts a second Workflow against the same subject while
        the first sits open until it expires. The candidate stays unruled for as
        long as the human has not answered, so it cannot be what says this step
        is finished — the marker is."""
        step = next(s for s in self.port_module.STEPS if s.name == "raise-gate")
        port = self.configured()
        with mock.patch.object(self.port_module, "_unruled_candidates",
                               lambda p: "one candidate has no ruling"):
            self.assertFalse(step.done(port))
            port.gate_marker.parent.mkdir(parents=True, exist_ok=True)
            port.gate_marker.write_text('{"workflow_id": "feat-444"}', encoding="utf-8")
            self.assertTrue(step.done(port))

    def test_the_raise_command_asks_for_the_marker_it_is_checked_on(self) -> None:
        """A predicate and a command that disagree is the defect one level up."""
        step = next(s for s in self.port_module.STEPS if s.name == "raise-gate")
        port = self.configured()
        command = [str(part) for part in step.command(port)]
        self.assertIn("raise", command)
        self.assertIn("--write-workflow-id", command)
        self.assertIn(str(port.gate_marker), command)
        self.assertIn("--build-id", command)
        self.assertIn("worker-1", command)

    def test_the_assess_command_carries_all_four_driver_arguments(self) -> None:
        step = next(s for s in self.port_module.STEPS if s.name == "assess")
        command = [str(part) for part in step.command(self.configured())]
        for flag in ("--state-root", "--assessment-run-id", "--actor", "--owner-token"):
            with self.subTest(flag=flag):
                self.assertIn(flag, command)
        self.assertIn("assess", command)

    def test_an_unrecorded_assessment_is_not_recorded(self) -> None:
        """False whenever it cannot tell: no state root, no ledger, an operation
        that never completed. Re-recording refuses by design, so a wrong "already
        done" sends the run to a gate with nothing behind it."""
        self.assertFalse(self.port_module._assessment_recorded(self.port()))
        self.assertFalse(self.port_module._assessment_recorded(self.configured()))

    # ---- a block outranks an artefact made before the block existed ----------

    def test_a_step_that_is_done_but_blocked_reports_blocked(self) -> None:
        """`ship-build` renders `url_block_rules`. A build made before a
        candidate was ruled does not carry the ruling, so an APK left by an
        earlier run must not let the step report finished once a new candidate
        appears — which it did, because `done` was asked first.

        Tested on a synthetic step rather than on `ship-build` itself, because
        `blocked_by` holds a **direct reference** captured when `STEPS` was
        built: patching `_unruled_candidates` by name reaches the `done` lambdas,
        which look it up at call time, and never reaches the blocker. `STEPS` is
        itself a module global the loop reads each run, so replacing that is what
        puts a done-and-blocked step in front of the ordering under test.
        """
        step = self.port_module.Step(
            "synthetic", "already done, and blocked anyway",
            lambda port: ["true"],
            lambda port: True,
            blocked_by=lambda port: "a candidate has no ruling",
        )
        with mock.patch.object(self.port_module, "STEPS", (step,)), \
             mock.patch.object(self.port_module, "device_attached", lambda: True):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = self.port_module.main([
                    "--apk", str(self.apk), "--version", "444",
                    "--out", str(self.root / "out"),
                    "--captures", str(self.root / "captures"),
                ])
            page = out.getvalue()
        self.assertEqual(1, code)
        line = [item for item in page.splitlines() if "synthetic" in item]
        self.assertEqual(1, len(line), f"expected one line, got {line}")
        self.assertIn("BLOCKED", line[0])
        self.assertNotIn("[done]", line[0])

    def test_a_step_that_is_done_and_unblocked_still_reports_done(self) -> None:
        """The control. The reorder must not turn every finished step into a
        blocked one."""
        step = self.port_module.Step(
            "synthetic", "done, nothing blocking",
            lambda port: ["true"], lambda port: True,
        )
        with mock.patch.object(self.port_module, "STEPS", (step,)), \
             mock.patch.object(self.port_module, "device_attached", lambda: True):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = self.port_module.main([
                    "--apk", str(self.apk), "--version", "444",
                    "--out", str(self.root / "out"),
                    "--captures", str(self.root / "captures"),
                ])
        self.assertEqual(0, code)
        self.assertIn("[done] synthetic", out.getvalue())


class TheGateMarkerIsForReadingTests(unittest.TestCase):
    """The one place unreadable may count as absent, and why that is allowed."""

    def setUp(self) -> None:
        self.port_module = load()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        apk = self.root / "stock.apk"
        apk.write_bytes(b"PK\x03\x04")
        self.port = self.port_module.Port(
            apk=apk, version="444", out=self.root / "out",
            captures=self.root / "captures",
        )
        self.port.out.mkdir(parents=True)

    def test_no_marker_reads_as_no_gate(self) -> None:
        self.assertIsNone(self.port_module._raised_gate(self.port))

    def test_a_marker_is_read_back(self) -> None:
        self.port.gate_marker.write_text(
            json.dumps({"workflow_id": "feat-444", "endpoint": "localhost:7233"}),
            encoding="utf-8",
        )
        self.assertEqual("feat-444", self.port_module._raised_gate(self.port)["workflow_id"])

    def test_a_torn_marker_reads_as_none_rather_than_raising(self) -> None:
        """This one feeds a message for a human. Withholding the rest of it
        because a marker got truncated would be the tail wagging the dog — every
        predicate that *decides* anything keeps absent and unreadable apart."""
        for junk in ("{ not json", "", "[1, 2, 3]"):
            with self.subTest(content=junk):
                self.port.gate_marker.write_text(junk, encoding="utf-8")
                self.assertIsNone(self.port_module._raised_gate(self.port))

    def test_the_handoff_names_the_workflow_when_there_is_one(self) -> None:
        self.port.gate_marker.write_text(
            json.dumps({"workflow_id": "feat-444", "endpoint": "localhost:7233",
                        "task_queue": "dfinsta-phase-a",
                        "gate_timeout_seconds": 7 * 24 * 3600}),
            encoding="utf-8",
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.port_module._judgement_steps(self.port)
        page = out.getvalue()
        self.assertIn("feat-444", page)
        self.assertIn("168h", page)
        self.assertIn("never an approval", page)
        self.assertNotIn("raise the gate", page, "it is already raised")

    def test_the_handoff_tells_you_how_when_there_is_not(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.port_module._judgement_steps(self.port)
        page = out.getvalue()
        self.assertIn("raise the gate", page)
        self.assertIn("--build-id", page, "and how to make it automatic")

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


class ItStopsBeforeTheJudgementTests(unittest.TestCase):
    """The last thing it does is raise the gate. Ruling is the human's."""

    def test_the_steps_are_mechanical_only(self) -> None:
        module = load()
        names = [step.name for step in module.STEPS]
        for judgement in ("submit", "rule", "apply-rulings", "gate"):
            self.assertNotIn(judgement, names)
        self.assertEqual(
            ["index", "watch", "observe-build", "sign", "install",
             "walk-one-pass-v1", "walk-three-round-v2",
             "record-one-pass-v1", "record-three-round-v2"],
            names,
        )

    def test_it_says_what_is_left_for_the_human(self) -> None:
        source = (REPOSITORY / "tools" / "port.py").read_text(encoding="utf-8")
        self.assertIn("rule on the candidates", source)
        self.assertIn("not derivable", source)

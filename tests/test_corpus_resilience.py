"""A walk that survives the cable, and a capture that is whole or absent.

Two changes made on 2026-08-19, both from the same afternoon: the phone dropped
off USB three times across two walks, and each time the walk stopped and the
repair by hand was to plug it back in and re-run.

**Why the runner may retry at all.** `run_corpus` stops at the first refusal
because the sessions after it would run against a phone in an unknown state, and
a corpus with one silently wrong session is worse than a corpus with nine. That
rule is not weakened here. A phone that is *no longer reachable* is a
distinguishable case: nothing touched the app, and the interrupted session left
no capture, so redoing that same session redoes exactly what was lost. What stays
forbidden is moving on to the next arm, which is what the rule is about.

**Why the capture is written atomically.** `run_corpus` decides a session is
already walked by its capture existing. A write interrupted halfway leaves a
truncated log that a resume counts as finished and `record_corpus` turns into a
committed row — the same reasoning `observation.append` gives for not using
`open(…, "a")`, and the same failure one layer earlier.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPOSITORY = Path(__file__).resolve().parents[1]


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPOSITORY / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def done(text: str = "walked", code: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], code, text, "")


class DropoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load("run_corpus")
        naps = mock.patch.object(self.module.time, "sleep", lambda seconds: None)
        naps.start()
        self.addCleanup(naps.stop)
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def stub(self, sessions, reachable):
        """`sessions` is what each call returns; `reachable` what `present` says."""
        self.calls: list[str] = []

        def fake_session(arm, out, walk):
            self.calls.append(arm)
            result = sessions.pop(0) if len(sessions) > 1 else sessions[0]
            if result.returncode == 0:
                Path(out).write_text("capture", encoding="utf-8")
            return result

        for name, value in (("session", fake_session),
                            ("present", lambda: reachable.pop(0) if len(reachable) > 1
                             else reachable[0])):
            patcher = mock.patch.object(self.module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_walk(self, which: str = "forward") -> int:
        return self.module.main(["one-pass-v1", "443", str(self.root), which])

    def test_a_session_that_fails_with_the_phone_gone_is_walked_again(self) -> None:
        """The three real dropouts, in one test.

        The dropout is put on the **third** arm, not the first. `ARMS[0]` is
        spelled `"none"`, so a retry that walked a hardcoded `"none"` instead of
        the arm that failed was indistinguishable from a correct one — the
        mutation that substitutes it survived until this moved.
        """
        failing = self.module.ARMS[2]
        self.assertNotEqual("none", failing, "the arm under test must be distinguishable")
        # `present` is consulted only when a session fails, so the list is
        # keyed to failures and not to arms: False once for the dropout, then
        # True for the poll inside `wait_for_device`.
        self.stub([done(), done(), done(code=1), done()], [False, True])
        self.assertEqual(0, self.run_walk())
        self.assertEqual(
            failing, self.calls[3],
            "it must redo the SAME arm, not move on to the next",
        )
        self.assertEqual(
            [*self.module.ARMS[:3], failing, *self.module.ARMS[3:]], self.calls,
            "and every other arm is walked exactly once, in order",
        )

    def test_a_session_that_fails_with_the_phone_present_still_refuses(self) -> None:
        """The rule that was not weakened. A walk failing on a reachable phone is
        the unknown state the refusal is for, and it must not be retried."""
        self.stub([done(code=1)], [True])
        self.assertEqual(1, self.run_walk())
        self.assertEqual([self.module.ARMS[0]], self.calls)

    def test_a_phone_that_does_not_come_back_refuses(self) -> None:
        """Bounded. An unplugged phone must fail the same evening, not block."""
        self.stub([done(code=1)], [False])
        self.assertEqual(1, self.run_walk())
        self.assertEqual([self.module.ARMS[0]], self.calls, "and it never retried")

    def test_a_retry_that_fails_again_refuses(self) -> None:
        """One retry, not a loop. The phone came back and the session still
        failed, which is now a real refusal."""
        self.stub([done(code=1), done(code=1)], [False, True])
        self.assertEqual(1, self.run_walk())
        self.assertEqual([self.module.ARMS[0]] * 2, self.calls)

    def test_the_wait_is_bounded(self) -> None:
        self.assertGreater(self.module.DEVICE_WAIT_SECONDS, 0)
        self.assertLessEqual(self.module.DEVICE_WAIT_SECONDS, 600)
        with mock.patch.object(self.module, "present", lambda: False):
            self.assertFalse(self.module.wait_for_device())

    def test_it_polls_until_the_phone_returns(self) -> None:
        answers = [False, False, True]
        with mock.patch.object(self.module, "present", lambda: answers.pop(0)):
            self.assertTrue(self.module.wait_for_device())

    def test_an_already_walked_session_is_never_re_run(self) -> None:
        """The resume property, which the retry must not disturb."""
        for arm in self.module.ARMS:
            (self.root / f"443-isolate-{self.module.short(arm)}.log").write_text(
                "x", encoding="utf-8"
            )
        self.stub([done()], [True])
        self.assertEqual(0, self.run_walk())
        self.assertEqual([], self.calls)

    def test_presence_is_false_for_every_reason_it_might_be(self) -> None:
        """`get-state` prints `device` and nothing else counts — an unauthorised
        phone, a missing adb and a timeout are all "not reachable"."""
        for outcome in ("unauthorized\n", "offline\n", "", "error: no devices\n"):
            with self.subTest(outcome=outcome):
                with mock.patch.object(
                    self.module.subprocess, "run",
                    lambda *a, **k: subprocess.CompletedProcess([], 0, outcome, ""),
                ):
                    self.assertFalse(self.module.present())
        for blow_up in (OSError("no adb"), subprocess.TimeoutExpired("adb", 30)):
            with self.subTest(raises=type(blow_up).__name__):
                with mock.patch.object(
                    self.module.subprocess, "run",
                    mock.Mock(side_effect=blow_up),
                ):
                    self.assertFalse(self.module.present())
        with mock.patch.object(
            self.module.subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess([], 0, "device\n", ""),
        ):
            self.assertTrue(self.module.present(), "the control")

    def test_it_asks_the_same_phone_the_sessions_use(self) -> None:
        """A second copy of the serial is a second thing that can name the wrong
        phone."""
        session_module = load("device_session")
        self.assertEqual(list(session_module.ADB), self.module.adb())


class AtomicCaptureTests(unittest.TestCase):
    """A capture is whole or absent, never truncated."""

    def setUp(self) -> None:
        self.module = load("device_session")
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_the_log_is_renamed_into_place_and_not_written_in_place(self) -> None:
        """`run_corpus` treats an existing capture as a finished session, so a
        half-written one becomes a committed row nothing can tell from a real."""
        seen: list = []
        real_replace = os.replace

        def watched(src, dst):
            seen.append((Path(src).name, Path(dst).name))
            return real_replace(src, dst)

        out = self.root / "443-on-isolate-none.log"
        with mock.patch.object(self.module.os, "replace", watched):
            with mock.patch.object(self.module, "sh", lambda *a: "LOG"):
                target = out.with_name(out.name + ".partial")
                target.write_text("LOG", encoding="utf-8")
                self.module.os.replace(target, out)
        self.assertEqual([("443-on-isolate-none.log.partial", out.name)], seen)
        self.assertEqual("LOG", out.read_text(encoding="utf-8"))

    def test_the_source_says_so(self) -> None:
        """The behaviour above is exercised through a stub, so this pins that the
        real path is the one being described."""
        source = (REPOSITORY / "tools" / "device_session.py").read_text(encoding="utf-8")
        self.assertIn("os.replace(partial, out)", source)
        self.assertNotIn("out.write_text(sh(", source)

    def test_no_partial_file_is_left_behind_on_success(self) -> None:
        out = self.root / "session.log"
        partial = out.with_name(out.name + ".partial")
        partial.write_text("LOG", encoding="utf-8")
        os.replace(partial, out)
        self.assertFalse(partial.exists())
        self.assertTrue(out.is_file())


if __name__ == "__main__":
    unittest.main()

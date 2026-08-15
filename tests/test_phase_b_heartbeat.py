"""The stage wrappers report they are alive, and from the right place.

Until 2026-08-05 no replay stage heartbeated, so a lost worker was invisible
until `start_to_close` expired — three hours for verify. Two things had to be
true before this could be added, and both were, in that order: a cancelled stage
must not be destroyed (it releases its claim), and the event loop must be free
often enough to run a heartbeater at all. The loop was blocked for stretches of
over nine minutes until the decoded-tree walks moved into a thread.

**The trap this file exists to guard.** `activity.heartbeat()` called from inside
an `asyncio.to_thread` thread of an `async def` activity puts details on a queue
that is not thread-safe and *then* raises `RuntimeError: no running event loop` —
enqueued, never flushed. And a test would not catch it: `ActivityEnvironment`'s
heartbeat callback is a plain synchronous lambda, so heartbeat-from-a-thread
passes there and fails against a real worker. So the guard here is structural —
the heartbeat must be emitted by a loop-side task — rather than a round trip
through a test environment that cannot tell the difference.
"""

import ast
import asyncio
import unittest
from pathlib import Path
from unittest import mock

from dfinsta_pipeline import activities

STAGE_WRAPPERS = (
    "replay_install_frameworks_stage_activity",
    "replay_decode_stage_activity",
    "replay_apply_tree_stage_activity",
    "replay_build_patched_apk_stage_activity",
    "replay_verify_final_apk_stage_activity",
)


def _tree(relative: str) -> ast.Module:
    path = Path(__file__).resolve().parents[1] / relative
    return ast.parse(path.read_text(encoding="utf-8"))


class HeartbeatWiringTests(unittest.TestCase):
    def test_every_stage_wrapper_heartbeats(self) -> None:
        """A stage that does not report is a stage whose worker loss is invisible."""
        found = {
            node.name: ast.unparse(node)
            for node in ast.walk(_tree("src/dfinsta_pipeline/activities.py"))
            if isinstance(node, ast.AsyncFunctionDef) and node.name in STAGE_WRAPPERS
        }
        self.assertEqual(set(found), set(STAGE_WRAPPERS))
        for name, body in found.items():
            with self.subTest(wrapper=name):
                self.assertIn("_with_heartbeat(", body, name)

    def test_the_heartbeat_is_emitted_from_the_loop_and_nowhere_else(self) -> None:
        """`activity.heartbeat` must not appear inside anything handed to a thread.

        Asserted by location rather than by behaviour, because the behaviour is
        exactly what `ActivityEnvironment` cannot distinguish.
        """
        tree = _tree("src/dfinsta_pipeline/activities.py")
        callers = set()

        def visit(node: ast.AST, enclosing: str | None) -> None:
            """Attribute a call to its INNERMOST function, not every ancestor.

            `ast.walk` from a function reaches the bodies of functions nested
            inside it, so a naive version reports the outer one too — and the
            whole point here is *which* function does the calling.
            """
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    visit(child, child.name)
                    continue
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr == "heartbeat"
                    and enclosing is not None
                ):
                    callers.add(enclosing)
                visit(child, enclosing)

        visit(tree, None)
        # `beat` is the loop-side task inside `_with_heartbeat`, and after
        # `PortRunWorkflow` was deleted on 2026-08-15 it is the only caller.
        # `apply_activity` used to be the other: Phase A's, which heartbeated from
        # the loop of an async activity and was the working precedent this was
        # written from. The rule it demonstrated is what this test holds, and the
        # rule does not need the precedent to keep standing.
        self.assertEqual(callers, {"beat"}, sorted(callers))

        # And nothing that a thread runs may call it. `to_thread` takes the
        # callable as its first argument; collect those names and check.
        threaded: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "to_thread"
                and node.args
            ):
                target = node.args[0]
                if isinstance(target, ast.Name):
                    threaded.add(target.id)
                elif isinstance(target, ast.Attribute):
                    threaded.add(target.attr)
        self.assertTrue(threaded, "found no to_thread call sites to check")
        self.assertEqual(threaded & callers, set(), sorted(threaded & callers))

    def test_the_workflow_sets_a_heartbeat_timeout_on_every_stage(self) -> None:
        """A heartbeat nothing waits for detects nothing."""
        source = (
            Path(__file__).resolve().parents[1]
            / "src/dfinsta_pipeline/replay_workflow.py"
        ).read_text(encoding="utf-8")
        # Both stage call sites: the loop over the plan, and verify.
        self.assertEqual(source.count("heartbeat_timeout=_STAGE_HEARTBEAT_TIMEOUT"), 2)
        self.assertEqual(source.count("start_to_close_timeout=timedelta(seconds="), 2)

        from dfinsta_pipeline import replay_workflow

        timeout = replay_workflow._STAGE_HEARTBEAT_TIMEOUT.total_seconds()
        interval = activities.HEARTBEAT_INTERVAL_SECONDS
        # Comfortably more than one interval, and comfortably less than the
        # shortest stage budget — otherwise it detects nothing sooner than
        # `start_to_close` already did.
        self.assertGreaterEqual(timeout, interval * 10)
        self.assertLess(timeout, 1800)


class HeartbeatBehaviourTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_slow_stage_reports_repeatedly(self) -> None:
        beats: list[dict] = []

        async def slow() -> str:
            await asyncio.sleep(0.25)
            return "done"

        with mock.patch.object(activities, "HEARTBEAT_INTERVAL_SECONDS", 0.02):
            with mock.patch.object(activities.activity, "heartbeat", beats.append):
                result = await activities._with_heartbeat("decode", slow())

        self.assertEqual(result, "done")
        self.assertGreater(len(beats), 3, beats)
        self.assertEqual({beat["stage"] for beat in beats}, {"decode"})
        # Monotonic, so a reader can tell a live stage from a stuck one.
        self.assertEqual([beat["beats"] for beat in beats], list(range(1, len(beats) + 1)))
        # And it reports the gap the loop actually delivered, which is what lets
        # the timeout be tightened against evidence instead of re-guessed.
        self.assertIn("worst_gap_seconds", beats[-1])

    async def test_the_heartbeater_stops_when_the_stage_returns(self) -> None:
        """A stage that finished must not keep claiming to be alive."""
        beats: list[dict] = []

        async def quick() -> str:
            return "done"

        before = len(asyncio.all_tasks())
        with mock.patch.object(activities, "HEARTBEAT_INTERVAL_SECONDS", 0.01):
            with mock.patch.object(activities.activity, "heartbeat", beats.append):
                await activities._with_heartbeat("decode", quick())
                await asyncio.sleep(0.05)
        self.assertEqual(beats, [])
        self.assertLessEqual(len(asyncio.all_tasks()), before + 1)

    async def test_cancellation_reaches_the_stage_body(self) -> None:
        """The stage's own handler must run — it decides release or quarantine.

        A heartbeater wrapped around a *shielded* task would swallow this, and the
        claim would be left for `start_to_close` to clean up.
        """
        reached = asyncio.Event()
        handled: list[str] = []

        async def stage() -> str:
            reached.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                handled.append("cancelled")
                raise
            return "unreachable"

        with mock.patch.object(activities, "HEARTBEAT_INTERVAL_SECONDS", 0.01):
            with mock.patch.object(activities.activity, "heartbeat", lambda _: None):
                task = asyncio.create_task(activities._with_heartbeat("decode", stage()))
                await reached.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertEqual(handled, ["cancelled"])

    async def test_a_failing_heartbeater_does_not_become_the_stage_outcome(self) -> None:
        """Reporting is not the work. A stage that succeeded must report success.

        The inverse would be worse than no heartbeat at all: a transient failure
        while telling the server "still alive" would fail a stage that was fine.
        """

        def explode(_details: dict) -> None:
            raise RuntimeError("heartbeat backend unavailable")

        async def slow() -> str:
            await asyncio.sleep(0.05)
            return "done"

        with mock.patch.object(activities, "HEARTBEAT_INTERVAL_SECONDS", 0.005):
            with mock.patch.object(activities.activity, "heartbeat", explode):
                self.assertEqual(
                    await activities._with_heartbeat("decode", slow()), "done"
                )


if __name__ == "__main__":
    unittest.main()

"""Stages must leave the worker's event loop free enough to answer.

A running Activity that never yields blocks the worker's loop, and a blocked
loop serves no queries and — since 2026-08-05 — emits no heartbeats. Both were
measured on real ports: 20 of 86 query samples answered, the longest unbroken
blocked stretch 28 samples (~560 s). Moving `materialize_decoded_tree` and
`capture_decoded_tree_fd` into threads took that to 58 of 63 and 3 samples.

`prepare_replay_verification_gate` was the stage that change deliberately did
NOT touch, and it stayed at 100% blocked across both runs. That was the useful
control at the time and it is a defect now. Its whole cost is `load_decoded_tree`
reached three levels down through receipt validation, which re-reads and
re-hashes every blob of a decoded tree.

**Why the primitive did not move, and the Activity did.** Four of
`load_decoded_tree`'s callers are synchronous validators that already run inside
`asyncio.to_thread` (`capture_and_verify`,
`_validate_replay_final_apk_verification_receipt`). There is no loop to await on
in a thread, so `load_decoded_tree` cannot become awaitable the way the other two
primitives did. The unit that moves is the Activity body.

The behavioural test here carries its own positive control: the same measurement
run against a deliberately loop-blocking callable must fail the assertion, or it
proves nothing about the threaded one.
"""

import ast
import asyncio
import time
import unittest
from pathlib import Path
from unittest import mock

from dfinsta_pipeline import activities

# Roughly 30x the tick interval below, so a served-ticks count near zero is
# unambiguous rather than a scheduling artifact.
_BLOCK_SECONDS = 0.3
_TICK_SECONDS = 0.01


def _activities_tree() -> ast.Module:
    path = Path(__file__).resolve().parents[1] / "src/dfinsta_pipeline/activities.py"
    return ast.parse(path.read_text(encoding="utf-8"))


def _function(name: str) -> ast.AST:
    for node in ast.walk(_activities_tree()):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in activities.py")


async def _ticks_while(work: "asyncio.Future[object] | asyncio.Task[object]") -> int:
    """How many times the loop got control while `work` ran."""
    served = 0
    while not work.done():
        await asyncio.sleep(_TICK_SECONDS)
        served += 1
    await work
    return served


class GateDerivationLocationTests(unittest.TestCase):
    def test_the_gate_activity_delegates_rather_than_deriving_inline(self) -> None:
        """The Activity body must hand the derivation to a thread.

        Asserted structurally because the cost is proportional to a real decoded
        tree: a unit test's tree is small enough that deriving on the loop would
        pass a timing assertion while a 209k-file port blocked for tens of
        seconds.
        """
        node = _function("prepare_replay_verification_gate_activity")
        body = ast.unparse(node)
        self.assertIn("_derive_replay_verification_gate", body)
        self.assertIn("to_thread", body)
        # The expensive calls must have moved out with it, not been left behind.
        for call in ("resolve_admitted_build", "load_admitted_replay_v3"):
            with self.subTest(call=call):
                self.assertNotIn(call, body)

        moved = ast.unparse(_function("_derive_replay_verification_gate"))
        self.assertIn("resolve_admitted_build", moved)
        self.assertIn("load_admitted_replay_v3", moved)

    def test_the_thread_is_waited_for_rather_than_cancelled(self) -> None:
        """`_await_thread_work`, not a bare await and not `task.cancel()`.

        A bare `await asyncio.to_thread(...)` returns on cancellation while the
        thread keeps hashing gigabytes, competing for the CPU it was moved off
        the loop to free. Cancelling is worse and is what
        `_await_verification_execution` is for -- a subprocess that must be
        reaped, never thread work.
        """
        body = ast.unparse(_function("prepare_replay_verification_gate_activity"))
        self.assertIn("_await_thread_work", body)
        self.assertNotIn("_await_verification_execution", body)
        self.assertNotIn(".cancel()", body)


class TreeVerificationLocationTests(unittest.TestCase):
    """Re-hashing a materialized tree must never happen on the event loop.

    Measured on a real 430 port *after* the two capture primitives had already
    moved into threads: the build stage reported a heartbeat gap of exactly
    30.0 s twenty-two times and then **111.6 s** once, near the end. That single
    block is `load_decoded_tree` plus `verify_materialized_decoded_tree` running
    on the loop after the build subprocess returned, and it is why build still
    measured 26% blocked when apply had fallen to 6%.

    111.6 s against a 300 s `_STAGE_HEARTBEAT_TIMEOUT` is 2.7x margin, not the
    10x the 340-only measurement suggested — so this is what stands between the
    timeout and being tightened, and a regression here is invisible except on a
    full port of the larger target.
    """

    def test_the_tree_re_hash_is_only_ever_reached_through_a_thread(self) -> None:
        tree = _activities_tree()
        callers: set[str] = set()

        def visit(node: ast.AST, enclosing: str | None) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    visit(child, child.name)
                    continue
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Name)
                    and child.func.id == "verify_materialized_decoded_tree"
                    and enclosing is not None
                ):
                    callers.add(enclosing)
                visit(child, enclosing)

        visit(tree, None)
        # `_load_and_verify_tree` is the threaded helper; `capture_and_verify` is
        # the verify stage's body, which `replay_verify_final_apk_checkpoint_activity`
        # already hands to `asyncio.to_thread` whole.
        self.assertEqual(callers, {"_load_and_verify_tree", "capture_and_verify"}, sorted(callers))

    def test_the_helper_is_always_the_thing_handed_to_the_thread(self) -> None:
        """A helper nothing threads is just a rename.

        Checked as "every mention of it is `to_thread`'s first argument", so a
        call site that reaches for it directly fails here rather than passing on
        a count that happens to match.
        """
        tree = _activities_tree()
        threaded = sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "to_thread"
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "_load_and_verify_tree"
        )
        mentions = sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and node.id == "_load_and_verify_tree"
        )
        self.assertEqual(threaded, 3, threaded)
        self.assertEqual(mentions, threaded, (mentions, threaded))

    def test_every_call_site_uses_the_drain_then_propagate_supervisor(self) -> None:
        """These run inside `try` blocks holding directory descriptors.

        That is the case `_await_thread_work`'s docstring was written for: a
        `finally` closing an fd under a live thread can have its number reused,
        and the walk then reads a *different* directory. A wrong answer, not an
        error.
        """
        source = (
            Path(__file__).resolve().parents[1] / "src/dfinsta_pipeline/activities.py"
        ).read_text(encoding="utf-8")
        threaded = source.count("_load_and_verify_tree,")
        supervised = source.count('"framework cache verification"') + source.count(
            '"patched tree verification"'
        )
        self.assertEqual(threaded, 3, threaded)
        self.assertEqual(supervised, threaded, (supervised, threaded))


class LoopAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_loop_bound_derivation_starves_the_loop(self) -> None:
        """POSITIVE CONTROL for the test below.

        Without this, "the loop kept ticking" could be true because the work was
        fast, the sleep was long, or the measurement never ran at all.
        """
        served = 0
        async def blocking() -> str:
            time.sleep(_BLOCK_SECONDS)
            return "done"

        task = asyncio.create_task(blocking())
        served = await _ticks_while(task)
        self.assertLessEqual(served, 1, served)

    async def test_the_gate_activity_keeps_the_loop_answering(self) -> None:
        handle = object()

        def slow(_handle: object) -> str:
            time.sleep(_BLOCK_SECONDS)
            return "gate"

        with mock.patch.object(activities, "_derive_replay_verification_gate", slow):
            task = asyncio.create_task(
                activities.prepare_replay_verification_gate_activity(handle)  # type: ignore[arg-type]
            )
            served = await _ticks_while(task)

        self.assertEqual(task.result(), "gate")
        # The control above served at most one tick over the same interval.
        self.assertGreater(served, 10, served)

    async def test_cancellation_waits_for_the_derivation_to_finish(self) -> None:
        """Cancelled means cancelled, but not until the thread has let go.

        The same rule the mutating stages follow. Here nothing holds a directory
        descriptor, so the hazard is CPU contention rather than a wrong answer --
        but a stage that returns while its thread runs on is the shape that
        produced the wrong answer elsewhere, and one rule is easier to keep than
        two.
        """
        finished = False

        def slow(_handle: object) -> str:
            nonlocal finished
            time.sleep(_BLOCK_SECONDS)
            finished = True
            return "gate"

        with mock.patch.object(activities, "_derive_replay_verification_gate", slow):
            task = asyncio.create_task(
                activities.prepare_replay_verification_gate_activity(object())  # type: ignore[arg-type]
            )
            await asyncio.sleep(_TICK_SECONDS * 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(finished, "the Activity returned while its thread was still running")


if __name__ == "__main__":
    unittest.main()

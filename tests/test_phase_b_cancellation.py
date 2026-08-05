"""How the five replay checkpoint Activities must handle being cancelled.

A cancelled operation is **released** so a later attempt can adopt it — except
where the subprocess could not be shown to have exited, which stays terminal.
`_releasable` is that rule and `executor.process_not_reaped` is the check.

Quarantine is terminal: `begin_operation` refuses the key forever and operation
keys derive from admitted content, so recovery needs a new run id, a new run spec
and a new human gate decision. Releasing leaves the claim `pending` with a blanked
owner, which is what lets `begin_operation` hand it to a second attempt *and* what
refuses a zombie's late `record_effect`. Getting this backwards costs a run.

**Why release is safe**, established by audit and recorded in
`docs/WORKFLOW_REGISTRATION_DESIGN.md` §3d: nothing a stage writes after its
workspace exists is shared — every write lands in the per-attempt workspace
`attempts_root/<key>/sha256(owner)` or in the content store, and every ledger call
in that window is a SELECT. CAS publication is atomic and content-addressed. And
`claims.py` already releases exactly this state by hand, adding only a human's
confirmation that the first attempt is dead; `process_not_reaped` is that
confirmation in code.

**Why only a cancellation.** An ordinary post-workspace failure still quarantines.
Releasing would be equally safe for the ledger, but such a failure is usually
deterministic and failing closed on it is the point, whereas a cancellation
carries no information about the work at all.

Three things are checked here:

* **the invariant is stated once.** Three stages once quarantined unconditionally
  where two used the graduated form, with no comment, no commit message and zero
  tests either way — drift from birth (2026-07-28 versus 07-29 and 07-31). A
  separate cancel handler is how that happened, so there must not be one.
* **the shape is identical in all five**, read out of the source.
* **the pre-workspace branch is still unreachable**, and the day it stops being
  so is a day someone should notice. There is no suspension point between
  claiming the operation and creating the workspace, and an async Activity is
  cancelled by `task.cancel()`, delivered only at a suspension point.

The behavioural halves live with the fixtures that can drive a real stage:
`test_phase_b_replay_activity` has both the released case and the unreapable
positive control, and `test_phase_b_verification_activity` injects the
cancellation the runtime cannot deliver before a workspace exists.
"""

import ast
import unittest
from pathlib import Path

REPLAY_CHECKPOINTS = (
    "replay_install_frameworks_checkpoint_activity",
    "replay_decode_checkpoint_activity",
    "replay_apply_tree_checkpoint_activity",
    "replay_build_patched_apk_checkpoint_activity",
    "replay_verify_final_apk_checkpoint_activity",
)


def _activities_tree() -> ast.Module:
    path = Path(__file__).resolve().parents[1] / "src/dfinsta_pipeline/activities.py"
    return ast.parse(path.read_text(encoding="utf-8"))


def _checkpoints() -> dict[str, ast.AsyncFunctionDef]:
    found = {
        node.name: node
        for node in ast.walk(_activities_tree())
        if isinstance(node, ast.AsyncFunctionDef) and node.name in REPLAY_CHECKPOINTS
    }
    assert set(found) == set(REPLAY_CHECKPOINTS), sorted(set(REPLAY_CHECKPOINTS) - set(found))
    return found


class OneHandlerTests(unittest.TestCase):
    def test_no_checkpoint_has_a_separate_cancellation_handler(self) -> None:
        """The anti-drift guard, and the only thing that would have caught this.

        Two handlers wanting the same outcome is how three stages came to do
        something terminal that the other two did not.
        """
        for name, node in _checkpoints().items():
            with self.subTest(activity=name):
                tries = [item for item in node.body if isinstance(item, ast.Try)]
                self.assertEqual(len(tries), 1, f"{name} has {len(tries)} top-level try blocks")
                handlers = [
                    ast.unparse(handler.type) if handler.type else "bare"
                    for handler in tries[0].handlers
                ]
                self.assertEqual(handlers, ["BaseException"], name)

    def test_the_one_handler_releases_before_a_workspace_and_quarantines_after(self) -> None:
        """The graduated shape, read out of the source of all five.

        Asserted structurally because the pre-workspace branch is unreachable
        today (see the module docstring), so no behavioural test of the real code
        path can distinguish it. The behavioural half is
        `test_cancelling_before_the_workspace_releases_the_claim` below, which
        injects the cancellation the runtime cannot deliver.
        """
        handlers = {
            name: ast.unparse(
                [item for item in node.body if isinstance(item, ast.Try)][0].handlers[0]
            )
            for name, node in _checkpoints().items()
        }
        # All five identical, which is the property that failed: two shapes
        # existed and nothing compared them.
        self.assertEqual(len(set(handlers.values())), 1, sorted(handlers))
        body = next(iter(handlers.values()))
        self.assertIn(
            "if workspace_created and (not effect_recorded) and (not _releasable(error)):",
            body,
        )
        self.assertIn("quarantine_operation", body)
        self.assertIn("elif operation_claimed and (not effect_recorded):", body)
        self.assertIn("release_pending_operation", body)
        # The quarantine comes first: a workspace that exists is the reason to
        # refuse a second attempt, so an inverted order would release exactly the
        # case that must not be released.
        self.assertLess(body.index("quarantine_operation"), body.index("release_pending"))
        # And the release is protected. An unprotected one that raised would
        # swallow the CancelledError and report the Activity as failed rather
        # than cancelled, which changes what the retry policy does. The cancel
        # path never had this.
        self.assertIn("add_note", body)
        self.assertIn("except BaseException as release_error:", body)

    def test_the_branch_this_fixes_is_still_unreachable_and_says_so(self) -> None:
        """If an await ever appears before the workspace, this becomes live code.

        Not a reason to remove the check — a reason to know when it stops being
        theoretical. A failure here means the pre-workspace region gained a
        suspension point, so cancellation can now be delivered inside it.
        """
        for name, node in _checkpoints().items():
            with self.subTest(activity=name):
                claim = next(
                    item.lineno
                    for item in ast.walk(node)
                    if isinstance(item, ast.Assign)
                    and isinstance(item.targets[0], ast.Name)
                    and item.targets[0].id == "operation_claimed"
                    and getattr(item.value, "value", None) is True
                )
                workspace = next(
                    item.lineno
                    for item in ast.walk(node)
                    if isinstance(item, ast.Assign)
                    and isinstance(item.targets[0], ast.Name)
                    and item.targets[0].id == "workspace_created"
                    and getattr(item.value, "value", None) is True
                )
                self.assertLess(claim, workspace, name)
                suspensions = [
                    item.lineno
                    for item in ast.walk(node)
                    if isinstance(item, (ast.Await, ast.AsyncWith, ast.AsyncFor))
                    and claim < item.lineno < workspace
                ]
                self.assertEqual(
                    suspensions,
                    [],
                    f"{name} gained a suspension point before its workspace at "
                    f"{suspensions}; cancellation can now be delivered there, so the "
                    "pre-workspace branch is live and needs a behavioural test",
                )


if __name__ == "__main__":
    unittest.main()

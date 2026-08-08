"""The open-item record, made falsifiable.

`docs/ROADMAP.md` carries the list of things that are not done, and it is read by
whoever picks the project up, and it is trusted. (Until 2026-08-08 that list lived
in `docs/IMPLEMENTATION_STATE.md`, now archived under `docs/history/`. `RecordShapeTests`
lived here to stop a struck-through item quietly un-striking in that document; it was
removed with the document, and the new roadmap uses `- [x]` checkboxes, whose shape a
reader cannot misread the way prose can.)

**On 2026-08-07 an audit found five of them stale.** Three said work was open that
had been finished two days earlier (non-destructive cancellation, the F4
prerequisite, the differential's durable home — that last one closed the previous
afternoon). Two were wrong about the code as it stands: `Resolution.smali_path` is
populated at three sites, and `RESERVED_CAPTURE_NAMES` was described as a
restatement of a regex lookahead with no runtime consumer when the regex has no
lookahead, *matches* both reserved names, and the frozenset gained a consumer in
`0b944ea`.

A record that says a thing is open when it is closed is worse than no record: the
next reader either redoes the work or stops looking. And the failure is silent by
construction — nothing executes a sentence in a document.

**So the checkable claims get executed.** Most of what remains open is a
*disconnection* — "nothing imports X", "nothing produces Y", "Z is confined to one
tree" — which is exactly the shape a test can assert. Each test below passes only
while its gap is open, so closing the gap fails the suite and forces the record to
be updated in the same commit. Documentation rot becomes a test failure, which is
the one thing this project reliably notices.

**These tests are meant to be deleted.** A failure here is good news: read the
docstring, confirm the gap really closed, update
`docs/ROADMAP.md`, and remove the test. Do not "fix" one by
loosening it — that re-hides the very thing it exists to surface.

Not everything is checkable this way. "The `type` kind accepts object and
primitive arrays but not every exotic descriptor" is a judgement about coverage,
and an over-count reporting "partially applied" is a wording complaint. Those stay
prose, and that is the honest split.
"""

import ast
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def importers_of(module: str) -> set[str]:
    """Every file under `src/` or `tools/` that really imports `module`.

    An `import` statement, not a mention: both live references to `surface_diff`
    are prose in docstrings, and a grep would have called that module connected.
    """
    found: set[str] = set()
    for path in (*(ROOT / "src").rglob("*.py"), *(ROOT / "tools").rglob("*.py")):
        if path.stem == module:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - not our files
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(module):
                found.add(str(path.relative_to(ROOT)))
            elif isinstance(node, ast.Import):
                if any(alias.name.split(".")[-1] == module for alias in node.names):
                    found.add(str(path.relative_to(ROOT)))
    return found


class DisconnectionTests(unittest.TestCase):
    """Modules that are complete and reached by nothing. The recurring defect."""

    def test_surface_diff_is_still_invoked_by_nothing(self):
        """Stage 3 produces candidates and no production code consumes them.

        Recorded as "invoked by nothing at all — not the driver, not a script, not
        a documented command line; its only mention outside its own tests is prose
        in two docstrings". Still exactly true, and deliberately so: an audit
        settled that stage 3 must NOT feed the feature gate — 44 of its 105
        candidates are permalink spellings of one already-blocked surface, and 90
        cannot be given a legal candidate id.

        So this failing means someone wired it up, which is a decision that needs
        the audit re-read, not a tidy-up.
        """
        self.assertEqual(importers_of("surface_diff"), set())

    def test_claims_py_still_has_no_production_importer(self):
        """The wedged-claim recovery tool is invoked by a human, never by code.

        Correct rather than a gap — releasing a claim is a decision a person makes
        after checking no worker is running — but it means the suite is the only
        thing that has ever exercised it. `reaper.py` deliberately does not call
        it either: removing a directory and releasing a claim are separate
        decisions, and a sweeper that quietly released claims would be doing the
        one thing `claims.py` refuses to do without a typed-back owner token.
        """
        self.assertEqual(importers_of("claims"), set())


class ConfinementTests(unittest.TestCase):
    def test_the_half_declared_toggle_is_still_confined_to_1_3(self):
        """`disable_suggested_posts` has a guard, an id and no way to turn it off.

        `getBoolTrueEz` defaults true, so suggested-post filtering is permanently
        on and un-toggleable, and nothing reports it. It stays a documented
        inaccuracy rather than a pipeline defect *only* because the string appears
        in no other source tree — fixing it would mean editing the CRLF-dirty 1.3
        tree, which is never staged.

        If it ever appears in a tree the pipeline actually ports, that reasoning
        expires and it becomes a real defect.
        """
        present = [
            tree.name
            for tree in sorted(ROOT.glob("dfinsta_source_*"))
            if subprocess.run(
                ["grep", "-rq", "disable_suggested_posts", str(tree)],
                capture_output=True,
            ).returncode
            == 0
        ]
        self.assertEqual(present, ["dfinsta_source_1.3"], present)


class VestigialTests(unittest.TestCase):
    def test_expected_anchor_count_is_still_pinned_to_one(self):
        """Declared per hook, asserted to be 1, so multi-site hooks cannot exist.

        `hook_manifest` refuses anything but 1 and `resolve` asserts it, so the
        field is a placeholder for a feature nobody has needed. A hook with two
        sites needs one entry per site, and this failing means that arrived.
        """
        from dfinsta_pipeline.hook_manifest import Hook  # noqa: PLC0415

        source = (ROOT / "src/dfinsta_pipeline/hook_manifest.py").read_text(encoding="utf-8")
        self.assertIn("expected_anchor_count: int = 1", source)
        self.assertIn("if self.expected_anchor_count != 1:", source)
        self.assertEqual(Hook.__dataclass_fields__["expected_anchor_count"].default, 1)


class LoopBlockingTests(unittest.TestCase):
    def test_five_activities_still_call_load_decoded_tree_on_the_loop(self):
        """Five async Activity bodies call it directly; the gate stage no longer does.

        The record says "decode and build", which is the *measured* half — residual
        blocking on a real 430 port was decode 5%, build 16%, gate 0%. The call
        sites are five, and the difference matters: the other three are cheap in
        practice, not absent.

        Left open because the remaining excursion is 8.9 s against a 300 s
        heartbeat timeout — worth knowing, not worth chasing. And the primitive
        cannot be threaded at its own call sites the way the other two were: four
        of its callers are synchronous validators that already run *inside*
        `asyncio.to_thread`, where there is no loop to await on. The unit that
        moves is the Activity body, one at a time — which is how the gate stage
        went from 100% blocked to 0%.

        "On the loop" is `async def` and nothing else. A call in a plain `def` may
        or may not be threaded depending on its caller, so counting those would
        make this assertion about something it cannot see.
        """
        tree = ast.parse(
            (ROOT / "src/dfinsta_pipeline/activities.py").read_text(encoding="utf-8")
        )
        on_the_loop: set[str] = set()

        def visit(node: ast.AST, enclosing: str | None) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    inner = child.name if isinstance(child, ast.AsyncFunctionDef) else None
                    visit(child, inner)
                    continue
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Name)
                    and child.func.id == "load_decoded_tree"
                    and enclosing is not None
                ):
                    on_the_loop.add(enclosing)
                visit(child, enclosing)

        visit(tree, None)
        self.assertEqual(
            on_the_loop,
            {
                "replay_install_frameworks_checkpoint_activity",
                "replay_decode_checkpoint_activity",
                "replay_apply_tree_checkpoint_activity",
                "replay_build_patched_apk_checkpoint_activity",
                "replay_verify_final_apk_checkpoint_activity",
            },
            sorted(on_the_loop),
        )
        # The gate stage is the control: it used to be here and was moved, so an
        # empty set above would mean the walk broke rather than the work finished.
        self.assertNotIn("prepare_replay_verification_gate_activity", on_the_loop)


if __name__ == "__main__":
    unittest.main()

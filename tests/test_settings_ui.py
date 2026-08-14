"""A toggle with no working settings row blocks an endpoint forever.

`throwIfBlocked` blocks when `getBoolTrueEz(key)` is true, and `getBoolTrueEz` is
`getBoolean(key, true)` — **one hardcoded default, for every key**. There is no
per-key default anywhere in the shipped tree. So a preference key the settings
dialog has never heard of does not default to off; it defaults to on, the
endpoint is blocked, and the user has no way to unblock it.

Nothing detected that before this module. `guards.Rule` checks only the
`disable_` prefix, and `rulings.existing_preference_keys` reads the keys out of
`throwIfBlocked` itself — so it returns whatever the manifest just declared and
is structurally incapable of noticing a key with no row.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from dfinsta_pipeline.guards import rules_from_manifest, toggles_of
from dfinsta_pipeline.settings_ui import (
    WRAPPER_PATH,
    SettingsError,
    check,
    coverage,
    main,
    read_rows,
)

REPOSITORY = Path(__file__).resolve().parent.parent
SOURCE = REPOSITORY / "dfinsta_source_439"


class ShippedTreeTests(unittest.TestCase):
    """The tree that actually ships, read as the dialog builds itself."""

    def setUp(self) -> None:
        if not (SOURCE / WRAPPER_PATH).is_file():
            self.skipTest("no 439 custom-code tree on this machine")

    def test_every_toggle_the_guard_reads_has_a_row_that_works(self) -> None:
        rows = check(REPOSITORY / "manifest" / "hooks.json", SOURCE)
        declared = toggles_of(rules_from_manifest(REPOSITORY / "manifest" / "hooks.json"))
        self.assertEqual(set(declared), set(rows.keys))
        self.assertEqual(5, len(rows.keys))

    def test_the_indices_are_read_and_not_assumed_from_order(self) -> None:
        """The dialog holds its indices in registers and reuses them —
        `aput-object v5, v4, v7` puts the string in v5 at the index in v7, set
        forty lines earlier. Document order happens to match array order today;
        trusting it would be trusting the thing worth checking."""
        rows, _ = coverage(REPOSITORY / "manifest" / "hooks.json", SOURCE)
        self.assertEqual({0, 1, 2, 3, 4}, set(rows.labels))
        self.assertEqual({0, 1, 2, 3, 4}, set(rows.read))
        self.assertEqual({0, 1, 2, 3, 4}, set(rows.written))
        self.assertEqual("Disable Stories", rows.labels[3])
        self.assertEqual("disable_stories", rows.read[3])
        self.assertEqual("disable_stories", rows.written[3])


class MutatedWrapperTests(unittest.TestCase):
    """Each way the wiring can be wrong, against a real dialog with one edit.

    Built by editing the shipped smali rather than by writing a fixture, because
    a fixture is a description of the file and these tests are about the file.
    """

    def setUp(self) -> None:
        if not (SOURCE / WRAPPER_PATH).is_file():
            self.skipTest("no 439 custom-code tree on this machine")
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.wrapper = self.root / WRAPPER_PATH
        self.wrapper.parent.mkdir(parents=True)
        self.original = (SOURCE / WRAPPER_PATH).read_text(encoding="utf-8")
        self.wrapper.write_text(self.original, encoding="utf-8")
        self.manifest = self.root / "hooks.json"
        self.manifest.write_text(
            (REPOSITORY / "manifest" / "hooks.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    def edit(self, old: str, new: str) -> None:
        text = self.wrapper.read_text(encoding="utf-8")
        self.assertEqual(1, text.count(old), f"fixture premise: {old!r} appears once")
        self.wrapper.write_text(text.replace(old, new), encoding="utf-8")

    def refuse(self) -> str:
        with self.assertRaises(SettingsError) as caught:
            check(self.manifest, self.root)
        return str(caught.exception)

    def test_the_unmutated_tree_passes(self) -> None:
        """The control. Every refusal below must be caused by its own edit."""
        self.assertEqual(5, len(check(self.manifest, self.root).keys))

    def test_a_row_that_renders_and_writes_nothing_is_refused(self) -> None:
        """Refusal C's silent failure: wired into the dialog, missed in the
        dispatch. It renders, animates, reports itself checked, and never touches
        SharedPreferences — `onClick`'s chain ends `if-ne p2, v0, :cond_return`
        and `:cond_return` is `return-void`."""
        self.edit(
            '    if-ne p2, v0, :cond_return\n\n    const-string v0, "disable_adds"',
            "    if-ne p2, v0, :cond_return",
        )
        message = self.refuse()
        self.assertIn("disable_adds", message)
        self.assertIn("writes nothing", message)

    def test_a_row_that_writes_another_rows_key_is_refused(self) -> None:
        """The quieter one: label at one index, dispatch at another. Tapping a row
        changes a setting the user did not touch, and nothing on screen says so."""
        self.edit(
            '    if-ne p2, v0, :cond_profile_ads\n\n    const-string v0, "disable_stories"',
            '    if-ne p2, v0, :cond_profile_ads\n\n    const-string v0, "disable_reels"',
        )
        message = self.refuse()
        self.assertIn("row 3", message)
        self.assertIn("did not touch", message)

    def test_a_toggle_with_no_row_at_all_is_refused_and_says_why(self) -> None:
        """The one that ships an endpoint nobody can unblock."""
        data = json.loads(self.manifest.read_text(encoding="utf-8"))
        hook = next(h for h in data["hooks"] if h["hook_id"] == "tigon_url_block")
        hook["url_block_rules"][0]["toggles"] = ["disable_stories_v2"]
        self.manifest.write_text(json.dumps(data, indent=1), encoding="utf-8")
        message = self.refuse()
        self.assertIn("disable_stories_v2", message)
        self.assertIn("cannot be unblocked", message)
        self.assertIn("getBoolean(key, true)", message)

    def test_a_row_for_a_key_no_rule_reads_is_refused(self) -> None:
        """Harmless to the user, and it is how the dangerous one arrives: the
        dialog and the manifest have drifted."""
        data = json.loads(self.manifest.read_text(encoding="utf-8"))
        hook = next(h for h in data["hooks"] if h["hook_id"] == "tigon_url_block")
        hook["url_block_rules"] = [
            rule for rule in hook["url_block_rules"]
            if "disable_adds" not in rule["toggles"]
        ]
        self.manifest.write_text(json.dumps(data, indent=1), encoding="utf-8")
        message = self.refuse()
        self.assertIn("disable_adds", message)
        self.assertIn("does nothing", message)

    def test_an_index_this_reader_cannot_follow_is_refused_not_skipped(self) -> None:
        """A store at an index held in a register nobody set. Skipping it would
        drop a row from the answer and report a smaller, consistent dialog."""
        self.edit("    const/4 v7, 0x2\n", "")
        message = self.refuse()
        self.assertIn("cannot follow", message)

    def test_two_copies_of_a_method_are_refused(self) -> None:
        """Reading the wrong copy would report a dialog nobody ships."""
        text = self.wrapper.read_text(encoding="utf-8")
        start = text.index(".method public final onClick(")
        end = text.index(".end method", start) + len(".end method")
        self.wrapper.write_text(text + "\n" + text[start:end], encoding="utf-8")
        self.assertIn("appears 2 times", self.refuse())


class CommandTests(unittest.TestCase):
    def test_it_exits_zero_and_prints_the_rows(self) -> None:
        if not (SOURCE / WRAPPER_PATH).is_file():
            self.skipTest("no 439 custom-code tree on this machine")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--manifest", str(REPOSITORY / "manifest" / "hooks.json"),
                         "--custom-code", str(SOURCE)])
        self.assertEqual(0, code)
        self.assertIn("disable_stories", out.getvalue())

    def test_a_missing_tree_is_refused_rather_than_read_as_no_toggles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = main(["--manifest", str(REPOSITORY / "manifest" / "hooks.json"),
                             "--custom-code", tmp])
            self.assertEqual(2, code)
            self.assertIn("refused", err.getvalue())

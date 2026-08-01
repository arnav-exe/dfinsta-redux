"""Mutation tests for the candidate validator.

A validator that only ever says OK is worse than none, because it reads as
coverage. Every check therefore has a negative test that breaks exactly one thing
and asserts the verdict flips. Each mutation mirrors a mistake that really
happened while porting 430 or 439.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from validate_candidates import validate  # noqa: E402

SMALI = """\
.class public LX/0Test;
.super Ljava/lang/Object;


# direct methods
.method public static final A00(Landroid/content/Context;)V
    .locals 4

    new-instance v0, LX/0Aaa;

    invoke-direct {v0}, LX/0Aaa;-><init>()V

    invoke-static {v0, v1}, LX/0Bbb;->A00(Ljava/lang/Object;Ljava/lang/Object;)V

    iput-object v2, v1, LX/0Ccc;->A0H:Landroid/view/View$OnLongClickListener;

    new-instance v0, LX/0Ddd;

    invoke-direct {v0, v1}, LX/0Ddd;-><init>(Ljava/lang/Object;)V

    invoke-virtual {v3, v0}, LX/0Eee;->A0Y(Ljava/lang/Object;)V

    return-void
.end method

.method public static final A01(Landroid/content/Context;)V
    .locals 3

    iput-object v2, v1, LX/0Ccc;->A0H:Landroid/view/View$OnLongClickListener;

    return-void
.end method
"""

GOOD = {
    "id": "good",
    "descriptor": "LX/0Test;",
    "mode": "insert_after",
    "anchor": [
        "new-instance v0, LX/0Aaa;",
        "invoke-direct {v0}, LX/0Aaa;-><init>()V",
        "invoke-static {v0, v1}, LX/0Bbb;->A00(Ljava/lang/Object;Ljava/lang/Object;)V",
    ],
    "expected_anchor_count": 1,
    "marker": "Lcom/dfinstagram/SettingsWrapper;",
    "expected_marker_count": 2,
    "payload": [
        "    new-instance v0, Lcom/dfinstagram/SettingsWrapper;",
        "    invoke-direct {v0}, Lcom/dfinstagram/SettingsWrapper;-><init>()V",
    ],
}


class ValidateCandidatesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        (root / "smali_classes2" / "X").mkdir(parents=True)
        (root / "smali_classes2" / "X" / "0Test.smali").write_text(SMALI, encoding="utf-8")
        self.decode = root
        self.addCleanup(self.dir.cleanup)

    def verdict(self, op):
        return validate(self.decode, [op])[0]

    def test_known_good_candidate_passes(self):
        row = self.verdict(dict(GOOD))
        self.assertEqual(row["verdict"], "OK", row.get("reason"))
        self.assertTrue(row["anchor_unique"])
        self.assertEqual(row["anchor_occurrences"], 1)

    def test_leading_whitespace_anchor_is_caught(self):
        """The exact 439 reels defect: the applier compares against line.strip()."""
        op = dict(GOOD, anchor=["    " + a for a in GOOD["anchor"]])
        row = self.verdict(op)
        # stripped matching still finds it, but the whitespace is reported so the
        # manifest can be fixed before it reaches a patcher that does not strip.
        self.assertFalse(row["anchor_whitespace_clean"])

    def test_non_unique_anchor_is_caught(self):
        """The A0H trap: the same iput occurs in two different branches."""
        op = dict(GOOD, anchor=["iput-object v2, v1, LX/0Ccc;->A0H:Landroid/view/View$OnLongClickListener;"])
        row = self.verdict(op)
        self.assertEqual(row["anchor_occurrences"], 2)
        self.assertFalse(row["anchor_unique"])
        self.assertEqual(row["verdict"], "BROKEN")

    def test_unresolvable_descriptor_is_caught(self):
        """Obfuscated names are recycled; a missing class must fail loudly."""
        row = self.verdict(dict(GOOD, descriptor="LX/0Missing;"))
        self.assertEqual(row["verdict"], "BROKEN")
        self.assertFalse(row["descriptor_resolves"])

    def test_anchor_that_does_not_exist_is_caught(self):
        row = self.verdict(dict(GOOD, anchor=["const/4 v0, 0x9"]))
        self.assertEqual(row["verdict"], "BROKEN")
        self.assertEqual(row["anchor_occurrences"], 0)
        self.assertFalse(row["anchor_matches"])

    def test_preexisting_marker_is_caught(self):
        """A marker already in the file means a partially applied patch."""
        row = self.verdict(dict(GOOD, marker="LX/0Aaa;"))
        self.assertFalse(row["marker_absent"])
        self.assertEqual(row["verdict"], "BROKEN")

    def test_register_clobber_is_caught(self):
        """v1 is read two instructions later, so a payload writing v1 corrupts it."""
        op = dict(GOOD, payload=["    const/4 v1, 0x0"])
        row = self.verdict(op)
        self.assertFalse(row["registers_safe"])
        self.assertEqual(row["verdict"], "BROKEN")
        self.assertIn("v1", row["registers_note"])

    def test_safe_register_reuse_is_allowed(self):
        """v0 is immediately rewritten by new-instance, so writing it is fine."""
        op = dict(GOOD, payload=["    const/4 v0, 0x0"])
        row = self.verdict(op)
        self.assertTrue(row["registers_safe"], row["registers_note"])
        self.assertEqual(row["verdict"], "OK")


if __name__ == "__main__":
    unittest.main()

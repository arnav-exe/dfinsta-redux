"""Tests for the generaliser — the stage that turns an agent finding into a rule.

The module exists to make one number fall, so every test here is written from the
question "what would let a wrong fingerprint through", not from the code's shape.

    1. **One version cannot tell a fingerprint from a coincidence.** This is not a
       hypothetical: the systrace string the 439 proposers cited selects exactly
       one class on 439 and it is the right one, and exactly one class on 430 and
       it is the *wrong* one. Every single-version check passes it. The
       cross-version measurement is the only thing that does not, so it is the
       first thing tested and it is tested against the real decodes.

    2. **A forbidden value is never proposed.** An obfuscated descriptor, a
       register, a member name and a resource id are the four values that turn a
       durable store into a confident wrong answer — `decisions.FORBIDDEN_SIGNAL`
       exists for the first and 103 of 11,737 drawable ids survived 430->439 for
       the last. `ForbiddenValueTests` puts each of them where the search would
       have to trip over it, and keeps a positive control in the same fixture so
       the test cannot pass by finding nothing at all.

    3. **"No fingerprint found" is a result.** A generaliser that quietly returns
       nothing and one that says the hook still needs an agent read the same in a
       results file and mean opposite things. The refusal is asserted to be
       explicit, and `host_entry()` is asserted to raise rather than emit an empty
       fingerprint.

The selectivity tests use the real 439 and 430 decodes. They are the measurement
the whole claim rests on and a fixture cannot stand in for them: a synthetic tree
would agree with whatever the implementation happened to do. `setUpClass` runs
the two real hooks once and every test in that class reads the same result, so
the decodes are walked a handful of times rather than once per assertion.
"""

import json
import tempfile
import unittest
from pathlib import Path

from dfinsta_pipeline.generalise import (
    BLOCK_ANCHOR_TEXT,
    BLOCK_NOT_INDEXED,
    BLOCK_SEMANTIC_DEP,
    VERDICT_AMBIGUOUS,
    VERDICT_WRONG,
    GeneraliseError,
    KnownHost,
    Proposal,
    Selection,
    forbidden_reason,
    generalise_host,
    read_discovery,
    with_blocks,
    write_proposals,
)
from dfinsta_pipeline.hook_index import HookIndex
from dfinsta_pipeline.hook_manifest import load_manifest

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "manifest" / "hooks.json"
DISCOVERY = REPO / "work" / "439-autonomous" / "discovery.json"
DECODE_439 = REPO / "work" / "439-explore" / "stock-439"
DECODE_430 = REPO / "work" / "430-clean-build-v2" / "stock-430"
INDEX_439 = REPO / "work" / "index-439"

#: The 430 hosts, as the previous port established them and as the manifest notes
#: record them ("430 LX/077K -> 439 LX/0DnT"). Version-stamped and used only as an
#: expectation to check, never as a lookup key.
HOSTS_430 = {
    "install_settings_long_click": ("LX/077K;", "smali_classes6/X/077K.smali"),
    "install_settings_long_click_actionbar": ("LX/06X7;", "smali_classes6/X/06X7.smali"),
}

#: The literal the 439 proposers cited for `install_settings_long_click`. It names
#: the class's own former identity, it is exactly the kind of thing this module was
#: built to promote, and it is wrong.
SYSTRACE = "ProfileActionBarViewBinder.bindUsernameTitle.setAutoSizeTextTypeUniformWithConfiguration"

HAVE_DECODES = DECODE_439.is_dir() and DECODE_430.is_dir() and DISCOVERY.is_file()


def write_class(root: Path, relative: str, descriptor: str, literals) -> str:
    """One smali class carrying exactly the given string constants."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    body = [f".class public final {descriptor}", ".super Ljava/lang/Object;", ".method public a()V"]
    body += [f'    const-string v{index}, "{value}"' for index, value in enumerate(literals)]
    body += ["    return-void", ".end method"]
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return relative


@unittest.skipUnless(HAVE_DECODES, "needs the real 439 and 430 decodes")
class RealDecodeSelectivityTests(unittest.TestCase):
    """The measurement. Real decodes, both settings hooks, one run for the class."""

    @classmethod
    def setUpClass(cls):
        cls.hooks = {hook.hook_id: hook for hook in load_manifest(MANIFEST)}
        cls.index = HookIndex.for_decode(INDEX_439, DECODE_439)
        cls.proposals = {}
        for found in read_discovery(DISCOVERY):
            descriptor, path = HOSTS_430[found.hook_id]
            cls.proposals[found.hook_id] = generalise_host(
                found.hook_id,
                KnownHost("439", DECODE_439, found.descriptor, found.smali_path),
                [KnownHost("430", DECODE_430, descriptor, path)],
                evidence=found.evidence,
            )

    def test_an_intersection_that_selects_one_class_on_both_versions_is_proposed(self):
        proposal = self.proposals["install_settings_long_click_actionbar"]
        self.assertTrue(proposal.found, proposal.reason)
        self.assertEqual(
            proposal.literals,
            ("notifications_entry_point_impression", "ig4a-instagram-schema"),
        )
        measured = {item.version: item for item in proposal.selections}
        self.assertEqual(measured["439"].sample, ("LX/0Di2;",))
        self.assertEqual(measured["430"].sample, ("LX/06X7;",))
        for version, item in measured.items():
            with self.subTest(version=version):
                self.assertEqual(item.count, 1)
                self.assertTrue(item.exact)

    def test_a_literal_that_selects_several_classes_is_rejected_and_an_intersection_is_tried(self):
        # The primary literal is in three classes on 439 on its own. It is refused
        # as a fingerprint and then kept as half of one, which is exactly what
        # `co_literals` does for the Reels hooks.
        proposal = self.proposals["install_settings_long_click_actionbar"]
        alone = {item.literals: item for item in proposal.rejected}
        entry = alone[("notifications_entry_point_impression",)]
        self.assertEqual(entry.selections[0].verdict, VERDICT_AMBIGUOUS)
        self.assertEqual(entry.selections[0].count, 3)
        self.assertIn("notifications_entry_point_impression", proposal.literals)
        self.assertGreater(len(proposal.literals), 1)

    def test_a_literal_that_selects_the_wrong_class_on_the_other_version_is_rejected(self):
        # The whole reason this module measures a second version. On 439 the cited
        # systrace string selects exactly LX/0DnT;, the agreed host. On 430 it
        # selects exactly one class too — and it is ProfileActionBar, not LX/077K;.
        proposal = self.proposals["install_settings_long_click"]
        rejected = {item.literals: item for item in proposal.rejected}
        entry = rejected[(SYSTRACE,)]
        on_439, on_430 = entry.selections
        self.assertTrue(on_439.exact)
        self.assertEqual(on_430.verdict, VERDICT_WRONG)
        self.assertEqual(on_430.count, 1)
        self.assertEqual(on_430.sample, ("Lcom/instagram/profile/actionbar/ProfileActionBar;",))
        # The rejection has to say WHICH version refused it and WHAT it picked
        # there, or a reader cannot tell this from a literal that simply missed.
        self.assertIn("430", entry.reason)
        self.assertIn("Lcom/instagram/profile/actionbar/ProfileActionBar;", entry.reason)

    def test_a_hook_with_no_durable_literal_says_so_rather_than_proposing_nothing(self):
        proposal = self.proposals["install_settings_long_click"]
        self.assertFalse(proposal.found)
        self.assertIn("no durable fingerprint found", proposal.reason)
        # The two hosts share exactly one string constant across the versions, and
        # it is in six classes in each. Naming it is what makes this an answer.
        self.assertIn("Threads", proposal.reason)
        self.assertIn("still needs an agent", proposal.reason)
        with self.assertRaises(GeneraliseError):
            proposal.host_entry()

    def test_the_things_that_would_refuse_the_proposal_are_reported(self):
        hook = self.hooks["install_settings_long_click_actionbar"]
        proposal = with_blocks(
            self.proposals["install_settings_long_click_actionbar"], hook, self.index
        )
        blocked = {where for where, _ in proposal.blocks}
        self.assertEqual(
            blocked, {BLOCK_SEMANTIC_DEP, BLOCK_ANCHOR_TEXT, BLOCK_NOT_INDEXED}
        )
        # A proposal that reads as promotable and resolves to nothing is how the
        # agent count falls without anything having been learned.
        self.assertFalse(proposal.mechanical)
        for literal in proposal.literals:
            with self.subTest(literal=literal):
                self.assertFalse(self.index.literal_is_indexed(literal))


class FixtureTests(unittest.TestCase):
    """Two synthetic versions, so the refusals can be attacked without a 4 GB decode."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="generalise-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))
        new, old = self.root / "new", self.root / "old"
        # The host in the new version carries one admissible literal, one shared
        # with a sibling, one shared with another sibling, and four values that a
        # careless extractor would happily promote.
        self.host_path = write_class(
            new,
            "smali/host.smali",
            "LX/0AAA;",
            [
                "unique-here",
                "shared-one",
                "shared-two",
                "LX/0AAA;",
                "v0",
                "A03",
                "0x7f082538",
            ],
        )
        write_class(new, "smali/b.smali", "LX/0BBB;", ["shared-one"])
        write_class(new, "smali/c.smali", "LX/0CCC;", ["shared-two"])
        # The old host carries the same four forbidden values, so each of them is a
        # PERFECT single-literal fingerprint by every measurement this module takes:
        # one class here, one class there, and the right one both times. Nothing but
        # `forbidden_reason` stands between them and the manifest, which is what
        # makes this fixture an attack rather than an illustration.
        self.old_path = write_class(
            old,
            "smali/host.smali",
            "LX/0ZZZ;",
            ["shared-one", "shared-two", "LX/0AAA;", "v0", "A03", "0x7f082538"],
        )
        write_class(old, "smali/d.smali", "LX/0DDD;", ["unique-here"])
        write_class(old, "smali/e.smali", "LX/0EEE;", ["shared-one"])
        self.subject = KnownHost("new", new, "LX/0AAA;", self.host_path)
        self.older = KnownHost("old", old, "LX/0ZZZ;", self.old_path)

    def propose(self, **kwargs) -> Proposal:
        return generalise_host("fixture_hook", self.subject, [self.older], **kwargs)

    def test_no_forbidden_value_is_ever_proposed(self):
        attacks = {
            "obfuscated descriptor": "LX/0AAA;",
            "register": "v0",
            "member name": "A03",
            "resource id": "0x7f082538",
        }
        # Each is a genuine string constant of the host, so provenance alone would
        # admit every one of them, AND each is suggested outright.
        proposal = self.propose(hints=list(attacks.values()))
        refused = {item.literals[0]: item.reason for item in proposal.rejected}
        for label, value in attacks.items():
            with self.subTest(attack=label):
                self.assertNotIn(value, proposal.literals)
                self.assertIn(value, refused)
                self.assertIn("never a fingerprint", refused[value])
                self.assertTrue(forbidden_reason(value))
        # Positive control, in the same fixture and the same call: the admissible
        # literal that is not forbidden IS proposed, so this test cannot pass by
        # the search having found nothing at all.
        self.assertTrue(proposal.found, proposal.reason)
        self.assertEqual(proposal.literals, ("shared-one", "shared-two"))
        self.assertEqual(forbidden_reason("shared-one"), "")

    def test_a_literal_correct_here_and_wrong_there_loses_to_an_intersection(self):
        proposal = self.propose()
        rejected = {item.literals: item for item in proposal.rejected}
        self.assertEqual(rejected[("unique-here",)].selections[1].verdict, VERDICT_WRONG)
        self.assertEqual(proposal.literals, ("shared-one", "shared-two"))

    def test_one_version_alone_yields_no_fingerprint(self):
        proposal = generalise_host("fixture_hook", self.subject)
        self.assertFalse(proposal.found)
        self.assertIn("only one version", proposal.reason)
        # `unique-here` selects exactly the host on the subject version, so a
        # generaliser that skipped the cross-version check would propose it here.
        self.assertNotIn("unique-here", proposal.literals)

    def test_a_proposal_cannot_be_built_from_a_measurement_that_is_not_exact(self):
        wrong = Selection.measure("old", ["unique-here"], ["LX/0DDD;"], "LX/0ZZZ;")
        right = Selection.measure("new", ["unique-here"], ["LX/0AAA;"], "LX/0AAA;")
        self.assertEqual(wrong.verdict, VERDICT_WRONG)
        with self.assertRaises(GeneraliseError) as caught:
            Proposal("h", "by_literal", "unique-here", selections=(right, wrong), reason="r")
        self.assertIn("proposed despite", str(caught.exception))
        self.assertIn("LX/0DDD;", str(caught.exception))
        with self.assertRaises(GeneraliseError) as single:
            Proposal("h", "by_literal", "unique-here", selections=(right,), reason="r")
        self.assertIn("not corroborated", str(single.exception))

    def test_the_proposal_round_trips_into_a_valid_hook_via_load_manifest(self):
        proposal = self.propose()
        entry = json.loads(MANIFEST.read_text(encoding="utf-8"))
        hook = next(
            item
            for item in entry["hooks"]
            if item["hook_id"] == "install_settings_long_click_actionbar"
        )
        hook["hosts"] = [proposal.host_entry()]
        target = self.root / "hooks-proposed.json"
        target.write_text(
            json.dumps({"schema_version": 1, "hooks": [hook]}, indent=2), encoding="utf-8"
        )
        loaded = load_manifest(target)
        self.assertEqual(loaded[0].hosts[0].kind, "by_literal")
        self.assertEqual(loaded[0].hosts[0].literal, proposal.literal)
        self.assertEqual(loaded[0].hosts[0].co_literals, proposal.co_literals)
        self.assertTrue(loaded[0].hosts[0].note.strip())

    def test_write_proposals_refuses_to_write_a_hook_manifest(self):
        proposal = self.propose()
        for name in ("hooks.json", "renamed-manifest.json"):
            target = self.root / name
            if name != "hooks.json":
                # Content, not just the filename: a manifest saved under another
                # name is still the file a human has to be the one to change.
                target.write_text(
                    json.dumps({"schema_version": 1, "hooks": []}), encoding="utf-8"
                )
            with self.subTest(target=name), self.assertRaises(GeneraliseError) as caught:
                write_proposals(target, [proposal], "new")
            self.assertIn("proposes", str(caught.exception))
        # Positive control: an ordinary path is written, and says it committed nothing.
        written = write_proposals(self.root / "proposals.json", [proposal], "new")
        document = json.loads(written.read_text(encoding="utf-8"))
        self.assertFalse(document["committed"])
        self.assertEqual(document["proposals"][0]["host_entry"]["literal"], proposal.literal)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

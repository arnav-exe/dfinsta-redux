"""Reading the bottom nav off the phone, and what it says when it cannot.

This module drives every measurement the project has and had **no tests at all**
until 2026-08-15 — the same distribution problem as the runtime-probe module,
found the same way: by changing it and looking for the test that should have
failed.

The refusal is the part worth pinning. `tabs()` has two very different failure
modes that read identically from outside — the screen never held still, or
Instagram renamed the ids — and 440 really did rename them, so the second is not
hypothetical. On 442 it was the first: the walk refused 68 seconds after
`adb install -r`, while Android was still compiling the new build, and the
message named both causes without saying which it had seen. Working that out by
hand cost a walk. Everything needed to say it was already in the loop.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

REPOSITORY = Path(__file__).resolve().parents[1]

TAB_ROW = 2274


def load():
    """Load `tools/device_session.py` as a module, without running it."""
    spec = importlib.util.spec_from_file_location(
        "device_session", REPOSITORY / "tools" / "device_session.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["device_session"] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop("device_session", None)
    return module


def hierarchy(nodes: dict[str, int]) -> ET.Element:
    """A UI dump holding `resource-id -> x centre`, all on one row."""
    root = ET.Element("hierarchy")
    for rid, x in nodes.items():
        ET.SubElement(
            root,
            "node",
            {
                "resource-id": f"com.instagram.android:id/{rid}",
                "bounds": f"[{x - 40},{TAB_ROW - 40}][{x + 40},{TAB_ROW + 40}]",
            },
        )
    return root


#: What the phone really returned on 439, 440, 441 and 442.
REAL_NAV = {"feed_tab": 108, "clips_tab": 324, "direct_tab": 540,
            "search_tab": 756, "profile_tab": 972}


class BottomNavTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load()
        # Nothing here may reach the phone. `sh` is the only door to it, and a
        # test that opened it would tap a real device.
        patcher = mock.patch.object(self.module, "sh", lambda *a: "")
        patcher.start()
        self.addCleanup(patcher.stop)
        # The retry loop sleeps 8s plus 6s a time; five attempts is 38 seconds of
        # test suite otherwise.
        naps = mock.patch.object(self.module.time, "sleep", lambda seconds: None)
        naps.start()
        self.addCleanup(naps.stop)

    def stub_dump(self, *returns):
        """Successive `dump()` results; the last repeats for later attempts."""
        sequence = list(returns)

        def fake():
            return sequence.pop(0) if len(sequence) > 1 else sequence[0]

        patcher = mock.patch.object(self.module, "dump", fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_it_reads_every_tab_centre_from_the_dump(self) -> None:
        self.stub_dump(hierarchy(REAL_NAV))
        found = self.module.tabs()
        self.assertEqual(
            {"Home": (108, TAB_ROW), "Reels": (324, TAB_ROW), "Message": (540, TAB_ROW),
             "Search and explore": (756, TAB_ROW), "Profile": (972, TAB_ROW)},
            found,
        )

    def test_it_retries_before_refusing(self) -> None:
        """A screen that settles on the third look is not a failure."""
        self.stub_dump(None, None, hierarchy(REAL_NAV))
        self.assertIn("Profile", self.module.tabs())

    def test_no_dump_at_all_reads_as_a_screen_that_never_held_still(self) -> None:
        """The 442 case: `adb install -r`, then a walk 68 seconds later."""
        self.stub_dump(None)
        with self.assertRaises(SystemExit) as caught:
            self.module.tabs()
        message = str(caught.exception)
        self.assertIn("no dump succeeded", message)
        self.assertIn("install", message)
        # And it must NOT accuse the ids, which is the expensive wrong turn.
        self.assertNotIn("what a rename looks like", message)

    def test_dumps_without_any_tab_id_read_as_the_wrong_screen(self) -> None:
        self.stub_dump(hierarchy({"login_button": 500}))
        with self.assertRaises(SystemExit) as caught:
            self.module.tabs()
        message = str(caught.exception)
        self.assertIn("not the screen we think it is", message)
        # It may say "not a rename"; it must not say this IS one.
        self.assertNotIn("what a rename looks like", message)

    def test_some_ids_present_but_not_all_reads_as_a_rename(self) -> None:
        """440 did exactly this, and it is the one case worth chasing."""
        self.stub_dump(hierarchy({"feed_tab": 108, "profile_tab": 972}))
        with self.assertRaises(SystemExit) as caught:
            self.module.tabs()
        self.assertIn("what a rename looks like", str(caught.exception))

    def test_a_nav_that_is_not_one_row_is_refused(self) -> None:
        """Five ids scattered down the screen are five different things that
        happen to share a name, not a nav bar."""
        root = hierarchy(REAL_NAV)
        for offset, node in enumerate(root.iter("node")):
            y = TAB_ROW - offset * 200
            x1, x2 = 100 + offset, 180 + offset
            node.set("bounds", f"[{x1},{y - 40}][{x2},{y + 40}]")
        self.stub_dump(root)
        with self.assertRaises(SystemExit) as caught:
            self.module.tabs()
        self.assertIn("not in one row", str(caught.exception))


class TabIdTests(unittest.TestCase):
    """The ids themselves, which are the thing a version bump breaks."""

    def test_the_four_required_tabs_are_a_subset_of_the_ids_it_knows(self) -> None:
        module = load()
        required = {"Home", "Profile", "Reels", "Search and explore"}
        self.assertTrue(required <= set(module.TAB_IDS.values()))

    def test_the_profile_guess_is_only_a_way_onto_a_static_screen(self) -> None:
        """It must never be used as a coordinate to act on: every tap the session
        makes is read back from a dump, so a wrong guess can only stop a run.
        Pinned because the comment saying so is the only thing that keeps it true.
        """
        source = (REPOSITORY / "tools" / "device_session.py").read_text(encoding="utf-8")
        uses = source.count("PROFILE_GUESS")
        self.assertEqual(2, uses, "declared once, used once — to reach a dumpable screen")


class WarmUpTests(unittest.TestCase):
    """The step that waits for Android to finish compiling a fresh install.

    Two defects, found on 443 and fixed together.

    **It sampled once.** One launch, sixteen seconds, one `tabs()` call — against
    a wait whose length is a property of the phone and the build, and which
    nobody here knows. 442 refused 68 seconds after the install and 443 refused
    twice, both with a message about the nav ids that reads exactly like the
    rename 440 really did. A function whose entire job is to wait has to wait.

    **It left nothing behind.** `tools/port.py` tells a step that ran from a step
    that did not by looking for its artefact, and warm had none — its check was
    "the first walk has produced a capture", which warm does not do. So it could
    never both run and pass. See `EveryStepCanFinishItsOwnStepTests` in
    `test_port_runbook.py` for that half; this is the half that writes the file.
    """

    def setUp(self) -> None:
        self.module = load()
        self.commands: list[tuple] = []
        patcher = mock.patch.object(
            self.module, "sh", lambda *a: self.commands.append(a) or ""
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        naps = mock.patch.object(self.module.time, "sleep", lambda seconds: None)
        naps.start()
        self.addCleanup(naps.stop)

    def stub_tabs(self, *results):
        """Successive `tabs()` results; the last repeats. A `SystemExit` instance
        is raised rather than returned, which is how `tabs()` really refuses."""
        sequence = list(results)

        def fake():
            item = sequence.pop(0) if len(sequence) > 1 else sequence[0]
            if isinstance(item, BaseException):
                raise item
            return item

        patcher = mock.patch.object(self.module, "tabs", fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def nav(self) -> dict:
        return {name: (x, TAB_ROW) for name, x in
                zip(("Home", "Reels", "Message", "Search and explore", "Profile"),
                    (108, 324, 540, 756, 972))}

    # ---- it writes what it read ---------------------------------------------

    def test_it_writes_the_nav_it_read(self) -> None:
        """The artefact `port.py` checks for. Without this file the runbook
        refuses the step as having reported success without doing the work."""
        import json
        import tempfile

        self.stub_tabs(self.nav())
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "sub" / "warm.json"
            self.assertEqual(0, self.module.warm(out))
            self.assertTrue(out.is_file(), "warm exited 0 and wrote nothing")
            written = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(
            ["Home", "Message", "Profile", "Reels", "Search and explore"],
            written["nav"],
            "and the file must say what was read, not merely that it was",
        )

    def test_it_still_works_with_nowhere_to_write(self) -> None:
        """`--out` is optional, so a hand-run warm-up stays a one-word command."""
        self.stub_tabs(self.nav())
        self.assertEqual(0, self.module.warm(None))

    def test_it_writes_nothing_when_it_refuses(self) -> None:
        """An artefact from a failed warm-up would make the runbook skip the step
        on the next attempt — the failure would become permanent and invisible."""
        import tempfile

        self.stub_tabs(SystemExit("no dump succeeded"))
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "warm.json"
            with self.assertRaises(SystemExit):
                self.module.warm(out)
            self.assertFalse(out.is_file())

    # ---- it waits -----------------------------------------------------------

    def test_a_nav_that_reads_on_the_second_launch_is_not_a_failure(self) -> None:
        """The 443 case exactly: the first look found none of the tab ids and a
        look a minute later found all five."""
        self.stub_tabs(SystemExit("nav is missing"), self.nav())
        self.assertEqual(0, self.module.warm(None))

    def test_it_relaunches_between_attempts(self) -> None:
        """Re-reading the same screen would not help. What is being waited for is
        the app starting faster once Android has compiled it, so each attempt has
        to be a fresh launch."""
        self.stub_tabs(SystemExit("nav is missing"), self.nav())
        launches = [c for c in self.commands if "monkey" in c]
        self.assertEqual(0, len(launches), "sanity: counted before the call")
        self.commands.clear()
        self.stub_tabs(SystemExit("nav is missing"), self.nav())
        self.module.warm(None)
        self.assertEqual(2, len([c for c in self.commands if "monkey" in c]))
        self.assertEqual(2, len([c for c in self.commands if "force-stop" in c]))

    def test_it_gives_up_eventually_and_names_both_causes(self) -> None:
        """It must not loop forever, and the message must not pick a side: on 440
        the ids really were renamed, and on 442 and 443 they were not."""
        self.stub_tabs(SystemExit("nav is missing ['clips_tab']"))
        with self.assertRaises(SystemExit) as caught:
            self.module.warm(None)
        message = str(caught.exception)
        self.assertIn(str(self.module.WARM_ATTEMPTS), message)
        self.assertIn("compiling", message)
        self.assertIn("renamed", message)
        self.assertIn("clips_tab", message, "and it must carry what the phone said")

    def test_it_makes_more_than_one_attempt(self) -> None:
        """Pinned as a number, because `range(1)` is a one-character regression
        that restores the original defect and changes no other behaviour."""
        self.assertGreater(self.module.WARM_ATTEMPTS, 1)
        self.stub_tabs(SystemExit("nav is missing"))
        with self.assertRaises(SystemExit):
            self.module.warm(None)
        self.assertEqual(
            self.module.WARM_ATTEMPTS,
            len([c for c in self.commands if "monkey" in c]),
        )

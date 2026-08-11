"""Run one measurement arm, deriving every coordinate from the phone.

Nothing here is hardcoded. Instagram 440 renamed the bottom-nav resource ids and
every probe surface stopped navigating **silently**; a day later two arms had
Reels and Explore swapped because the positions had been carried forward from a
previous version. So tabs are found by `content-desc` and settings rows by their
text, on the version actually installed, and a tab that cannot be found is a
refusal rather than a tap into empty space.

Usage:  session.py <toggle-name|none> <output.log>
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ADB = [str(Path.home() / "Android/Sdk/platform-tools/adb"), "-s", "P3227J000775"]
PKG = "com.instagram.android"
WALK = ("Home", "Search and explore", "Reels")


def sh(*args: str) -> str:
    return subprocess.run(ADB + list(args), capture_output=True, text=True, timeout=240).stdout


def dump() -> ET.Element | None:
    """A fresh UI snapshot, or None. Never falls back to a previous one.

    A failed `uiautomator dump` leaves the last successful capture on the device,
    so reading it back compares a screen against itself — that produced an invalid
    dataset here once and nearly got reported.
    """
    sh("shell", "rm", "-f", "/sdcard/s.xml")
    sh("shell", "uiautomator", "dump", "/sdcard/s.xml")
    text = sh("shell", "cat", "/sdcard/s.xml")
    if "<hierarchy" not in text:
        return None
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        return None


def centres(root: ET.Element, want: str) -> dict[str, tuple[int, int]]:
    """`content-desc` -> centre, for nodes whose desc matches `want`."""
    found: dict[str, tuple[int, int]] = {}
    for node in root.iter("node"):
        desc = (node.get("content-desc") or "").strip()
        box = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", "") or "")
        if not desc or not box or not re.search(want, desc):
            continue
        x1, y1, x2, y2 = map(int, box.groups())
        found.setdefault(desc, ((x1 + x2) // 2, (y1 + y2) // 2))
    return found


#: The bottom nav's resource ids. Selecting on these rather than on a label is
#: the whole fix: `content-desc="Reels"` also matches a *content* node near the
#: top of the feed, and taking the first match in document order sent four of
#: eight 439 arms and six of twelve 440 arms to (379, 220) — the top of the
#: screen — instead of the Reels tab, for three taps each. Silently. That is the
#: same failure as 440 renaming these ids, a third time, inside the rewrite whose
#: docstring says it exists to prevent it. A label describes what a thing says; an
#: id says what it is.
TAB_IDS = {"feed_tab": "Home", "clips_tab": "Reels", "search_tab": "Search and explore",
           "profile_tab": "Profile", "direct_tab": "Message"}


#: Where the Profile tab has sat on 439, 440 and 441. Used ONLY to reach a screen
#: that can be dumped; every coordinate actually used is read back from that dump,
#: and the run refuses if the nav is not there. So a wrong guess here cannot send a
#: session somewhere unrecorded — it can only stop it.
PROFILE_GUESS = (972, 2274)


def tabs() -> dict[str, tuple[int, int]]:
    """Every tab's centre, read from the nav's resource ids on a static screen.

    Two failures shaped this. Selecting by `content-desc` matched a *content* node
    labelled "Reels" near the top of the feed and, taking the first match in
    document order, sent four of eight 439 arms and six of twelve 440 arms to
    (379, 220) for three taps each — silently. A label says what a thing reads; an
    id says what it is.

    And the feed cannot be dumped at all: `uiautomator` never reaches idle while a
    video autoplays, which it does on launch, so every dump there returns "could
    not get idle state" — disabling the animation scales does not help. The profile
    grid is static, so the run steps onto it first and reads the nav from there.
    The nav is identical on every main tab, so where we stand does not matter; only
    that the screen holds still.
    """
    sh("shell", "input", "tap", *map(str, PROFILE_GUESS))
    time.sleep(8)
    for attempt in range(5):
        root = dump()
        if root is not None:
            found: dict[str, tuple[int, int]] = {}
            for node in root.iter("node"):
                rid = (node.get("resource-id") or "").rsplit("/", 1)[-1]
                box = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", "") or "")
                if rid in TAB_IDS and box:
                    x1, y1, x2, y2 = map(int, box.groups())
                    found[TAB_IDS[rid]] = ((x1 + x2) // 2, (y1 + y2) // 2)
            if {"Home", "Profile", "Reels", "Search and explore"} <= set(found):
                ys = {y for _, y in found.values()}
                if max(ys) - min(ys) > 40:
                    raise SystemExit(f"refusing: nav ids are not in one row: {found}")
                return found
        time.sleep(6)
    raise SystemExit(
        "refusing: could not read the bottom nav from the profile screen. Either the "
        "screen never went idle, or the ids changed again — check which before "
        "assuming the second."
    )


def rows() -> dict[str, tuple[int, bool]]:
    """Settings rows by label -> (y, checked). Found by text, not position."""
    root = dump()
    out: dict[str, tuple[int, bool]] = {}
    if root is None:
        return out
    for node in root.iter("node"):
        if "CheckedTextView" not in (node.get("class") or ""):
            continue
        box = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
        label = (node.get("text") or "").strip().lower()
        key = {"disable feed": "disable_feed", "disable explore": "disable_explore",
               "disable reels": "disable_reels", "disable stories": "disable_stories",
               "disable profile ads": "disable_adds"}.get(label)
        if key and box:
            out[key] = ((int(box.group(2)) + int(box.group(4))) // 2,
                        node.get("checked") == "true")
    return out


def set_toggles(on: set[str], nav: dict[str, tuple[int, int]]) -> dict[str, bool]:
    sh("shell", "input", "tap", *map(str, nav["Profile"])); time.sleep(8)
    root = dump()
    options = centres(root, r"^Options$") if root is not None else {}
    if not options:
        raise SystemExit("refusing: no Options control on the profile")
    x, y = next(iter(options.values()))
    sh("shell", "input", "swipe", str(x), str(y), str(x), str(y), "900"); time.sleep(4)
    for _ in range(4):
        current = rows()
        if not current:
            raise SystemExit("refusing: the settings dialog did not open")
        wrong = [k for k, (_, state) in current.items() if (k in on) != state]
        if not wrong:
            return {k: s for k, (_, s) in current.items()}
        sh("shell", "input", "tap", "540", str(current[wrong[0]][0])); time.sleep(2)
    raise SystemExit(f"refusing: could not reach the toggle state {on or 'all off'}")


def main() -> int:
    name, out = sys.argv[1], Path(sys.argv[2])
    on: set[str] = set() if name == "none" else {name}

    sh("shell", "am", "force-stop", PKG); time.sleep(3)
    sh("shell", "monkey", "-p", PKG, "-c", "android.intent.category.LAUNCHER", "1")
    time.sleep(16)
    nav = tabs()
    missing = [t for t in WALK if t not in nav]
    if missing:
        raise SystemExit(f"refusing: nav is missing {missing}; found {sorted(nav)}")

    state = set_toggles(on, nav)
    if {k for k, v in state.items() if v} != on:
        raise SystemExit(f"refusing: toggles read back as {state}, wanted {on or 'all off'}")
    sh("shell", "input", "keyevent", "KEYCODE_BACK"); time.sleep(2)

    sh("shell", "am", "force-stop", PKG); time.sleep(3)
    sh("logcat", "-G", "16M"); sh("logcat", "-c")
    sh("shell", "monkey", "-p", PKG, "-c", "android.intent.category.LAUNCHER", "1")
    time.sleep(18)
    # THREE rounds, not one pass. Measured on 440: a single pass observed 11 and 16
    # requests where 439 saw 19 and 21, and the derivation correctly refused to
    # classify a real erasure because a fall of 1 cannot clear a noise floor of 1.
    # The cure is signal, not a looser rule.
    #
    # Rounds rather than more scrolling, because the actions that cost a request
    # are re-entering a surface and pulling to refresh. Scrolling inside content
    # the app has already loaded mostly costs nothing until pagination.
    for round_number in range(3):
        for surface in WALK:
            sh("shell", "input", "tap", *map(str, nav[surface])); time.sleep(8)
            if surface == "Home":
                sh("shell", "input", "swipe", "540", "900", "540", "1900", "400")
                time.sleep(11)
            for _ in range(5):
                sh("shell", "input", "swipe", "540", "1700", "540", "500", "280")
                time.sleep(3)
    out.write_text(sh("logcat", "-d"), encoding="utf-8")
    print(f"  {name}: {out.name}  nav={ {k: v for k, v in sorted(nav.items())} }")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Run one measurement arm, deriving every coordinate from the phone.

Nothing here is hardcoded. Instagram 440 renamed the bottom-nav resource ids and
every probe surface stopped navigating **silently**; a day later two arms had
Reels and Explore swapped because the positions had been carried forward from a
previous version. So tabs are found by `content-desc` and settings rows by their
text, on the version actually installed, and a tab that cannot be found is a
refusal rather than a tap into empty space.

Usage:  device_session.py <toggle-name|none> <output.log> [walk]
        device_session.py --warm
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

#: The named protocols, as `(rounds, scrolls per surface)`. The name is what a
#: session records, and two spellings of one protocol silently halve every group,
#: so they live here rather than being typed at the command line.
#:
#: `one-pass-v1` is what 439's committed corpus was measured with, kept exactly so
#: a later version can be compared against it without the walk being a difference.
#: `three-round-v2` observes roughly twice as much, which matters because a fall
#: to zero cannot clear a noise floor unless the baseline was large enough to fall
#: from — but a comparison between two versions measured differently says nothing
#: about either.
WALKS = {"one-pass-v1": (1, 6), "three-round-v2": (3, 5)}


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
    dumps = 0
    seen_any_tab_id = False
    for attempt in range(5):
        root = dump()
        if root is not None:
            dumps += 1
            found: dict[str, tuple[int, int]] = {}
            for node in root.iter("node"):
                rid = (node.get("resource-id") or "").rsplit("/", 1)[-1]
                box = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", "") or "")
                if rid in TAB_IDS and box:
                    seen_any_tab_id = True
                    x1, y1, x2, y2 = map(int, box.groups())
                    found[TAB_IDS[rid]] = ((x1 + x2) // 2, (y1 + y2) // 2)
            if {"Home", "Profile", "Reels", "Search and explore"} <= set(found):
                ys = {y for _, y in found.values()}
                if max(ys) - min(ys) > 40:
                    raise SystemExit(f"refusing: nav ids are not in one row: {found}")
                return found
        time.sleep(6)
    # Say which of the two it was. The refusal used to name both causes and leave
    # the operator to dump the screen by hand — which cost a walk and half an hour
    # on 442, where it turned out to be the first and reads exactly like the
    # second. The evidence to tell them apart was already in this loop.
    if dumps == 0:
        why = (
            "no dump succeeded at all in 5 attempts, so the screen never held still. "
            "The commonest cause is the FIRST launch after `adb install -r`, while "
            "Android is still compiling the new build: launch the app by hand, wait "
            "for the profile grid, and run this again"
        )
    elif not seen_any_tab_id:
        why = (
            f"{dumps} dump(s) succeeded and NONE of {sorted(TAB_IDS)} was in any of "
            "them, so this is not the screen we think it is — a dialog, an "
            "interstitial or a logged-out app, not a rename"
        )
    else:
        why = (
            f"{dumps} dump(s) succeeded and some of {sorted(TAB_IDS)} were present but "
            "not all four required ones. THIS is what a rename looks like: dump the "
            "screen and compare the ids before changing anything"
        )
    raise SystemExit(f"refusing: could not read the bottom nav from the profile screen. {why}")


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
    # Generous, because the budget is per *attempt* and an attempt can be spent on
    # nothing: a dump taken while the dialog is still animating reads the state it
    # had before the last tap, so the same toggle gets flipped twice and flipped
    # back. Four attempts was enough for a warm app and not for the first launch
    # after a fresh install, which is where a corpus starts. Twenty costs seconds
    # when it is not needed and an hour of walking when it is.
    seen: dict[str, bool] = {}
    for _ in range(20):
        current = rows()
        if not current:
            raise SystemExit("refusing: the settings dialog did not open")
        seen = {k: s for k, (_, s) in current.items()}
        wrong = [k for k, (_, state) in current.items() if (k in on) != state]
        if not wrong:
            return seen
        sh("shell", "input", "tap", "540", str(current[wrong[0]][0])); time.sleep(3)
    # What it saw, not just what it wanted. A refusal naming only the target tells
    # the next person nothing about which toggle would not move.
    raise SystemExit(
        f"refusing: could not reach the toggle state {on or 'all off'}; the dialog "
        f"last read {seen}"
    )


def warm() -> int:
    """Launch the app once and wait until the nav can be read. No session.

    **Why this exists as a step of its own.** `adb install -r` is followed by
    Android compiling the new build, so the first launch after an install is far
    slower than any later one — slower than the ~38 seconds `tabs()` allows. On
    442 the first walk refused 68 seconds after the install and the message read
    exactly like the ids having been renamed, which is a thing that really
    happened on 440. It cost a walk and half an hour.

    It reads the nav with `tabs()` — the same function every session uses, not a
    copy — so a warm-up that succeeds is a precondition the sessions have already
    passed once. Nothing about the walk protocol changes: this runs *before* the
    corpus, and a session still force-stops and relaunches for itself, so what a
    session measures is what it measured before this existed.
    """
    sh("shell", "am", "force-stop", PKG)
    time.sleep(3)
    sh("shell", "monkey", "-p", PKG, "-c", "android.intent.category.LAUNCHER", "1")
    time.sleep(16)
    nav = tabs()
    missing = [tab for tab in WALK if tab not in nav]
    if missing:
        raise SystemExit(f"refusing: nav is missing {missing}; found {sorted(nav)}")
    print(f"  warm: nav reads {sorted(nav)}")
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--warm":
        return warm()
    name, out = sys.argv[1], Path(sys.argv[2])
    walk = sys.argv[3] if len(sys.argv) > 3 else "three-round-v2"
    if walk not in WALKS:
        raise SystemExit(f"refusing: unknown walk {walk!r}; known: {', '.join(WALKS)}")
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
    rounds, scrolls = WALKS[walk]
    # Rounds rather than more scrolling: the actions that cost a request are
    # re-entering a surface and pulling to refresh, whereas scrolling inside
    # content the app has already loaded costs nothing until pagination.
    for _ in range(rounds):
        for surface in WALK:
            # Announce the surface into the log the capture is already reading,
            # BEFORE the tap that causes its requests. Same stream, same device
            # clock, so attribution is exact rather than inferred from this
            # function's own timing — and the app announces nothing itself,
            # because every tab is a fragment of one activity.
            #
            # It does not change what a session measures. The taps, swipes and
            # sleeps are identical; this adds one shell call of a few tens of
            # milliseconds against sleeps of 8 and 3 seconds, so corpora stay
            # comparable across the change.
            sh("shell", "log", "-t", "DFInstaWalk", f"surface={surface}")
            sh("shell", "input", "tap", *map(str, nav[surface])); time.sleep(8)
            if surface == "Home":
                sh("shell", "input", "swipe", "540", "900", "540", "1900", "400")
                time.sleep(11)
            for _ in range(scrolls):
                sh("shell", "input", "swipe", "540", "1700", "540", "500", "280")
                time.sleep(3)
    out.write_text(sh("logcat", "-d"), encoding="utf-8")
    print(f"  {name} [{walk}]: {out.name}  nav={ {k: v for k, v in sorted(nav.items())} }")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

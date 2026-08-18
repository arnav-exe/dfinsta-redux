"""Which surface was on screen when a path was requested.

The question this exists for: a new endpoint is discovered, the phone really does
request it, and someone has to decide which of the five switches should own it —
or whether it needs a sixth. Until now that was answered from the path's *name*,
and a name that reads like a feature is not evidence a feature exists.
`delivery/background_prefetch` was ruled `block` from its name and turned out not
to be an endpoint at all.

**The app announces nothing.** Instagram runs every tab as a fragment of one
activity, so switching tabs produces no activity transition and there is no
screen-change marker anywhere in the log — checked against a real 442 capture.
So the walk annotates the same stream it is capturing, and every observation
after a marker belongs to that surface.

Measured on the phone 2026-08-17, in a three-minute annotated walk:
`/discover/topical_explore` appeared only while Explore was on screen, and
`/feed/reels_media_stream/` appeared on **Home** — Reels content injected into
the feed, which is exactly what a human had guessed from its name in August and
could not previously show.

**A request is not caused by the surface it appears on**, and nothing here may
imply it is. A path seen only on Reels is strong evidence it serves Reels; a path
seen everywhere is evidence of nothing in particular. The record says where, and
a human still decides what that means.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dfinsta_pipeline.grouping import _where_seen, render
from dfinsta_pipeline.observation import (
    STARTUP,
    ObservationError,
    ObservationSession,
    SurfaceCounts,
    parse,
)

REPOSITORY = Path(__file__).resolve().parents[1]
HEADER = "08-08 17:31:00.000 1 1 I DFInstaObserve: !toggles +blocked disable_reels=0\n"


def capture(*lines: str) -> str:
    return HEADER + "".join(
        f"08-08 17:31:{n:02d}.000 1 1 {line}\n" for n, line in enumerate(lines, 1)
    )


class ParsingTests(unittest.TestCase):
    def test_a_request_belongs_to_the_last_surface_announced(self) -> None:
        found = parse(capture(
            "I DFInstaWalk: surface=Home",
            "I DFInstaObserve: /feed/timeline/",
            "I DFInstaWalk: surface=Reels",
            "I DFInstaObserve: /clips/discover/stream/",
            "I DFInstaObserve: /clips/discover/stream/",
        ))
        self.assertEqual(
            {"Home": {"/feed/timeline/": 1}, "Reels": {"/clips/discover/stream/": 2}},
            found.per_surface.as_dict(),
        )

    def test_requests_before_the_first_marker_are_startup_not_the_first_tab(self) -> None:
        """App startup fires requests before any tap. Calling them Home — because
        Home is where the app opens — would be inventing an attribution. On the
        real 442 walk `/feed/reels_tray/` appeared ONLY here."""
        found = parse(capture(
            "I DFInstaObserve: /feed/reels_tray/",
            "I DFInstaWalk: surface=Home",
            "I DFInstaObserve: /feed/timeline/",
        ))
        self.assertEqual(
            {STARTUP: {"/feed/reels_tray/": 1}, "Home": {"/feed/timeline/": 1}},
            found.per_surface.as_dict(),
        )

    def test_a_capture_with_no_marker_attributes_nothing(self) -> None:
        """Absent, not "everything happened at startup". A capture taken by hand
        has no walk to annotate it, and that is normal rather than historical."""
        found = parse(capture("I DFInstaObserve: /feed/timeline/"))
        self.assertIsNone(found.per_surface)

    def test_the_marker_is_not_counted_as_a_request(self) -> None:
        found = parse(capture(
            "I DFInstaWalk: surface=Home",
            "I DFInstaObserve: /feed/timeline/",
        ))
        self.assertEqual({"/feed/timeline/": 1}, dict(found.counts))

    def test_an_empty_or_padded_surface_name_is_refused(self) -> None:
        for message in ("I DFInstaWalk: surface=", "I DFInstaWalk: surface=  Home "):
            with self.subTest(message=message):
                with self.assertRaises(ObservationError):
                    parse(capture(message))

    def test_a_line_merely_mentioning_the_tag_is_not_a_marker(self) -> None:
        """The `adbd` line echoing the shell command contains the tag."""
        found = parse(capture(
            "I adbd    : shell,v2,raw:log -t DFInstaWalk surface=Reels",
            "I DFInstaObserve: /feed/timeline/",
        ))
        self.assertIsNone(found.per_surface)

    def test_the_same_path_on_two_surfaces_is_counted_on_both(self) -> None:
        found = parse(capture(
            "I DFInstaWalk: surface=Home",
            "I DFInstaObserve: /clips/discover/stream/",
            "I DFInstaWalk: surface=Reels",
            "I DFInstaObserve: /clips/discover/stream/",
        ))
        self.assertEqual(
            (("Home", 1), ("Reels", 1)),
            found.per_surface.surfaces_for("/clips/discover/stream/"),
        )


class RecordTests(unittest.TestCase):
    def test_busiest_surface_first(self) -> None:
        """The lookup exists to answer "which switch owns this", so the answer
        should be readable off the front."""
        counts = SurfaceCounts({"Home": {"/x/": 1}, "Reels": {"/x/": 9}})
        self.assertEqual((("Reels", 9), ("Home", 1)), counts.surfaces_for("/x/"))

    def test_a_recorded_zero_is_refused(self) -> None:
        with self.assertRaises(ObservationError):
            SurfaceCounts({"Home": {"/x/": 0}})

    def test_an_empty_surface_name_is_refused(self) -> None:
        with self.assertRaises(ObservationError):
            SurfaceCounts({" ": {"/x/": 1}})

    def test_a_path_no_surface_saw_returns_nothing(self) -> None:
        self.assertEqual((), SurfaceCounts({"Home": {"/x/": 1}}).surfaces_for("/y/"))

    def session(self, **overrides):
        fields = dict(
            schema_version=1, version="443", build_sha256="a" * 64,
            recorded_at="2026-08-18T00:00:00Z", session_id="s", surface="feed",
            watched=("/x/",), toggles=None, walk="one-pass-v1",
            span_seconds=2, counts={"/x/": 1},
        )
        fields.update(overrides)
        return ObservationSession(**fields)

    def test_it_round_trips_through_a_row(self) -> None:
        counts = SurfaceCounts({"Reels": {"/x/": 1}})
        row = self.session(per_surface=counts).to_dict()
        self.assertEqual({"Reels": {"/x/": 1}}, row["per_surface"])
        self.assertEqual(counts, ObservationSession.from_dict(row).per_surface)

    def test_a_row_from_an_unannotated_walk_keeps_its_absence(self) -> None:
        row = self.session().to_dict()
        self.assertNotIn("per_surface", row)
        self.assertIsNone(ObservationSession.from_dict(row).per_surface)

    def test_a_null_is_refused_as_a_second_spelling_of_absent(self) -> None:
        row = self.session().to_dict()
        row["per_surface"] = None
        with self.assertRaises(ObservationError):
            ObservationSession.from_dict(row)


class State:
    """Only what `_where_seen` uses."""

    def __init__(self, label, sessions):
        self.label = label
        self.sessions = sessions

    def surfaces_for(self, endpoint):
        totals = {}
        for item in self.sessions:
            if item is None:
                continue
            for surface, count in item.surfaces_for(endpoint):
                totals[surface] = totals.get(surface, 0) + count
        return tuple(sorted(totals.items(), key=lambda pair: (-pair[1], pair[0])))


class WhereSeenTests(unittest.TestCase):
    def test_it_sums_across_states_busiest_first(self) -> None:
        """One session is not evidence of where a path belongs."""
        home = SurfaceCounts({"Home": {"/x/": 2}})
        reels = SurfaceCounts({"Reels": {"/x/": 5}})
        found = _where_seen("/x/", State("baseline", [home]), [State("arm", [reels])])
        self.assertEqual(("requested on Reels x5, Home x2",), found)

    def test_it_is_silent_when_no_session_was_annotated(self) -> None:
        """Absent, never "seen nowhere"."""
        self.assertEqual((), _where_seen("/x/", State("baseline", [None]), []))

    def test_a_path_nobody_requested_says_nothing(self) -> None:
        counts = SurfaceCounts({"Home": {"/other/": 1}})
        self.assertEqual((), _where_seen("/x/", State("baseline", [counts]), []))


class ReportTests(unittest.TestCase):
    """It reaches the page a human rules from — through a real store.

    Not by handing `render` a dict: that dict would be this test's idea of a
    report, and what is worth checking is that a surface recorded in a session
    comes out on the page.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        store = self.root / "manifest" / "observations"
        store.mkdir(parents=True)
        (self.root / "manifest" / "hooks.json").write_text(
            (REPOSITORY / "manifest" / "hooks.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        names = ("disable_adds", "disable_explore", "disable_feed",
                 "disable_reels", "disable_stories")
        rows = []
        for state, on in (("none", set()), ("reels", {"disable_reels"})):
            for run in (1, 2):
                rows.append({
                    "schema_version": 1, "version": "998", "build_sha256": "b" * 64,
                    "recorded_at": f"2026-08-18T00:0{run}:00Z",
                    "session_id": f"998-{state}-{run}",
                    "surface": "feed_explore_reels", "walk": "one-pass-v1",
                    "span_seconds": 100,
                    "watched": ["/clips/discover/stream/", "/feed/timeline/"],
                    "toggles": {name: name in on for name in names},
                    "counts": (
                        {"/feed/timeline/": 5} if on
                        else {"/clips/discover/stream/": 9, "/feed/timeline/": 5}
                    ),
                    "total": 5 if on else 14,
                    "per_surface": (
                        {"Home": {"/feed/timeline/": 5}} if on
                        else {"Home": {"/feed/timeline/": 5},
                              "Reels": {"/clips/discover/stream/": 9}}
                    ),
                })
        (store / "998.jsonl").write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
        )

    def test_a_surface_recorded_in_a_session_comes_out_on_the_page(self) -> None:
        from dfinsta_pipeline.grouping import summary

        rendered = render(summary("998", self.root, walk="one-pass-v1"))
        self.assertIn("@ requested on Reels x18", rendered)

    def test_the_marks_stay_distinct_on_one_endpoint(self) -> None:
        """`@` where the app asked, `*` what our code did. Two questions, two
        marks, so nobody has to parse prose to tell them apart."""
        from dfinsta_pipeline.grouping import Classification

        row = Classification(
            endpoint="/x/", verdict="unaffected", toggle=None, reason="r",
            execution=("a_hook ran in all 4 session(s)",),
            seen_on=("requested on Reels x5",),
        ).to_dict()
        self.assertEqual(["requested on Reels x5"], row["seen_on"])
        self.assertEqual(["a_hook ran in all 4 session(s)"], row["execution"])


class RedactionTests(unittest.TestCase):
    def redact(self, text: str) -> str:
        import importlib.util
        import sys

        spec = importlib.util.spec_from_file_location(
            "redact_capture", REPOSITORY / "tools" / "redact_capture.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["redact_capture"] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop("redact_capture", None)
        return module.redact(text)

    def test_a_marker_survives_and_the_capture_still_parses_the_same(self) -> None:
        """Without the marker the committed capture could not corroborate the
        row's attribution — the same gap probes had until yesterday."""
        text = capture(
            "I DFInstaWalk: surface=Reels",
            "I DFInstaObserve: /clips/discover/stream/",
            "I SomeOtherTag: unrelated telemetry",
        )
        reduced = self.redact(text)
        self.assertIn("DFInstaWalk: surface=Reels", reduced)
        self.assertNotIn("unrelated telemetry", reduced)
        self.assertEqual(parse(text), parse(reduced))


class TheWalkAnnouncesItselfTests(unittest.TestCase):
    """The harness half. Nothing else in the log says which tab is on screen."""

    def test_the_walk_marks_each_surface_before_tapping_it(self) -> None:
        source = (REPOSITORY / "tools" / "device_session.py").read_text(encoding="utf-8")
        marker = source.index('"log", "-t", "DFInstaWalk"')
        tap = source.index('"input", "tap", *map(str, nav[surface])')
        self.assertLess(marker, tap, "the marker must precede the tap that causes the requests")

    def test_it_marks_every_surface_the_walk_visits(self) -> None:
        import importlib.util
        import sys

        spec = importlib.util.spec_from_file_location(
            "device_session", REPOSITORY / "tools" / "device_session.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["device_session"] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop("device_session", None)
        # The marker is emitted inside the loop over WALK, so every surface the
        # walk visits announces itself; a surface added to WALK cannot be missed.
        source = (REPOSITORY / "tools" / "device_session.py").read_text(encoding="utf-8")
        self.assertIn("for surface in WALK:", source)
        self.assertIn('f"surface={surface}"', source)
        self.assertEqual(("Home", "Search and explore", "Reels"), module.WALK)

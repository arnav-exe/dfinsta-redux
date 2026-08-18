"""The launch window as the unsolicited signal, and the producer that was missing.

The gate asks a human "did a user action cause this content to appear?"
(`test_consent_test.py`). This is the only measurement this project has that
bears on the answer: between process start and the first surface marker the
harness has tapped nothing, so every request in that window arrived unbidden.

**The signal is one-directional and the code has to keep saying so.** A high
launch-window count is evidence of unsolicited delivery. A low one is *not*
evidence of solicited delivery, because a request that follows a tap need not be
a request for what the tap asked for — which is exactly why generic
recommendations and search sit at 100% and 33% in Lukoff's bands with the same
tap in front of both. Every summary that prints the number carries the caveat,
and `AsymmetryTests` is what stops the caveat being dropped as boilerplate.

**A dedicated idle probe was designed and dropped, on measurement.** The app goes
37 to 76 seconds without requesting a watched path *while being swiped every
three seconds*, so a 30-second window of silence measures nothing. The launch
window costs no extra walk and has no user action in it at all.

`ProducerTests` is the one that found a real defect. `parse` had produced
`per_surface` and `probes` for days, the record type held them, the store round-
tripped them and the reader consumed them — and the `record` command that is the
only writer of `manifest/observations/*.jsonl` **passed neither**, so a row
recorded through the documented runbook said "nobody looked" about a build that
was reporting on every hook. Same shape as every other producer gap here: every
piece complete and tested, and the line that moves the value between two of them
absent.
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from dfinsta_pipeline.device_evidence import DeviceReading, _consent_note, _evidence
from dfinsta_pipeline.feature_gate import DEVICE_REQUESTED
from dfinsta_pipeline.grouping import _where_seen
from dfinsta_pipeline.observation import STARTUP, ObservationSession, SurfaceCounts, parse

REPOSITORY = Path(__file__).resolve().parents[1]
HEADER = "08-08 17:31:00.000 1 1 I DFInstaObserve: !toggles +blocked disable_reels=0\n"


def capture(*lines: str) -> str:
    return HEADER + "".join(
        f"08-08 17:31:{n:02d}.000 1 1 {line}\n" for n, line in enumerate(lines, 1)
    )


# ------------------------------------------------------------------ the split


class ConsentSplitTests(unittest.TestCase):
    def test_the_launch_window_and_the_surfaces_are_counted_apart(self) -> None:
        counts = SurfaceCounts({
            STARTUP: {"/feed/timeline/": 3, "/clips/discover": 2},
            "Reels": {"/clips/discover": 7},
        })
        self.assertEqual((3, 0), counts.consent_split("/feed/timeline/"))
        self.assertEqual((2, 7), counts.consent_split("/clips/discover"))

    def test_a_path_nobody_requested_splits_to_nothing(self) -> None:
        counts = SurfaceCounts({STARTUP: {"/feed/timeline/": 3}})
        self.assertEqual((0, 0), counts.consent_split("/never/"))

    def test_every_named_surface_counts_as_after_a_tap(self) -> None:
        """Only `STARTUP` is the unsolicited side; every other name is the other.

        A second special surface name added later must not silently join the
        unsolicited side — that would widen the strongest claim this project
        makes without anyone deciding to.
        """
        counts = SurfaceCounts({
            STARTUP: {"/x/": 1}, "Home": {"/x/": 2}, "Reels": {"/x/": 4},
            "(anything else)": {"/x/": 8},
        })
        self.assertEqual((1, 14), counts.consent_split("/x/"))

    def test_it_agrees_with_the_parser_on_a_real_capture(self) -> None:
        """The split is only worth anything if it lines up with what parse does.

        Requests before the first marker are `STARTUP`; the two after `Home` are
        not. Computed from a capture rather than a hand-built record, so a change
        to either side has to keep them agreeing.
        """
        found = parse(capture(
            "I DFInstaObserve: /feed/timeline/",
            "I DFInstaObserve: /feed/timeline/",
            "I DFInstaWalk: surface=Home",
            "I DFInstaObserve: /feed/timeline/",
        ))
        assert found.per_surface is not None
        self.assertEqual((2, 1), found.per_surface.consent_split("/feed/timeline/"))


# --------------------------------------------------------------- the producer


class ProducerTests(unittest.TestCase):
    """The `record` command must write what `parse` read.

    Both fields are asserted together because they were dropped together and by
    the same omission: a construction that names every other field the capture
    carries and simply does not mention these two.
    """

    def _record(self, text: str, root: Path) -> dict:
        watched = root / "watched.txt"
        watched.write_text("/feed/timeline/\n", encoding="utf-8")
        source = root / "capture.log"
        source.write_text(text, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-B", "-m", "dfinsta_pipeline.observation",
             "--root", str(root), "record",
             "--version", "443", "--build-sha256", "a" * 64,
             "--recorded-at", "2026-08-18T00:00:00Z", "--session-id", "s1",
             "--surface", "feed", "--walk", "one-pass-v1",
             "--watched-from", str(watched), "--capture", str(source)],
            capture_output=True, text=True, cwd=REPOSITORY,
            env={"PYTHONPATH": str(REPOSITORY / "src"), "PATH": "/usr/bin:/bin",
                 "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual(0, result.returncode, result.stderr)
        line = (root / "manifest" / "observations" / "443.jsonl").read_text().splitlines()[0]
        return json.loads(line)

    def test_the_recorded_row_carries_the_surface_attribution(self) -> None:
        with TemporaryDirectory() as directory:
            row = self._record(capture(
                "I DFInstaObserve: /feed/timeline/",
                "I DFInstaWalk: surface=Home",
                "I DFInstaObserve: /feed/timeline/",
            ), Path(directory))
        self.assertEqual(
            {STARTUP: {"/feed/timeline/": 1}, "Home": {"/feed/timeline/": 1}},
            row["per_surface"],
        )

    def test_the_recorded_row_carries_the_probe_lines(self) -> None:
        with TemporaryDirectory() as directory:
            row = self._record(capture(
                "I DFInstaObserve: /feed/timeline/",
                "I DFInstaProbe: tigon_url_block",
                "I DFInstaProbe: tigon_url_block",
            ), Path(directory))
        self.assertEqual({"tigon_url_block": 2}, row["probes"])

    def test_an_unannotated_walk_records_no_attribution_rather_than_an_empty_one(self) -> None:
        """Absence survives the trip. A row that said `{}` would claim the walk
        attributed every request to no surface, which is a measurement nobody
        made."""
        with TemporaryDirectory() as directory:
            row = self._record(capture("I DFInstaObserve: /feed/timeline/"), Path(directory))
        self.assertNotIn("per_surface", row)
        self.assertIsNone(ObservationSession.from_dict(row).per_surface)


# ------------------------------------------------------------- what it reports


class ReadingTests(unittest.TestCase):
    def test_an_unattributed_corpus_reports_null_and_not_zero(self) -> None:
        """The distinction this whole module exists for, one level further in.

        `0` would say the app asked for nothing at launch. `None` says no walk
        was in a position to tell, which is what an unannotated corpus supports.
        """
        reading = DeviceReading("x", watched_in=(("443", "one-pass-v1"),), sessions=4, seen=9)
        self.assertEqual(0, reading.attributed_sessions)
        self.assertIsNone(reading.unsolicited)
        self.assertIsNone(reading.solicited)
        self.assertIsNone(reading.as_dict()["unsolicited"])
        self.assertIsNone(reading.as_dict()["solicited"])

    def test_the_counts_reach_the_machine_readable_view(self) -> None:
        reading = DeviceReading(
            "x", watched_in=(("443", "one-pass-v1"),), sessions=4, seen=9,
            attributed_sessions=4, unsolicited=3, solicited=6,
        )
        data = reading.as_dict()
        self.assertEqual(3, data["unsolicited"])
        self.assertEqual(6, data["solicited"])
        self.assertEqual(4, data["attributed_sessions"])


class ComputingTheSplitTests(unittest.TestCase):
    """`reading_for` over a real store, not a hand-built reading.

    Every test above this builds a `DeviceReading` directly, which exercises what
    the split *reports* and none of what computes it. Two mutations survived that
    way — one that counted an unannotated session as annotated, one that reported
    a measured zero for a corpus nobody annotated — and both are only reachable
    through the store.
    """

    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = self.root / "manifest" / "observations"
        self.store.mkdir(parents=True)

    def row(self, session_id: str, counts: dict, per_surface: dict | None = None) -> dict:
        row = {
            "schema_version": 1,
            "version": "443",
            "build_sha256": "c" * 64,
            "recorded_at": "2026-08-18T00:00:00Z",
            "session_id": session_id,
            "surface": "feed_explore_reels",
            "watched": ["/feed/timeline/"],
            "toggles": {"disable_feed": False},
            "walk": "one-pass-v1",
            "counts": counts,
            "total": sum(counts.values()),
            "refusals": {},
        }
        if per_surface is not None:
            row["per_surface"] = per_surface
        return row

    def write(self, *rows: dict) -> None:
        (self.store / "443.jsonl").write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )

    def test_the_split_is_summed_across_annotated_sessions(self) -> None:
        from dfinsta_pipeline.device_evidence import reading_for

        self.write(
            self.row("a", {"/feed/timeline/": 3},
                     {STARTUP: {"/feed/timeline/": 1}, "Home": {"/feed/timeline/": 2}}),
            self.row("b", {"/feed/timeline/": 5},
                     {STARTUP: {"/feed/timeline/": 2}, "Home": {"/feed/timeline/": 3}}),
        )
        reading = reading_for("/feed/timeline/", self.root)
        self.assertEqual(2, reading.attributed_sessions)
        self.assertEqual(3, reading.unsolicited)
        self.assertEqual(5, reading.solicited)

    def test_an_unannotated_session_is_not_counted_as_annotated(self) -> None:
        """The denominator has to be the sessions that could actually answer.

        A corpus where one walk marked and three did not reads as "3 of 4 arrived
        unbidden across 4 sessions" if the unannotated ones are counted — which
        understates the signal against a denominator nobody measured.
        """
        from dfinsta_pipeline.device_evidence import reading_for

        self.write(
            self.row("marked", {"/feed/timeline/": 3},
                     {STARTUP: {"/feed/timeline/": 1}, "Home": {"/feed/timeline/": 2}}),
            self.row("unmarked-1", {"/feed/timeline/": 4}),
            self.row("unmarked-2", {"/feed/timeline/": 4}),
        )
        reading = reading_for("/feed/timeline/", self.root)
        self.assertEqual(3, reading.sessions, "all three sessions still count as looking")
        self.assertEqual(1, reading.attributed_sessions)
        self.assertEqual(1, reading.unsolicited)
        self.assertEqual(2, reading.solicited)
        self.assertEqual(
            11, reading.seen,
            "and the unannotated requests are still requests — only unattributable",
        )

    def test_a_wholly_unannotated_corpus_computes_to_absence(self) -> None:
        """Through the store, which is where the `if attributed` guard lives."""
        from dfinsta_pipeline.device_evidence import reading_for

        self.write(
            self.row("a", {"/feed/timeline/": 3}),
            self.row("b", {"/feed/timeline/": 5}),
        )
        reading = reading_for("/feed/timeline/", self.root)
        self.assertEqual(0, reading.attributed_sessions)
        self.assertIsNone(reading.unsolicited)
        self.assertIsNone(reading.solicited)

    def test_the_split_joins_through_spellings_like_everything_else(self) -> None:
        """A candidate carries the index's spelling and the store the guard's.

        The split has to survive that join or it would silently read zero for
        every real candidate — five of six of which do not match by equality.
        """
        from dfinsta_pipeline.device_evidence import reading_for

        self.write(
            self.row("a", {"/feed/timeline/": 3},
                     {STARTUP: {"/feed/timeline/": 3}}),
        )
        reading = reading_for("feed/timeline/", self.root)
        self.assertEqual(3, reading.unsolicited)


class ConsentNoteTests(unittest.TestCase):
    def _reading(self, **fields) -> DeviceReading:
        return DeviceReading(
            "x", watched_in=(("443", "one-pass-v1"),), sessions=4, seen=9, **fields
        )

    def test_an_unattributed_corpus_says_it_cannot_say(self) -> None:
        note = _consent_note(self._reading())
        self.assertIn("cannot say", note)
        self.assertIn("device_session", note, "and it must name the repair")

    def test_a_launch_window_hit_is_reported_as_measured(self) -> None:
        note = _consent_note(
            self._reading(attributed_sessions=4, unsolicited=3, solicited=6)
        )
        self.assertIn("3 arrived in the launch window", note)
        self.assertIn("unsolicited by measurement", note)


class AsymmetryTests(unittest.TestCase):
    """A zero must never read as "the app only asks when asked".

    This is the claim the criterion cannot support in that direction, and it is
    the one a reader will reach for, because the table looks symmetric. If the
    caveat is ever dropped the number becomes an argument for leaving a path
    open, which no measurement here licenses.
    """

    def test_a_zero_launch_window_carries_the_caveat(self) -> None:
        note = _consent_note(DeviceReading(
            "x", watched_in=(("443", "one-pass-v1"),), sessions=4, seen=9,
            attributed_sessions=4, unsolicited=0, solicited=9,
        ))
        self.assertIn("NOT evidence", note)
        self.assertNotIn("unsolicited by measurement", note)

    def test_the_rendered_block_does_not_call_a_tap_consent(self) -> None:
        """`after a tap` and `solicited` are different claims and the render says so."""
        from dfinsta_pipeline.device_evidence import render

        page = render(DeviceReading(
            "x", watched_in=(("443", "one-pass-v1"),), corpora=(("443", "one-pass-v1"),),
            sessions=4, seen=9, attributed_sessions=4, unsolicited=3, solicited=6,
        ))
        self.assertIn("after a tap", page)
        self.assertIn("NOT the same as solicited", page)

    def test_an_unmeasured_corpus_renders_as_unmeasured(self) -> None:
        from dfinsta_pipeline.device_evidence import render

        page = render(DeviceReading(
            "x", watched_in=(("443", "one-pass-v1"),), corpora=(("443", "one-pass-v1"),),
            sessions=4, seen=9,
        ))
        self.assertIn("not measured", page)


class GateEvidenceTests(unittest.TestCase):
    def test_the_split_reaches_the_gate_inside_one_evidence_item(self) -> None:
        """One item per candidate, whatever the state.

        `Assessment` refuses anything in `measured` that is not an `Evidence`, and
        a second device item would change what the gate counts. This is the same
        requests split by when they arrived, not a separate finding.
        """
        evidence = _evidence(DeviceReading(
            "x", watched_in=(("443", "one-pass-v1"),), sessions=4, seen=9,
            attributed_sessions=4, unsolicited=3, solicited=6,
        ))
        self.assertEqual(DEVICE_REQUESTED, evidence.kind)
        self.assertEqual(3, evidence.detail["unsolicited"])
        self.assertEqual(6, evidence.detail["solicited"])
        self.assertIn("launch window", evidence.summary)

    def test_an_unattributed_candidate_reaches_the_gate_saying_so(self) -> None:
        evidence = _evidence(DeviceReading(
            "x", watched_in=(("443", "one-pass-v1"),), sessions=4, seen=9,
        ))
        self.assertIsNone(evidence.detail["unsolicited"])
        self.assertIn("cannot say", evidence.summary)


# ------------------------------------------------------------ the walk report


class State:
    """Only what `_where_seen` uses."""

    def __init__(self, sessions: dict[str, dict[str, int]]) -> None:
        self._counts = SurfaceCounts(sessions)

    def surfaces_for(self, endpoint: str):
        return self._counts.surfaces_for(endpoint)


class WhereSeenTests(unittest.TestCase):
    def test_the_launch_window_gets_a_line_of_its_own(self) -> None:
        """Inline it reads as one more tab, which is the nav-label mistake again."""
        notes = _where_seen("/x/", State({STARTUP: {"/x/": 4}, "Home": {"/x/": 2}}), ())
        self.assertEqual(2, len(notes))
        self.assertIn("4 of those arrived in the launch window", notes[1])
        self.assertIn("not the\nsame as having been asked for".replace("\n", " "), notes[1])

    def test_no_launch_window_hit_adds_no_line(self) -> None:
        notes = _where_seen("/x/", State({"Home": {"/x/": 2}}), ())
        self.assertEqual(1, len(notes))
        self.assertIn("requested on Home x2", notes[0])

    def test_an_unannotated_state_still_says_nothing_at_all(self) -> None:
        self.assertEqual((), _where_seen("/x/", State({}), ()))


if __name__ == "__main__":
    unittest.main()

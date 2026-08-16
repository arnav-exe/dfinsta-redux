"""The one signal DFInsta emits about itself, from the capture to the report.

`grouping` classified on counts alone until 2026-08-17. On 442 that made a
working block read `unaffected`: the Reels path fell from 2,3 to 0,0 under
`disable_reels` exactly as it had on 441, but 441's baseline was 7,7 and the fall
of 7 cleared a noise floor of 2 where a fall of 2 does not. The app asked for the
path a third as often, and the verdict moved because of that and not because
anything about the block changed.

The probe is what counting cannot supply. It says a patched site **ran**. It
never says a request was blocked — the site executes in every toggle state,
because the toggle is tested inside the code the probe sits beside — and a test
here that let it mean "blocked" would be worse than having no probe at all.

What it does answer is the other direction, and the report now prints both:

    * replace_reels_stream_endpoint ran in all 12 session(s), every state
    * replace_reels_discover_endpoint never ran, in any of 12 session(s) that
      looked — a movement here would not be ours

The second sentence is a finding that previously took a hand-run census over the
raw captures to notice.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dfinsta_pipeline.grouping import _execution, hooks_owning, render
from dfinsta_pipeline.observation import (
    ObservationError,
    ObservationSession,
    Probes,
    parse,
)

REPOSITORY = Path(__file__).resolve().parents[1]

HEADER = "08-08 17:31:00.000 1 1 I DFInstaObserve: !toggles +blocked disable_reels=1\n"


def capture(*lines: str) -> str:
    return HEADER + "".join(f"08-08 17:31:0{n % 10}.000 1 1 {line}\n"
                            for n, line in enumerate(lines, 1))


class ParsingTests(unittest.TestCase):
    def test_a_probe_line_is_counted_per_hook(self) -> None:
        found = parse(capture(
            "I DFInstaProbe: replace_reels_stream_endpoint",
            "I DFInstaObserve: /feed/timeline/",
            "I DFInstaProbe: replace_reels_stream_endpoint",
            "I DFInstaProbe: set_app_context",
        ))
        self.assertEqual(
            (("replace_reels_stream_endpoint", 2), ("set_app_context", 1)),
            found.probes.by_hook,
        )

    def test_a_probe_is_not_counted_as_an_observation(self) -> None:
        """Two different tags because they are two different facts: an
        observation is a thing the app did, a probe is a thing our code did."""
        found = parse(capture(
            "I DFInstaProbe: replace_reels_stream_endpoint",
            "I DFInstaObserve: /feed/timeline/",
        ))
        self.assertEqual({"/feed/timeline/": 1}, dict(found.counts))
        self.assertEqual(1, found.probes.total)

    def test_an_empty_or_padded_hook_id_is_refused(self) -> None:
        for message in ("I DFInstaProbe: ", "I DFInstaProbe:   spaced  "):
            with self.subTest(message=message):
                with self.assertRaises(ObservationError):
                    parse(capture(message))

    def test_a_line_merely_mentioning_the_tag_is_not_a_probe(self) -> None:
        found = parse(capture("I SomethingElse: saw DFInstaProbe: in a message body"))
        self.assertEqual((), found.probes.by_hook)

    def test_a_capture_that_was_read_has_probes_even_when_empty(self) -> None:
        """Empty is a measurement — nothing reported — and it is what makes the
        absent case mean "nobody looked"."""
        found = parse(capture("I DFInstaObserve: /feed/timeline/"))
        self.assertIsNotNone(found.probes)
        self.assertEqual((), found.probes.by_hook)


class RecordTests(unittest.TestCase):
    def session(self, **overrides):
        fields = dict(
            schema_version=1, version="442", build_sha256="a" * 64,
            recorded_at="2026-08-17T00:00:00Z", session_id="s", surface="feed",
            watched=("/feed/timeline/",), toggles=None, walk="one-pass-v1",
            span_seconds=2, counts={"/feed/timeline/": 1},
        )
        fields.update(overrides)
        return ObservationSession(**fields)

    def test_probes_round_trip(self) -> None:
        row = self.session(probes=Probes({"h": 3})).to_dict()
        self.assertEqual({"h": 3}, row["probes"])
        self.assertEqual(Probes({"h": 3}), ObservationSession.from_dict(row).probes)

    def test_a_row_written_before_anyone_looked_keeps_its_absence(self) -> None:
        """It must not acquire an empty object that reads as "no hook ran" — a
        measurement none of those rows took."""
        row = self.session().to_dict()
        self.assertNotIn("probes", row)
        self.assertIsNone(ObservationSession.from_dict(row).probes)

    def test_an_empty_probes_is_written_and_is_not_the_same_as_absent(self) -> None:
        row = self.session(probes=Probes()).to_dict()
        self.assertEqual({}, row["probes"])
        self.assertEqual(Probes(), ObservationSession.from_dict(row).probes)

    def test_a_null_is_refused_as_a_second_spelling_of_absent(self) -> None:
        row = self.session().to_dict()
        row["probes"] = None
        with self.assertRaises(ObservationError) as caught:
            ObservationSession.from_dict(row)
        self.assertIn("absent", str(caught.exception))

    def test_a_recorded_zero_is_refused(self) -> None:
        with self.assertRaises(ObservationError):
            Probes({"h": 0})


class OwnershipTests(unittest.TestCase):
    """Which hook a literal belongs to, read from the manifest's own statement."""

    def test_an_erasure_hook_owns_its_literal(self) -> None:
        self.assertEqual(
            ("replace_reels_stream_endpoint",),
            hooks_owning("/clips/discover/stream/", REPOSITORY),
        )

    def test_the_join_survives_the_leading_slash(self) -> None:
        """The store spells a path `/clips/…` and the manifest `clips/…`."""
        self.assertEqual(
            hooks_owning("clips/discover/stream/", REPOSITORY),
            hooks_owning("/clips/discover/stream/", REPOSITORY),
        )

    def test_a_literal_no_hook_declares_owns_nothing(self) -> None:
        self.assertEqual((), hooks_owning("/nothing/declares/this/", REPOSITORY))

    def test_an_unreadable_manifest_is_silence_not_a_crash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual((), hooks_owning("/feed/timeline/", Path(directory)))


class State:
    """The two methods `_execution` uses, and nothing else."""

    def __init__(self, label, runs):
        self.label = label
        self._runs = runs

    def executions(self, hook_id):
        return self._runs.get(hook_id)


class ExecutionLineTests(unittest.TestCase):
    """One line per hook, not one per state.

    `tigon_url_block` runs on every checked request, so a line per state would
    print six identical sentences under every endpoint in the report — which is
    how a reader learns to skip the section.
    """

    def lines(self, baseline, arms):
        return _execution("/clips/discover/stream/", baseline, arms, REPOSITORY)

    def test_ran_everywhere_is_one_line(self) -> None:
        hook = "replace_reels_stream_endpoint"
        found = self.lines(
            State("baseline", {hook: (1, 1)}),
            [State("disable_reels", {hook: (2, 1)})],
        )
        self.assertEqual(1, len(found))
        self.assertIn("all 4 session(s)", found[0])

    def test_never_ran_says_a_movement_would_not_be_ours(self) -> None:
        """The sharpest thing the probe can say, and it is about our code rather
        than about the app."""
        hook = "replace_reels_stream_endpoint"
        found = self.lines(
            State("baseline", {hook: (0, 0)}),
            [State("disable_reels", {hook: (0, 0)})],
        )
        self.assertIn("never ran", found[0])
        self.assertIn("would not be ours", found[0])

    def test_a_partial_run_names_the_states_it_missed(self) -> None:
        hook = "replace_reels_stream_endpoint"
        found = self.lines(
            State("baseline", {hook: (1, 1)}),
            [State("disable_reels", {hook: (0, 0)})],
        )
        self.assertIn("2 of 4", found[0])
        self.assertIn("disable_reels", found[0])

    def test_a_state_that_did_not_look_is_not_counted_as_a_zero(self) -> None:
        hook = "replace_reels_stream_endpoint"
        found = self.lines(
            State("baseline", {hook: (1, 1)}),
            [State("disable_reels", {})],   # `executions` returns None
        )
        self.assertIn("all 2 session(s)", found[0])

    def test_nothing_is_said_when_no_state_looked(self) -> None:
        self.assertEqual((), self.lines(State("baseline", {}), [State("arm", {})]))

    def test_a_literal_no_hook_owns_says_nothing(self) -> None:
        self.assertEqual(
            (), _execution("/nobody/owns/this/", State("baseline", {}), [], REPOSITORY)
        )


class ReportTests(unittest.TestCase):
    """It reaches the page a human rules from — through a real store.

    Driven end to end rather than by handing `render` a dict: the dict would be
    this test's idea of a report, and the thing worth checking is that a probe
    recorded in a session comes out on the page.
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
        # Every toggle spelled: a state that names none states nothing, and the
        # store refuses it — the build reports every key it reads.
        names = ("disable_adds", "disable_explore", "disable_feed",
                 "disable_reels", "disable_stories")
        rows = []
        for state, on in (("none", set()), ("reels", {"disable_reels"})):
            toggles = {name: name in on for name in names}
            for run in (1, 2):
                rows.append({
                    "schema_version": 1, "version": "999",
                    "build_sha256": "b" * 64,
                    "recorded_at": f"2026-08-17T00:0{run}:00Z",
                    "session_id": f"999-{state}-{run}", "surface": "feed_explore_reels",
                    "walk": "one-pass-v1", "span_seconds": 100,
                    "watched": ["/clips/discover/stream/", "/feed/timeline/"],
                    "toggles": dict(toggles),
                    # The arm still observes something. A session that observed
                    # nothing is deliberately not a device having looked, so an
                    # arm whose only watched path went to zero would be filtered
                    # out and never reach a classification at all.
                    "counts": (
                        {"/feed/timeline/": 5} if on
                        else {"/clips/discover/stream/": 9, "/feed/timeline/": 5}
                    ),
                    "total": 5 if on else 14,
                    # The hook ran in every session, including the ones where the
                    # path was never requested. That is the whole point.
                    "probes": {"replace_reels_stream_endpoint": 3},
                })
        (store / "999.jsonl").write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
        )

    def test_a_probe_recorded_in_a_session_comes_out_on_the_page(self) -> None:
        from dfinsta_pipeline.grouping import summary

        rendered = render(summary("999", self.root, walk="one-pass-v1"))
        self.assertIn("* replace_reels_stream_endpoint ran in all 4 session(s)", rendered)

    def test_a_classification_carries_its_execution_into_json(self) -> None:
        from dfinsta_pipeline.grouping import Classification

        row = Classification(
            endpoint="/x/", verdict="unaffected", toggle=None, reason="r",
            execution=("h ran in all 4 session(s)",),
        ).to_dict()
        self.assertEqual(["h ran in all 4 session(s)"], row["execution"])


class RedactionKeepsThemTests(unittest.TestCase):
    """The committed capture has to be able to corroborate the row.

    Probe lines were dropped until 2026-08-17, so the committed evidence could
    not check a probe count — and the reducer's own losslessness verify is what
    said so, the moment the parser learned to read them.
    """

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

    def test_a_probe_line_survives_redaction(self) -> None:
        text = capture(
            "I DFInstaProbe: replace_reels_stream_endpoint",
            "I DFInstaObserve: /feed/timeline/",
            "I SomeOtherTag: unrelated telemetry",
        )
        reduced = self.redact(text)
        self.assertIn("DFInstaProbe: replace_reels_stream_endpoint", reduced)
        self.assertNotIn("unrelated telemetry", reduced)

    def test_the_redaction_parses_to_the_same_capture(self) -> None:
        """The property the reducer promises, now including probes."""
        text = capture(
            "I DFInstaProbe: replace_reels_stream_endpoint",
            "I DFInstaObserve: /feed/timeline/",
            "I SomeOtherTag: unrelated telemetry",
        )
        self.assertEqual(parse(text), parse(self.redact(text)))

"""What the phone said about one endpoint, as the feature gate will see it.

On 2026-08-08 six candidates were ruled `block` at the gate on static evidence
alone — "the app groups this with things you block" and "no hook blocks it".
Across the 72 device sessions recorded since, five of the six are requested zero
times and one is not an endpoint at all. This module is what puts that in front of
the human next time, and these tests are mostly about the distinction it exists to
keep: **a path nobody looked for and a path looked for and never seen are
different facts**, and only one of them restricts what may be ruled.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from dfinsta_pipeline.assessment import Assessment, Evidence, Strength, spellings
from dfinsta_pipeline.device_evidence import (
    DeviceReading,
    corpora,
    evidence_for_all,
    grid,
    main,
    reading_for,
    render,
)
from dfinsta_pipeline.feature_gate import (
    DEVICE_KINDS,
    DEVICE_NEVER_REQUESTED,
    DEVICE_REQUESTED,
    DEVICE_UNWATCHED,
)

REPOSITORY = Path(__file__).resolve().parent.parent


class ThreeStatesTests(unittest.TestCase):
    """The distinction the module exists for, over hand-built readings.

    Built rather than measured, so each state is reachable on its own — the real
    corpus happens to hold only two of the three, and a test that could only see
    what the corpus contains would leave the third unexercised.
    """

    def test_a_literal_nobody_watched_is_not_a_literal_nobody_requested(self) -> None:
        unwatched = DeviceReading("x", corpora=(("439", "one-pass-v1"),))
        watched = DeviceReading(
            "x", watched_in=(("439", "one-pass-v1"),),
            corpora=(("439", "one-pass-v1"),), sessions=12,
        )
        self.assertEqual(DEVICE_UNWATCHED, unwatched.kind)
        self.assertEqual(DEVICE_NEVER_REQUESTED, watched.kind)
        self.assertNotEqual(unwatched.kind, watched.kind)
        # Both report zero requests. The count is the thing that cannot tell them
        # apart, which is why the kind is what anything downstream keys on.
        self.assertEqual(0, unwatched.seen)
        self.assertEqual(0, watched.seen)

    def test_seeing_it_once_is_enough_to_be_requested(self) -> None:
        reading = DeviceReading(
            "x", watched_in=(("439", "one-pass-v1"),), sessions=12, seen=1
        )
        self.assertEqual(DEVICE_REQUESTED, reading.kind)

    def test_every_kind_is_declared_where_the_gate_can_see_it(self) -> None:
        """The constants live in `feature_gate` so the authority never imports a
        module that reads the filesystem, and never parses a `detail` blob whose
        shape would then be load-bearing in the thing that admits decisions."""
        self.assertEqual(
            {DEVICE_UNWATCHED, DEVICE_NEVER_REQUESTED, DEVICE_REQUESTED},
            set(DEVICE_KINDS),
        )


class EvidenceStrengthTests(unittest.TestCase):
    """A zero is weak, and stays weak however many sessions produced it.

    `feed/timeline_stream/` is requested zero times on this account and blocking it
    is still right: it sits in Instagram's own list of continuous-feed paths and
    the routing that decides what an account sees is server-side. The argument is
    about what a zero can mean, not about sample size — so scale must not promote
    it. Owner decision, 2026-08-08.
    """

    def evidence(self, reading: DeviceReading) -> Evidence:
        from dfinsta_pipeline.device_evidence import _evidence

        return _evidence(reading)

    def test_a_zero_over_a_huge_corpus_is_still_weak(self) -> None:
        small = DeviceReading(
            "x", watched_in=(("439", "one-pass-v1"),), sessions=2,
        )
        huge = DeviceReading(
            "x",
            watched_in=tuple((v, w) for v in ("439", "440", "441")
                             for w in ("one-pass-v1", "three-round-v2")),
            sessions=720,
        )
        self.assertEqual(Strength.WEAK, self.evidence(small).strength)
        self.assertEqual(
            Strength.WEAK, self.evidence(huge).strength,
            "360x the sessions must not promote a zero: the caveat is about "
            "server-side routing, not about sample size",
        )

    def test_the_zero_carries_the_reason_it_is_not_a_reason_to_leave_a_path_open(self) -> None:
        summary = self.evidence(
            DeviceReading("x", watched_in=(("439", "one-pass-v1"),), sessions=12)
        ).summary
        self.assertIn("server-side", summary)
        self.assertIn("timeline_stream", summary)

    def test_being_unwatched_says_so_and_says_what_to_do(self) -> None:
        summary = self.evidence(DeviceReading("x")).summary
        self.assertIn("no device run has looked for", summary)
        self.assertIn("observe_watch", summary)
        self.assertIn("says nothing about whether the app requests it", summary)

    def test_only_a_positive_observation_is_strong(self) -> None:
        reading = DeviceReading(
            "x", watched_in=(("439", "one-pass-v1"),), sessions=12, seen=170,
            verdicts=(("439", "one-pass-v1", "blocked", "disable_explore"),),
        )
        found = self.evidence(reading)
        self.assertEqual(Strength.STRONG, found.strength)
        self.assertIn("170", found.summary)
        self.assertIn("disable_explore", found.summary)

    def test_exactly_one_evidence_per_literal_whatever_the_state(self) -> None:
        """More than one would make a candidate's device evidence a list whose
        length varied by state, and `strongest` scans for a level rather than
        counting — a second WEAK item would read as more evidence, not clearer."""
        for reading in (
            DeviceReading("x"),
            DeviceReading("x", watched_in=(("439", "one-pass-v1"),), sessions=1),
            DeviceReading("x", watched_in=(("439", "one-pass-v1"),), sessions=1, seen=3),
        ):
            with self.subTest(kind=reading.kind):
                self.assertIsInstance(self.evidence(reading), Evidence)

    def test_the_evidence_is_admissible_as_measured(self) -> None:
        """`Assessment` refuses anything in `measured` that is not an `Evidence`,
        because a `Judgement` once serialised into that array while `judgement`
        stayed null. This module must never mint a reading OF the evidence."""
        found = self.evidence(DeviceReading("x"))
        Assessment(candidate_id="gap:x", literal="x", measured=(found,))


class SpellingJoinTests(unittest.TestCase):
    """A candidate carries the index's spelling; the store carries the guard's.

    `throwIfBlocked` tests a URI path, so the watch list is written `/feed/x/`
    while a candidate literal is `feed/x/`. Of the six real candidates on record,
    exactly one joins by equality and all six join through `spellings`.
    """

    def test_the_real_candidates_do_not_join_by_equality(self) -> None:
        ruled = [json.loads(line)["record"]["candidate_id"][4:]
                 for line in (REPOSITORY / "manifest" / "rulings.jsonl")
                 .read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(6, len(ruled))
        watched = set()
        for store in (REPOSITORY / "manifest" / "observations").glob("*.jsonl"):
            for line in store.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    watched.update(json.loads(line)["watched"])
        exact = [item for item in ruled if item in watched]
        by_spelling = [item for item in ruled if set(spellings(item)) & watched]
        self.assertEqual(1, len(exact), f"expected one exact join, got {exact}")
        self.assertEqual(6, len(by_spelling), "all six must join through spellings")


class CommittedCorpusTests(unittest.TestCase):
    """Against the real store, which is what the gate will actually read."""

    def setUp(self) -> None:
        self.computed = grid(REPOSITORY)

    def test_the_grid_is_discovered_and_not_configured(self) -> None:
        """A version measured and never added to a constant would be silently
        missing from every candidate's evidence, and the gate would show less than
        the project holds without saying so."""
        found = corpora(REPOSITORY)
        self.assertEqual(
            {("439", "one-pass-v1"), ("439", "three-round-v2"),
             ("440", "one-pass-v1"), ("440", "three-round-v2"),
             ("441", "one-pass-v1"), ("441", "three-round-v2")},
            set(found),
        )

    def test_the_six_admitted_rulings_read_as_the_measurement_says(self) -> None:
        """The reproduction of the table this module was built for.

        Five of the six the gate admitted as `block` are requested zero times;
        `feed/reels_media_stream/` is real and blocked by `disable_reels` on 440.
        None of that was visible when they were ruled.
        """
        ruled = [json.loads(line)["record"]["candidate_id"][4:]
                 for line in (REPOSITORY / "manifest" / "rulings.jsonl")
                 .read_text(encoding="utf-8").splitlines() if line.strip()]
        found = evidence_for_all(ruled, REPOSITORY)
        kinds = {literal: items[0].kind for literal, items in found.items()}
        self.assertEqual(DEVICE_REQUESTED, kinds["feed/reels_media_stream/"])
        for literal in ruled:
            if literal != "feed/reels_media_stream/":
                with self.subTest(literal=literal):
                    self.assertEqual(DEVICE_NEVER_REQUESTED, kinds[literal])

    def test_a_watched_literal_reports_every_corpus_that_watched_it(self) -> None:
        reading = reading_for("feed/timeline/", REPOSITORY, computed=self.computed)
        self.assertEqual(DEVICE_REQUESTED, reading.kind)
        self.assertEqual(6, len(reading.watched_in))
        self.assertEqual(("439", "440", "441"), reading.versions)
        self.assertEqual(72, reading.sessions)
        self.assertGreater(reading.seen, 100)
        blocked = {(v, verdict, toggle) for v, _, verdict, toggle in reading.verdicts}
        self.assertEqual(
            {("439", "blocked", "disable_feed"), ("440", "blocked", "disable_feed"),
             ("441", "blocked", "disable_feed")},
            blocked,
        )

    def test_a_blank_literal_matches_nothing_where_there_is_something_to_match(self) -> None:
        """Over the **real** store, because an empty one cannot fail this.

        The first version of this test ran against a root with no corpora, where
        every literal is unwatched under every implementation — so deleting the
        guard it was written for changed nothing and the test still passed. An
        absence assertion needs somewhere the presence could have shown up.
        """
        for blank in ("", "   ", "/", "///"):
            with self.subTest(literal=repr(blank)):
                reading = reading_for(blank, REPOSITORY, computed=self.computed)
                self.assertEqual(DEVICE_UNWATCHED, reading.kind)
                self.assertEqual(0, reading.sessions, "it must join no session")
                self.assertEqual(6, len(reading.corpora),
                                 "and there were six corpora it could have matched")

    def test_the_api_v1_spelling_is_reachable_from_a_candidate_literal(self) -> None:
        """The watch list carries `/api/v1/clips/homecoming/`; a candidate carries
        `clips/homecoming/`. Slash variants alone never bridge that, so the gate
        reported "no device run has looked for it" about a path watched in 72
        sessions — and then refused to let it be blocked."""
        reading = reading_for("clips/homecoming/", REPOSITORY, computed=self.computed)
        self.assertEqual(DEVICE_NEVER_REQUESTED, reading.kind)
        self.assertEqual(72, reading.sessions)

    def test_a_vacuous_session_is_not_a_device_looking(self) -> None:
        """A session that observed nothing is equally well explained by a build
        that was not observing, a capture that was empty and an app that never
        ran. Counting one would unlock `block` on a session that measured
        nothing."""
        from dfinsta_pipeline.observation import evidential, read

        rows = read("439", REPOSITORY)
        self.assertEqual(len(rows), len(evidential(rows)),
                         "premise: the committed corpus holds no vacuous rows")

    def test_a_literal_no_corpus_watched_is_unwatched_not_unrequested(self) -> None:
        """The control for the whole distinction, on the real store."""
        reading = reading_for("media/configure_to_story/", REPOSITORY,
                              computed=self.computed)
        self.assertEqual(DEVICE_UNWATCHED, reading.kind)
        self.assertEqual((), reading.watched_in)
        self.assertEqual(6, len(reading.corpora),
                         "it still reports how much was on record to look in")

    def test_one_pass_over_the_grid_answers_every_literal(self) -> None:
        """A hundred candidates must not mean six hundred `classify` calls."""
        calls = 0
        import dfinsta_pipeline.device_evidence as module

        original = module.grid

        def counted(root=".") -> dict:
            nonlocal calls
            calls += 1
            return original(root)

        module.grid = counted
        try:
            evidence_for_all(["feed/timeline/", "feed/reels_media/", "x/y/"], REPOSITORY)
        finally:
            module.grid = original
        self.assertEqual(1, calls)


class EmptyStoreTests(unittest.TestCase):
    """A root with no observations answers, rather than raising or pretending."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_nothing_measured_reads_as_unwatched_over_zero_corpora(self) -> None:
        self.assertEqual((), corpora(self.root))
        reading = reading_for("feed/timeline/", self.root)
        self.assertEqual(DEVICE_UNWATCHED, reading.kind)
        self.assertEqual((), reading.corpora)
        # And it says so, rather than reporting a confident-looking zero.
        from dfinsta_pipeline.device_evidence import _evidence

        self.assertIn("0 corpus", _evidence(reading).summary)

    def test_a_blank_literal_is_unwatched_over_an_empty_store_too(self) -> None:
        self.assertEqual((), spellings("   "))
        self.assertEqual(DEVICE_UNWATCHED, reading_for("   ", self.root).kind)


class CommandTests(unittest.TestCase):
    def test_the_two_forms_agree_about_the_state(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(0, main(["/feed/timeline_stream/", "--root", str(REPOSITORY)]))
        page = out.getvalue()
        self.assertIn(DEVICE_NEVER_REQUESTED, page)

        machine = io.StringIO()
        with contextlib.redirect_stdout(machine):
            main(["/feed/timeline_stream/", "--root", str(REPOSITORY), "--json"])
        document = json.loads(machine.getvalue())
        # The **state**, in both forms. A machine view carrying only counts would
        # reproduce the conflation this module exists to end, and the previous
        # version of this test checked the state in the human form and the counts
        # in the machine one — so it was named for an agreement it never checked.
        self.assertEqual(DEVICE_NEVER_REQUESTED, document["kind"])
        self.assertIn(document["kind"], page)
        self.assertTrue(document["watched"])
        self.assertEqual(0, document["seen"])

    def test_the_page_names_the_state_rather_than_only_a_count(self) -> None:
        """A reader who sees `requests: 0` and nothing else cannot tell which of
        the two zeroes they are looking at."""
        page = render(DeviceReading("x", corpora=(("439", "one-pass-v1"),)))
        self.assertIn(DEVICE_UNWATCHED, page)
        self.assertIn("requests : 0", page)


class WatchCandidatesTests(unittest.TestCase):
    """The tool that makes a candidate measurable before it is ruled on.

    The gate refuses `block` and `offer_toggle` for a candidate no device looked
    for, so there has to be a low-friction path to looking. This is it, and its
    two properties are that it never removes anything and that it does not write
    unless asked.
    """

    def tool(self):
        import importlib.util

        path = REPOSITORY / "tools" / "watch_candidates.py"
        spec = importlib.util.spec_from_file_location("watch_candidates", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def manifest_with(self, watch: list[str], rules: list[str]) -> Path:
        data = {
            "schema_version": 1,
            "policy_revision": "2026-08-01",
            "hooks": [
                {
                    "hook_id": "tigon_url_block",
                    "observe_watch": watch,
                    "url_block_rules": [
                        {"literals": [{"text": item}], "toggles": ["disable_feed"]}
                        for item in rules
                    ],
                }
            ],
        }
        path = Path(self.directory.name) / "hooks.json"
        path.write_text(json.dumps(data, indent=1), encoding="utf-8")
        return path

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def test_a_literal_already_blocked_is_not_watched_twice(self) -> None:
        """`watched_literals` unions the rules with the extras, so a blocked
        literal is already watched. Adding it again would count one request as
        two."""
        module = self.tool()
        manifest = self.manifest_with([], ["/feed/timeline/"])
        self.assertEqual((), module.missing(("feed/timeline/",), manifest))

    def test_the_join_is_slash_insensitive(self) -> None:
        """The index writes `feed/x/` where the guard tests `/feed/x/`. Comparing
        the two verbatim would re-add every literal on every run."""
        module = self.tool()
        manifest = self.manifest_with(["delivery/reels_cache"], ["/feed/timeline/"])
        self.assertEqual((), module.missing(("/delivery/reels_cache/",), manifest))
        self.assertEqual(("new/thing/",), module.missing(("new/thing/",), manifest))

    def test_a_manifest_with_no_url_block_hook_is_refused(self) -> None:
        module = self.tool()
        path = Path(self.directory.name) / "empty.json"
        path.write_text(json.dumps({"hooks": []}), encoding="utf-8")
        with self.assertRaises(SystemExit) as caught:
            module.missing(("x/",), path)
        self.assertIn("tigon_url_block", str(caught.exception))

    def test_reporting_does_not_write(self) -> None:
        """`manifest/hooks.json` is what every build is rendered from, so a tool
        that edits it as a side effect of being run is one nobody can use to
        look.

        Runs against the **real** manifest and index, because a synthetic one
        cannot be loaded by `load_manifest` without carrying every field a hook
        owes — and the property under test is about writing, not about parsing.
        """
        index = REPOSITORY / "work" / "441-port" / "index"
        manifest = REPOSITORY / "manifest" / "hooks.json"
        if not index.is_dir():
            self.skipTest("no built 441 index on this machine")
        before = manifest.read_bytes()
        with contextlib.redirect_stdout(io.StringIO()):
            self.tool().main(["--index", str(index), "--manifest", str(manifest)])
        self.assertEqual(before, manifest.read_bytes(),
                         "reporting must never touch the shipped manifest")


class UnreadableIsNotAbsentTests(unittest.TestCase):
    """A store that cannot be read must not answer "nothing was measured".

    `observation.read` goes to some length — `stat`, `S_ISREG`, a symlink check —
    to keep "unreadable" from wearing the answer "empty". Two readers added on
    2026-08-14 threw that away with `if not directory.is_dir(): return ()`, and
    the digest half is the worse one: an unreadable store hashing to the same
    `""` as no store means the operation key stops moving with the corpus, for a
    reason nobody can see.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = self.root / "manifest" / "observations"
        self.store.parent.mkdir(parents=True)

    def test_a_store_that_is_a_file_is_refused_by_both_readers(self) -> None:
        from dfinsta_pipeline.assessment_record import RecordError, _observations_digest
        from dfinsta_pipeline.observation import ObservationError

        self.store.write_text("not a directory", encoding="utf-8")
        with self.assertRaises(ObservationError):
            corpora(self.root)
        with self.assertRaises(RecordError):
            _observations_digest(self.root)

    def test_an_absent_store_is_still_simply_absent(self) -> None:
        """The control: the refusal above must be about unreadable, not about
        missing — a machine that has never walked a phone answers normally."""
        from dfinsta_pipeline.assessment_record import _observations_digest

        self.assertEqual((), corpora(self.root))
        self.assertEqual("", _observations_digest(self.root))

    def test_a_stray_file_does_not_take_out_the_whole_stage(self) -> None:
        """`439.bak.jsonl`, an editor's leftover, a README — `observation.read`
        refuses a non-numeric stem, and that refusal would escape `record`
        untranslated from a module the caller never imported."""
        self.store.mkdir()
        for name in ("439.bak.jsonl", "notes.jsonl", "README.jsonl"):
            (self.store / name).write_text("{}\n", encoding="utf-8")
        self.assertEqual((), corpora(self.root))

    def test_a_stray_file_beside_a_real_corpus_leaves_it_readable(self) -> None:
        """The control, so the test above is not passing because nothing is
        readable in the first place."""
        real = REPOSITORY / "manifest" / "observations"
        if not real.is_dir():
            self.skipTest("no committed observation store")
        self.store.mkdir()
        for name in ("439.jsonl", "440.jsonl", "441.jsonl"):
            (self.store / name).write_bytes((real / name).read_bytes())
        (self.store / "439.bak.jsonl").write_text("{ not json\n", encoding="utf-8")
        found = corpora(self.root)
        self.assertEqual(6, len(found), f"expected the six real corpora, got {found}")


class VacuousSessionsDoNotCountAsLookingTests(unittest.TestCase):
    """A session that observed nothing is not a device having looked.

    `grouping`, `walks` and `states` all filter through `observation.evidential`
    first, and the reason is in `ObservationSession.vacuous`: a build that was not
    observing, a capture that was empty and an app that never ran are
    indistinguishable. Counting one as a measurement would unlock `block` on a
    session that measured nothing — which is the whole failure this restriction
    exists to prevent, arriving through the back door.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        store = self.root / "manifest" / "observations"
        store.mkdir(parents=True)
        rows = [
            self.row("439-real-1", {"/feed/timeline/": 4}),
            self.row("439-real-2", {"/feed/timeline/": 5}),
            self.row("439-empty", {}),
        ]
        (store / "439.jsonl").write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )

    def row(self, session_id: str, counts: dict) -> dict:
        return {
            "schema_version": 1,
            "version": "439",
            "build_sha256": "c" * 64,
            "recorded_at": "2026-08-14T00:00:00Z",
            "session_id": session_id,
            "surface": "feed_explore_reels",
            "watched": ["/feed/timeline/", "/hypothetical/new_surface/"],
            "toggles": {"disable_feed": False},
            "walk": "one-pass-v1",
            "counts": counts,
            "total": sum(counts.values()),
            "refusals": {},
        }

    def test_only_the_sessions_that_saw_something_are_counted(self) -> None:
        reading = reading_for("/hypothetical/new_surface/", self.root)
        self.assertEqual(DEVICE_NEVER_REQUESTED, reading.kind)
        self.assertEqual(
            2, reading.sessions,
            "the vacuous session must not be counted as a device looking",
        )

    def test_a_path_watched_only_by_a_vacuous_session_is_unwatched(self) -> None:
        """The sharp end: if the *only* session that named a literal saw nothing,
        nobody looked — and `block` must stay refused."""
        store = self.root / "manifest" / "observations"
        rows = [
            self.row("439-real-1", {"/feed/timeline/": 4}),
            self.row("439-real-2", {"/feed/timeline/": 5}),
        ]
        for row in rows:
            row["watched"] = ["/feed/timeline/"]
        vacuous = self.row("439-empty", {})
        vacuous["watched"] = ["/feed/timeline/", "/only/here/"]
        store.joinpath("439.jsonl").write_text(
            "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows + [vacuous]),
            encoding="utf-8",
        )
        self.assertEqual(DEVICE_UNWATCHED, reading_for("/only/here/", self.root).kind)

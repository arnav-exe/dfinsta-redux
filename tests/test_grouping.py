"""Which endpoints belong to which toggle, and the answers this must refuse.

Two properties are load-bearing and most of this file is about them.

**The noise floor is measured.** A test that only checked "`/feed/timeline/` came
out blocked" would pass just as happily with `floor = 2` hard-coded. So the tests
here move the *corpus* and require the verdict to move with it, and the sharpest
of them changes nothing about the path being judged: the same baseline, the same
arm, the same fall to zero, and one *unrelated state* made noisier — after which
the erasure must stop being reported. No constant can respond to that.

**A negative claim needs a positive control.** `unaffected` says "no toggle
changes this", which is exactly the shape that passes when the measurement did not
happen. So every one of its tests has a twin in which the evidence is made
unreadable and the verdict must stop being `unaffected` — and one in which the
evidence is made readable again and it must come back, because a rule that never
says `unaffected` would pass the first half on its own.

**Nothing here writes into `manifest/`.** Every root is a temporary directory
except `CommittedCorpusTests`, which only reads. A test in this repository once
wrote into a committed corpus and shipped 36 fabricated rows.
"""

from __future__ import annotations

import contextlib
import io
import json
from dataclasses import replace
import tempfile
import unittest
from pathlib import Path

from dfinsta_pipeline import grouping
from dfinsta_pipeline.grouping import (
    BLOCKED,
    ERASED,
    NEVER_REQUESTED,
    UNAFFECTED,
    UNCLASSIFIABLE,
    GroupingError,
    classify,
    main,
    noise_floors,
    partition,
    render,
    summary,
)
from dfinsta_pipeline.observation import (
    SCHEMA_VERSION,
    ObservationError,
    BlockCount,
    ObservationSession,
    ToggleState,
    read,
    store_path,
    walks,
)

REPOSITORY = Path(__file__).resolve().parent.parent

BUILD = "a" * 64
KEYS = ("disable_feed", "disable_explore", "disable_reels", "disable_stories",
        "disable_adds")
WATCHED = ("/feed/timeline/", "/feed/reels_tray/", "/clips/discover", "/never/asked/")
#: One more path, for the arms that block two things at once. Kept out of
#: `WATCHED` so the ordinary fixtures stay four wide and readable.
WIDER = (*WATCHED, "/feed/timeline_stream/")


def state(*on: str) -> ToggleState:
    """The five keys, with the named ones on. Always all five: a state that names
    different keys is a different experiment, not a subset of one."""

    return ToggleState.of({key: key in on for key in KEYS})


BASE = state()


#: The walk every fixture here claims, and the one `verdicts()` asks for. A
#: comparison names the protocol it rests on, so every test in this file is a
#: comparison within one walk unless it says otherwise.
WALK = "one-pass-three-surfaces"


def row(
    session_id: str,
    *,
    toggles: ToggleState = BASE,
    counts: dict[str, int] | None = None,
    blocks: BlockCount | None = BlockCount(0),
    watched: tuple[str, ...] = WATCHED,
    build: str = BUILD,
    at: str = "2026-08-10T19:00:00+00:00",
    version: str = "439",
    surface: str = "feed_explore_reels",
    walk: str | None = WALK,
    span_seconds: int | None = None,
) -> dict:
    return ObservationSession(
        schema_version=SCHEMA_VERSION,
        version=version,
        build_sha256=build,
        recorded_at=at,
        session_id=session_id,
        surface=surface,
        watched=watched,
        toggles=toggles,
        walk=walk,
        span_seconds=span_seconds,
        blocks=blocks,
        counts=dict(counts or {}),
    ).to_dict()


class RootedTestCase(unittest.TestCase):
    """A temporary root, and a manifest inside it. Never this repository's."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.manifest = self.root / "manifest" / "hooks.json"
        self.manifest.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "hooks": [
                        {
                            "hook_id": "tigon_url_block",
                            "url_block_rules": [
                                {
                                    "literals": [
                                        {"text": "/feed/timeline/", "match": "endswith"}
                                    ],
                                    "toggles": ["disable_feed"],
                                },
                                {
                                    "literals": [
                                        {"text": "/clips/discover", "match": "contains"}
                                    ],
                                    "toggles": ["disable_reels"],
                                },
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def write(self, *rows: dict, version: str = "439") -> Path:
        path = store_path(version, self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in rows),
            encoding="utf-8",
        )
        return path

    def verdicts(self, version: str = "439") -> dict[str, str]:
        return {
            item.endpoint: item.verdict
            for item in classify(version, self.root, walk=WALK).classifications
        }

    def verdict_of(self, endpoint: str, version: str = "439"):
        for item in classify(version, self.root, walk=WALK).classifications:
            if item.endpoint == endpoint:
                return item
        raise AssertionError(f"{endpoint} was not classified at all")


# --------------------------------------------------------- the corpus shapes


def flat_corpus(**overrides: dict[str, int]) -> tuple[dict, ...]:
    """A baseline and five arms, twice each, with nothing happening anywhere.

    Every count identical, so any verdict other than `unaffected` in a test built
    on this is caused by whatever that test changed. Arms are named by their
    toggle, and `overrides` replaces one session's counts wholesale.
    """

    steady = {"/feed/timeline/": 6, "/feed/reels_tray/": 3, "/clips/discover": 4}
    rows: list[dict] = []
    for index, on in enumerate((None, *KEYS)):
        toggles = state() if on is None else state(on)
        label = "base" if on is None else on
        for order in ("fwd", "rev"):
            key = f"{label}-{order}"
            rows.append(
                row(
                    key,
                    toggles=toggles,
                    counts=overrides.get(key, steady),
                    at=f"2026-08-10T{9 + index:02d}:{0 if order == 'fwd' else 30:02d}:00+00:00",
                )
            )
    return tuple(rows)


def with_blocks(rows: tuple[dict, ...], **totals: int) -> tuple[dict, ...]:
    """Give named sessions a block count, keeping the rest at a measured zero."""

    out = []
    for item in rows:
        item = dict(item)
        if item["session_id"] in totals:
            item["blocks"] = BlockCount.of(
                totals[item["session_id"]],
                {"FEED_NOT_LOADING": totals[item["session_id"]]}
                if totals[item["session_id"]]
                else {},
            ).as_dict()
        out.append(item)
    return tuple(out)


class NoiseFloorTests(RootedTestCase):
    """Derived from the corpus, and it has to move when the corpus does."""

    def test_the_floor_is_the_largest_difference_two_runs_of_one_state_produced(
        self,
    ) -> None:
        rows = flat_corpus(
            **{"base-rev": {"/feed/timeline/": 9, "/feed/reels_tray/": 3,
                            "/clips/discover": 4}}
        )
        self.write(*rows)
        states = partition(
            [ObservationSession.from_dict(item) for item in rows]
        )
        floors = noise_floors(states, ("/feed/timeline/", "/feed/reels_tray/"))
        self.assertEqual(3, floors["/feed/timeline/"], "6 and 9 in the baseline")
        self.assertEqual(0, floors["/feed/reels_tray/"], "identical everywhere")

    def test_movement_is_range_separation_and_not_a_difference_of_averages(self) -> None:
        """Two sessions make an average two numbers over two, which hides an overlap.

        Baseline 2, 3 against an arm 0, 1: the ranges touch at a distance of 1 and
        the means differ by 2. Ranges say "not shown twice", means say "moved".
        """

        self.assertEqual(1, grouping._gap((0, 1), (2, 3)))
        self.assertEqual(0, grouping._gap((0, 3), (2, 3)), "overlapping ranges")
        self.assertEqual(13, grouping._gap((20, 23), (6, 7)), "and it is symmetric")
        self.assertEqual(13, grouping._gap((6, 7), (20, 23)))

        near = {"/feed/timeline/": 6, "/feed/reels_tray/": 3, "/clips/discover": 2}
        far = {"/feed/timeline/": 6, "/feed/reels_tray/": 3, "/clips/discover": 3}
        # A count of zero is spelled by the key being absent; the store refuses a
        # recorded 0 as a second spelling of it.
        arm_low = {"/feed/timeline/": 6, "/feed/reels_tray/": 3}
        arm_high = {"/feed/timeline/": 6, "/feed/reels_tray/": 3, "/clips/discover": 1}
        self.write(
            *flat_corpus(
                **{
                    "base-fwd": near, "base-rev": far,
                    "disable_feed-fwd": arm_low, "disable_feed-rev": arm_high,
                    "disable_explore-fwd": far, "disable_explore-rev": far,
                    "disable_reels-fwd": far, "disable_reels-rev": far,
                    "disable_stories-fwd": far, "disable_stories-rev": far,
                    "disable_adds-fwd": far, "disable_adds-rev": far,
                }
            )
        )
        found = self.verdict_of("/clips/discover")
        self.assertEqual(1, found.noise_floor)
        self.assertEqual((0, 1), found.observed["disable_feed"])
        self.assertEqual((2, 3), found.observed["baseline"])
        self.assertNotIn(
            "no mechanism accounts",
            found.reason,
            "ranges are 1 apart against a floor of 1; the means differ by 2 and "
            "would have called this a movement nothing explains",
        )

    def test_a_path_no_state_measured_twice_has_no_floor(self) -> None:
        """`None`, not `0`. A floor of zero says every difference is real."""

        rows = tuple(item for item in flat_corpus() if item["session_id"].endswith("fwd"))
        states = partition([ObservationSession.from_dict(item) for item in rows])
        self.assertIsNone(noise_floors(states, ("/feed/timeline/",))["/feed/timeline/"])

    def test_making_an_unrelated_state_noisier_withdraws_an_erasure(self) -> None:
        """The sharpest anti-constant test: nothing about the path changes.

        Same baseline for `/clips/discover`, same arm, same fall to zero. The only
        edit is how much a *different* state — one this path's verdict never
        mentions — varied while nothing was being done to it. The floor has to
        follow, and the erasure has to be withdrawn. A `NOISE = 2` cannot notice.
        """

        low = {"/feed/timeline/": 6, "/feed/reels_tray/": 3, "/clips/discover": 4}
        gone = {"/feed/timeline/": 6, "/feed/reels_tray/": 3}
        quiet = flat_corpus(
            **{"base-fwd": low, "base-rev": low,
               "disable_reels-fwd": gone, "disable_reels-rev": gone}
        )
        self.write(*quiet)
        self.assertEqual(0, self.verdict_of("/clips/discover").noise_floor)
        self.assertEqual(ERASED, self.verdicts()["/clips/discover"])

        noisy = flat_corpus(
            **{"base-fwd": low, "base-rev": low,
               "disable_reels-fwd": gone, "disable_reels-rev": gone,
               "disable_adds-rev": {"/feed/timeline/": 6, "/feed/reels_tray/": 3,
                                    "/clips/discover": 12}}
        )
        self.write(*noisy)
        found = self.verdict_of("/clips/discover")
        self.assertEqual(8, found.noise_floor, "4 and 12 in the disable_adds state")
        self.assertNotEqual(
            ERASED, found.verdict, "a fall of 4 against a floor of 8 is not a finding"
        )

    def test_the_same_arm_reads_differently_against_a_noisier_baseline(self) -> None:
        """The anti-constant test, and the reason a hard-coded floor cannot pass.

        One arm, one movement, two corpora — and the only difference between them
        is how much the *baseline* varied when nothing was being done to it.
        """

        arm = {"/feed/timeline/": 11, "/feed/reels_tray/": 3, "/clips/discover": 4}
        quiet = flat_corpus(
            **{"disable_feed-fwd": arm, "disable_feed-rev": arm}
        )
        self.write(*with_blocks(quiet, **{"disable_feed-fwd": 11,
                                          "disable_feed-rev": 11}))
        self.assertEqual(BLOCKED, self.verdicts()["/feed/timeline/"])

        noisy = flat_corpus(
            **{
                "disable_feed-fwd": arm,
                "disable_feed-rev": arm,
                "disable_explore-rev": {"/feed/timeline/": 60, "/feed/reels_tray/": 3,
                                        "/clips/discover": 4},
            }
        )
        self.write(*with_blocks(noisy, **{"disable_feed-fwd": 11,
                                          "disable_feed-rev": 11}))
        found = self.verdict_of("/feed/timeline/")
        self.assertEqual(54, found.noise_floor, "6 and 60 in one state")
        self.assertEqual(
            BLOCKED,
            found.verdict,
            "the block signal does not depend on the floor — only the movement does",
        )
        self.assertIn(
            "did not move beyond noise",
            " ".join(found.findings[0].corroboration),
            "and the corroboration must now say the count did not move",
        )


class ErasureTests(RootedTestCase):
    def erased_corpus(self, **overrides) -> None:
        gone = {"/feed/timeline/": 6, "/feed/reels_tray/": 3}
        rows = flat_corpus(
            **{"disable_reels-fwd": gone, "disable_reels-rev": gone, **overrides}
        )
        self.write(*rows)

    def test_a_path_that_disappears_under_one_toggle_is_erased_by_it(self) -> None:
        self.erased_corpus()
        found = self.verdict_of("/clips/discover")
        self.assertEqual(ERASED, found.verdict)
        self.assertEqual("disable_reels", found.toggle)

    def test_one_of_the_two_sessions_keeping_it_is_not_an_erasure(self) -> None:
        """Replication. A finding in one running order only is an artefact."""

        self.erased_corpus(
            **{"disable_reels-rev": {"/feed/timeline/": 6, "/feed/reels_tray/": 3,
                                     "/clips/discover": 4}}
        )
        self.assertNotEqual(ERASED, self.verdicts()["/clips/discover"])

    def test_a_fall_no_bigger_than_the_measured_noise_is_not_an_erasure(self) -> None:
        """1 → 0 with a state that already swung by 1 on its own says nothing."""

        steady = {"/feed/timeline/": 6, "/feed/reels_tray/": 3, "/clips/discover": 1}
        swung = {"/feed/timeline/": 6, "/feed/reels_tray/": 3, "/clips/discover": 2}
        gone = {"/feed/timeline/": 6, "/feed/reels_tray/": 3}
        self.write(
            *flat_corpus(
                **{
                    "base-fwd": steady,
                    "base-rev": steady,
                    "disable_feed-fwd": steady,
                    "disable_feed-rev": swung,
                    "disable_reels-fwd": gone,
                    "disable_reels-rev": gone,
                    "disable_explore-fwd": steady,
                    "disable_explore-rev": steady,
                    "disable_stories-fwd": steady,
                    "disable_stories-rev": steady,
                    "disable_adds-fwd": steady,
                    "disable_adds-rev": steady,
                }
            )
        )
        found = self.verdict_of("/clips/discover")
        self.assertEqual(1, found.noise_floor)
        self.assertNotEqual(ERASED, found.verdict)

    def test_a_path_the_baseline_did_not_reliably_see_is_never_erased(self) -> None:
        """A zero under an arm is unreadable when the baseline produces zeros too."""

        gone = {"/feed/timeline/": 6, "/feed/reels_tray/": 3}
        self.write(
            *flat_corpus(
                **{
                    "base-rev": gone,
                    "disable_reels-fwd": gone,
                    "disable_reels-rev": gone,
                }
            )
        )
        found = self.verdict_of("/clips/discover")
        self.assertEqual(UNCLASSIFIABLE, found.verdict)
        self.assertIn("not reliably requested", found.reason)


class BlockAttributionTests(RootedTestCase):
    """The block-accounting identity, and every way it must decline to answer."""

    def blocking(self, *, arm_counts: dict[str, int], fwd: int, rev: int,
                 baseline: int = 0, **overrides) -> None:
        rows = flat_corpus(
            **{"disable_feed-fwd": arm_counts, "disable_feed-rev": arm_counts,
               **overrides}
        )
        self.write(
            *with_blocks(
                rows,
                **{
                    "disable_feed-fwd": fwd,
                    "disable_feed-rev": rev,
                    "base-fwd": baseline,
                    "base-rev": baseline,
                },
            )
        )

    def test_the_path_accounting_for_every_block_is_the_blocked_one(self) -> None:
        """Different totals in the two sessions, so the identity is checked twice."""

        arm = {"/feed/timeline/": 20, "/feed/reels_tray/": 3, "/clips/discover": 4}
        other = {"/feed/timeline/": 21, "/feed/reels_tray/": 3, "/clips/discover": 4}
        rows = flat_corpus(**{"disable_feed-fwd": arm, "disable_feed-rev": other})
        self.write(*with_blocks(rows, **{"disable_feed-fwd": 20,
                                         "disable_feed-rev": 21}))
        found = self.verdict_of("/feed/timeline/")
        self.assertEqual(BLOCKED, found.verdict)
        self.assertEqual("disable_feed", found.toggle)
        self.assertNotEqual(
            BLOCKED,
            self.verdicts()["/feed/reels_tray/"],
            "only one path may be named by one arm's blocks",
        )

    def test_nothing_is_attributed_to_an_arm_that_refused_nothing(self) -> None:
        """`0 == 0` must never name a path, and the guard is in `_accounts`.

        `blocks_replicate is True` and `all(arm_counts)` on the branch each imply the
        other under the attribution equality, so deleting either alone changes no
        answer and no test can pin one. That makes it worth stating where the
        protection actually lives: `_accounts` refuses a zero total outright and drops
        zero-count paths from every subset. Both of those *are* individually pinnable,
        and both are what stop "blocked by a toggle that refused nothing".
        """

        self.assertEqual((frozenset(), False), grouping._accounts({"/a": 0}, 0))
        self.assertEqual(
            (frozenset(), False),
            grouping._accounts({"/a": 0, "/b": 5}, 0),
            "a zero total attributes nothing, however many paths are on offer",
        )
        self.assertEqual(
            (frozenset({"/b"}), False),
            grouping._accounts({"/a": 0, "/b": 2}, 2),
            "and a path nobody requested is never a candidate",
        )

        gone = {"/feed/timeline/": 6, "/feed/reels_tray/": 3}
        rows = flat_corpus(**{"disable_adds-fwd": gone, "disable_adds-rev": gone})
        self.write(*rows)
        arm = next(
            item for item in classify("439", self.root, walk=WALK).arms if item.arm == "disable_adds"
        )
        self.assertEqual((0, 0), arm.block_totals)
        self.assertEqual((), grouping._attribute(arm, sorted(WATCHED)))
        self.assertEqual(NEVER_REQUESTED, self.verdicts()["/never/asked/"])

    def test_a_block_invisible_in_the_counts_is_still_attributed(self) -> None:
        """The `/feed/reels_tray/` shape: 2 → 3 is nothing, and it is still a block."""

        arm = {"/feed/timeline/": 6, "/feed/reels_tray/": 3, "/clips/discover": 4}
        self.blocking(arm_counts=arm, fwd=3, rev=3)
        found = self.verdict_of("/feed/reels_tray/")
        self.assertEqual(BLOCKED, found.verdict)
        self.assertEqual(0, grouping._gap((3, 3), (3, 3)), "the count did not move")

    def test_one_ambiguous_session_is_resolved_by_the_other(self) -> None:
        """Intersection, not union. Three paths read 3 in one session, one in both.

        Run **both ways round**. With the unambiguous session first, "look at the
        first session only" passes this and the property the name claims — *every*
        session — goes unchecked. The committed 439 corpus has that same ordering
        (`439-isolate-stories` is the unambiguous one and is first), so the fixture
        that looks like the real data is exactly the one that proves least.
        """

        ambiguous = {"/feed/timeline/": 3, "/feed/reels_tray/": 3, "/clips/discover": 3}
        clear = {"/feed/timeline/": 6, "/feed/reels_tray/": 3, "/clips/discover": 4}
        for first, second in ((clear, ambiguous), (ambiguous, clear)):
            with self.subTest(clear_first=first is clear):
                rows = flat_corpus(
                    **{"disable_feed-fwd": first, "disable_feed-rev": second}
                )
                self.write(
                    *with_blocks(rows, **{"disable_feed-fwd": 3, "disable_feed-rev": 3})
                )
                verdicts = self.verdicts()
                self.assertEqual(BLOCKED, verdicts["/feed/reels_tray/"])
                self.assertNotEqual(
                    BLOCKED,
                    verdicts["/clips/discover"],
                    "a candidate in only one session must not survive",
                )

    def test_two_paths_matching_in_both_sessions_attributes_neither(self) -> None:
        """Ambiguity in *both* sessions is not resolved by picking one."""

        # Different counts in the two sessions, so intersection, union and
        # first-only are three different answers here. Identical sessions made all
        # three agree and the test could not fail for the reason it names.
        first = {"/feed/timeline/": 3, "/feed/reels_tray/": 3, "/clips/discover": 9}
        second = {"/feed/timeline/": 3, "/feed/reels_tray/": 3, "/clips/discover": 4}
        rows = flat_corpus(**{"disable_feed-fwd": first, "disable_feed-rev": second})
        self.write(*with_blocks(rows, **{"disable_feed-fwd": 3, "disable_feed-rev": 3}))
        verdicts = self.verdicts()
        self.assertNotEqual(BLOCKED, verdicts["/feed/timeline/"])
        self.assertNotEqual(BLOCKED, verdicts["/feed/reels_tray/"])
        self.assertEqual(
            ("/feed/reels_tray/", "/feed/timeline/"),
            grouping._attribute(
                next(arm for arm in classify("439", self.root, walk=WALK).arms
                     if arm.arm == "disable_feed"),
                sorted(verdicts),
            ),
            "two candidates survive the intersection, and a pair names nobody",
        )

    def test_a_total_no_single_path_accounts_for_attributes_nothing(self) -> None:
        """Dropped events break the equality, which is the identity's own control."""

        self.blocking(
            arm_counts={"/feed/timeline/": 20, "/feed/reels_tray/": 3,
                        "/clips/discover": 4},
            fwd=17,
            rev=17,
        )
        found = self.verdict_of("/feed/timeline/")
        self.assertEqual(UNCLASSIFIABLE, found.verdict, "17 blocks, and nothing reads 17")
        self.assertEqual((), found.findings)
        self.assertIn("no mechanism accounts", found.reason)
        self.assertNotIn(BLOCKED, self.verdicts().values())

    def test_blocks_in_only_one_session_of_the_arm_attribute_nothing(self) -> None:
        """The 439 `disable_explore` shape: 1 block, then none, same state."""

        self.blocking(
            arm_counts={"/feed/timeline/": 6, "/feed/reels_tray/": 3,
                        "/clips/discover": 4},
            fwd=6,
            rev=0,
        )
        self.assertNotIn(BLOCKED, self.verdicts().values())
        found = self.verdict_of("/feed/reels_tray/")
        self.assertEqual(UNCLASSIFIABLE, found.verdict)
        self.assertIn("disable_feed", found.reason)

    def test_two_blocked_paths_summing_to_a_third_attribute_nothing(self) -> None:
        """The defect an adversarial pass found, and the reason `_accounts` exists.

        A toggle may block more than one live path — the real manifest declares
        three literals under `disable_feed` and five under `disable_reels` — and
        then the block total is their *sum*. Here `/feed/timeline/` (4) and
        `/feed/timeline_stream/` (1) are both blocked, the arm reports 5, and
        `/discover/topical_explore` happens to read 5. Before the combination
        check that fourth path was reported blocked by `disable_feed`, with no
        caveat, while the two really blocked read `unaffected`.
        """

        steady = {"/feed/timeline/": 4, "/feed/timeline_stream/": 1,
                  "/discover/topical_explore": 5, "/feed/reels_tray/": 3}
        rows = []
        for item in flat_corpus():
            item = dict(item)
            item["watched"] = [*WIDER, "/discover/topical_explore"]
            item["counts"] = dict(steady)
            item["total"] = sum(steady.values())
            rows.append(item)
        self.write(*with_blocks(tuple(rows), **{"disable_feed-fwd": 5,
                                                "disable_feed-rev": 5}))
        found = self.verdict_of("/discover/topical_explore")
        self.assertNotEqual(
            BLOCKED, found.verdict, "5 = 5 and 5 = 4 + 1; two explanations is none"
        )
        self.assertNotIn(BLOCKED, self.verdicts().values())

    def test_the_control_for_the_combination_check(self) -> None:
        """Remove the second blocked path and the same arm attributes cleanly.

        Without this, a `_accounts` that always reported a combination would pass
        the test above and delete the feature.
        """

        steady = {"/feed/timeline/": 4, "/feed/timeline_stream/": 9,
                  "/discover/topical_explore": 7, "/feed/reels_tray/": 3}
        rows = []
        for item in flat_corpus():
            item = dict(item)
            item["watched"] = [*WIDER, "/discover/topical_explore"]
            item["counts"] = dict(steady)
            item["total"] = sum(steady.values())
            rows.append(item)
        self.write(*with_blocks(tuple(rows), **{"disable_feed-fwd": 4,
                                                "disable_feed-rev": 4}))
        self.assertEqual(BLOCKED, self.verdicts()["/feed/timeline/"])

    def test_a_path_nobody_requested_never_joins_a_combination(self) -> None:
        """A zero would otherwise sum with anything without changing the total."""

        self.assertEqual(
            (frozenset({"/a"}), False),
            grouping._accounts({"/a": 3, "/b": 0, "/c": 0}, 3),
        )

    def test_a_baseline_that_blocks_attributes_nothing(self) -> None:
        """Blocks must be *new* under the arm. With every toggle off nothing throws."""

        self.blocking(
            arm_counts={"/feed/timeline/": 20, "/feed/reels_tray/": 3,
                        "/clips/discover": 4},
            fwd=20,
            rev=20,
            baseline=6,
        )
        self.assertNotIn(BLOCKED, self.verdicts().values())
        warned = [
            item
            for item in classify("439", self.root, walk=WALK).warnings
            if "with every toggle off" in item
        ]
        self.assertEqual(1, len(warned), warned)

    def test_an_uncounted_arm_attributes_nothing(self) -> None:
        rows = flat_corpus(
            **{
                "disable_feed-fwd": {"/feed/timeline/": 20, "/feed/reels_tray/": 3,
                                     "/clips/discover": 4},
                "disable_feed-rev": {"/feed/timeline/": 20, "/feed/reels_tray/": 3,
                                     "/clips/discover": 4},
            }
        )
        stripped = []
        for item in rows:
            item = dict(item)
            if item["session_id"].startswith("disable_feed"):
                item.pop("blocks")
            stripped.append(item)
        self.write(*stripped)
        found = self.verdict_of("/feed/timeline/")
        self.assertNotEqual(BLOCKED, found.verdict)
        self.assertIn(
            "disable_feed",
            found.reason,
            "and the arm that could not answer is named, not silently dropped",
        )

    def test_a_path_erased_under_the_arm_is_not_called_blocked(self) -> None:
        """Blocked means still requested. Zero requests cannot produce blocks."""

        gone = {"/feed/timeline/": 6, "/feed/reels_tray/": 3}
        rows = flat_corpus(**{"disable_reels-fwd": gone, "disable_reels-rev": gone})
        self.write(*with_blocks(rows, **{"disable_reels-fwd": 4,
                                         "disable_reels-rev": 4}))
        self.assertEqual(ERASED, self.verdicts()["/clips/discover"])


class UnaffectedTests(RootedTestCase):
    """The negative claim, and the control that keeps it from passing vacuously."""

    def test_nothing_moving_anywhere_reads_unaffected(self) -> None:
        self.write(*flat_corpus())
        self.assertEqual(
            {
                "/feed/timeline/": UNAFFECTED,
                "/feed/reels_tray/": UNAFFECTED,
                "/clips/discover": UNAFFECTED,
                "/never/asked/": NEVER_REQUESTED,
            },
            self.verdicts(),
        )

    def test_one_unreadable_arm_stops_every_path_being_called_unaffected(self) -> None:
        """`no toggle affects it` is a claim about all five, so all five must speak."""

        self.write(*with_blocks(flat_corpus(), **{"disable_explore-fwd": 2}))
        verdicts = self.verdicts()
        self.assertNotIn(UNAFFECTED, verdicts.values())
        self.assertEqual(UNCLASSIFIABLE, verdicts["/feed/timeline/"])
        self.assertIn("disable_explore", self.verdict_of("/feed/timeline/").reason)

    def test_an_arm_walked_once_stops_it_too(self) -> None:
        rows = tuple(
            item for item in flat_corpus() if item["session_id"] != "disable_adds-rev"
        )
        self.write(*rows)
        found = self.verdict_of("/feed/timeline/")
        self.assertEqual(UNCLASSIFIABLE, found.verdict)
        self.assertIn("disable_adds (walked once)", found.reason)
        self.assertNotIn(UNAFFECTED, self.verdicts().values())

    def test_movement_nothing_accounts_for_is_not_unaffected_either(self) -> None:
        surge = {"/feed/timeline/": 40, "/feed/reels_tray/": 3, "/clips/discover": 4}
        self.write(
            *flat_corpus(**{"disable_feed-fwd": surge, "disable_feed-rev": surge})
        )
        found = self.verdict_of("/feed/timeline/")
        self.assertEqual(UNCLASSIFIABLE, found.verdict)
        self.assertIn("no mechanism accounts", found.reason)
        self.assertEqual(
            UNAFFECTED,
            self.verdicts()["/feed/reels_tray/"],
            "and the paths that did not move are unharmed by it",
        )


class NeverRequestedTests(RootedTestCase):
    def test_zero_everywhere_is_recorded_not_built(self) -> None:
        self.write(*flat_corpus())
        found = self.verdict_of("/never/asked/")
        self.assertEqual(NEVER_REQUESTED, found.verdict)
        self.assertIn("recorded, not built", found.reason)

    def test_a_literal_only_some_sessions_watched_is_excluded_not_answered(self) -> None:
        """Its zero in a session that was not watching means 'not looked for'."""

        rows = []
        for item in flat_corpus():
            item = dict(item)
            if item["session_id"] == "base-rev":
                item["watched"] = [*WATCHED, "/half/watched/"]
            rows.append(item)
        self.write(*rows)
        self.assertNotIn("/half/watched/", self.verdicts())
        warned = [
            item
            for item in classify("439", self.root, walk=WALK).warnings
            if "/half/watched/" in item
        ]
        self.assertEqual(1, len(warned), warned)


class MultipleFindingTests(RootedTestCase):
    def test_two_toggles_affecting_one_path_is_refused_not_averaged(self) -> None:
        gone = {"/feed/timeline/": 6, "/feed/reels_tray/": 3}
        arm = {"/feed/timeline/": 6, "/feed/reels_tray/": 3, "/clips/discover": 4}
        rows = flat_corpus(
            **{"disable_reels-fwd": gone, "disable_reels-rev": gone,
               "disable_feed-fwd": arm, "disable_feed-rev": arm}
        )
        self.write(*with_blocks(rows, **{"disable_feed-fwd": 4, "disable_feed-rev": 4}))
        found = self.verdict_of("/clips/discover")
        self.assertEqual(UNCLASSIFIABLE, found.verdict)
        self.assertEqual(2, len(found.findings))
        self.assertIn("more than one toggle", found.reason)


class DeclaredTests(RootedTestCase):
    def test_a_contains_rule_declares_the_longer_path_it_catches(self) -> None:
        """Match semantics, not string equality — the leading-slash failure again."""

        rows = []
        for item in flat_corpus():
            item = dict(item)
            item["watched"] = [*WATCHED, "/clips/discover/stream/"]
            rows.append(item)
        self.write(*rows)
        self.assertEqual(
            ("disable_reels",), self.verdict_of("/clips/discover/stream/").declared
        )

    def test_an_unreadable_manifest_is_a_warning_and_an_unknown_not_a_refusal(
        self,
    ) -> None:
        self.manifest.write_text("{ not json", encoding="utf-8")
        self.write(*flat_corpus())
        found = self.verdict_of("/feed/timeline/")
        self.assertIsNone(found.declared, "unknown, which is not 'declared by nothing'")
        self.assertEqual(UNAFFECTED, found.verdict, "measurement still answers")
        self.assertTrue(
            any("declared under today" in item
                for item in classify("439", self.root, walk=WALK).warnings)
        )

    def test_a_path_no_rule_matches_is_declared_by_nothing_not_unknown(self) -> None:
        self.write(*flat_corpus())
        self.assertEqual((), self.verdict_of("/feed/reels_tray/").declared)


class RefusalTests(RootedTestCase):
    """Seven, each naming a different missing thing, because each has its own fix."""

    def refusal(self, *rows: dict) -> str:
        self.write(*rows)
        with self.assertRaises(GroupingError) as caught:
            classify("439", self.root, walk=WALK)
        return str(caught.exception)

    def test_no_sessions_at_all(self) -> None:
        self.assertIn("holds no session", self.refusal())

    def test_every_session_vacuous(self) -> None:
        self.assertIn(
            "vacuous",
            self.refusal(row("a", counts={}), row("b", counts={})),
        )

    def test_an_evidential_session_with_no_toggle_state(self) -> None:
        rows = [dict(item) for item in flat_corpus()]
        rows[0].pop("toggles")
        message = self.refusal(*rows)
        self.assertIn("state no toggle state", message)
        self.assertIn("base-fwd", message)

    def test_no_baseline(self) -> None:
        rows = [
            item for item in flat_corpus() if not item["session_id"].startswith("base")
        ]
        self.assertIn("every toggle off", self.refusal(*rows))

    def test_no_single_toggle_state(self) -> None:
        both = state("disable_feed", "disable_explore")
        rows = [
            item for item in flat_corpus() if item["session_id"].startswith("base")
        ] + [
            row("both-fwd", toggles=both, counts={"/feed/timeline/": 6}),
            row("both-rev", toggles=both, counts={"/feed/timeline/": 6}),
        ]
        self.assertIn("exactly one toggle on", self.refusal(*rows))

    def test_two_states_reporting_every_toggle_off_over_different_keys(self) -> None:
        """A version that grew a sixth toggle has not measured the same experiment.

        Both states are all-off, so both satisfy `is_baseline`. Taking the first by
        sort order dropped the other's sessions with no warning — and dropped them
        *before* the build check, so a second build could ride in behind it.
        """

        wider = ToggleState.of({**{key: False for key in KEYS}, "disable_shop": False})
        rows = [
            *flat_corpus(),
            row("wide-fwd", toggles=wider, counts={"/feed/timeline/": 99},
                build="c" * 64),
            row("wide-rev", toggles=wider, counts={"/feed/timeline/": 99},
                build="c" * 64),
        ]
        message = self.refusal(*rows)
        self.assertIn("report every toggle off", message)
        self.assertIn("wide-fwd", message)

    def test_more_than_one_build(self) -> None:
        rows = []
        for item in flat_corpus():
            item = dict(item)
            if item["session_id"] == "disable_feed-rev":
                item["build_sha256"] = "c" * 64
            rows.append(item)
        message = self.refusal(*rows)
        self.assertIn("2 builds", message)
        self.assertIn("a toggle name is not a rule", message.lower())

    def test_no_literal_watched_by_every_session(self) -> None:
        """An empty answer where 'nothing was measured' is the truth."""

        rows = []
        for index, item in enumerate(flat_corpus()):
            item = dict(item)
            item["watched"] = [f"/only/{index}/"]
            item["counts"] = {f"/only/{index}/": 1}
            item["total"] = 1
            rows.append(item)
        self.assertIn("no literal was watched by all", self.refusal(*rows))

    def test_a_readable_corpus_refuses_none_of_them(self) -> None:
        """The positive control. Six refusals that always fire check nothing."""

        self.write(*flat_corpus())
        self.assertEqual(4, len(classify("439", self.root, walk=WALK).classifications))


class ExcludedStateTests(RootedTestCase):
    def test_a_two_toggle_state_is_excluded_by_name_not_refused(self) -> None:
        both = state("disable_feed", "disable_explore")
        rows = [
            *flat_corpus(),
            row("both-fwd", toggles=both, counts={"/feed/timeline/": 99}),
            row("both-rev", toggles=both, counts={"/feed/timeline/": 99}),
        ]
        self.write(*rows)
        grouped = classify("439", self.root, walk=WALK)
        self.assertEqual(UNAFFECTED, self.verdicts()["/feed/timeline/"])
        warned = [item for item in grouped.warnings if "more than one toggle" in item]
        self.assertEqual(1, len(warned), grouped.warnings)
        self.assertIn("both-fwd", warned[0])

    def test_a_vacuous_session_is_excluded_and_named(self) -> None:
        rows = []
        for item in flat_corpus():
            item = dict(item)
            if item["session_id"] == "disable_adds-rev":
                item["counts"] = {}
                item["total"] = 0
            rows.append(item)
        self.write(*rows)
        grouped = classify("439", self.root, walk=WALK)
        self.assertTrue(
            any("disable_adds-rev" in item and "observed nothing" in item
                for item in grouped.warnings),
            grouped.warnings,
        )
        self.assertNotIn(
            UNAFFECTED,
            self.verdicts().values(),
            "the arm is now a single session and nothing may be called unaffected",
        )


class ReportTests(RootedTestCase):
    def test_the_two_forms_carry_exactly_the_same_warnings(self) -> None:
        """Both directions. The defect this names is the *machine* view going quiet.

        Asserting only that every JSON warning reaches the page proves the page is
        not quiet, which is the opposite claim. The one that bites is the other way:
        a line `render` prints and `summary` never put in `warnings` is invisible to
        the script that gates on the JSON, and it would sail through a one-way check.
        """

        self.write(*with_blocks(flat_corpus(), **{"disable_explore-fwd": 2}))
        report = summary("439", self.root, walk=WALK)
        page = render(report)
        self.assertTrue(report["warnings"])

        for warning in report["warnings"]:
            self.assertIn(warning, page, "a warning the JSON has and the page does not")

        lines = page.splitlines()
        start = lines.index("  WARNINGS") + 2
        printed = [line.strip() for line in lines[start:] if line.strip()]
        printed = printed[: len(printed) - 2]  # the two closing lines of the page
        self.assertEqual(
            list(report["warnings"]),
            printed,
            "a line the page warns about that the JSON does not carry",
        )

    def test_an_uncounted_arm_is_not_reported_as_one_that_disagrees(self) -> None:
        """Three ways to be unreadable, three fixes, and three different sentences.

        `grouping report --version 439` printed "does not replicate" over four arms
        whose blocks were never counted at all — one fact in the words of another,
        on the only real corpus there is, while the ARMS section of the same page
        said "never counted".
        """

        rows = []
        for item in flat_corpus():
            item = dict(item)
            if item["session_id"].startswith("disable_feed"):
                item.pop("blocks")
            rows.append(item)
        self.write(*rows)
        report = summary("439", self.root, walk=WALK)
        self.assertEqual(
            {"disable_feed": "its blocks were never counted"},
            report["unreadable_toggles"],
        )
        page = render(report)
        self.assertIn("UNREADABLE — its blocks were never counted", page)
        self.assertNotIn("does not replicate", page)

    def test_a_refusal_reaches_both_forms_and_exits_two(self) -> None:
        self.write()
        report = summary("439", self.root, walk=WALK)
        self.assertIn("holds no session", report["unanswerable_reason"])
        self.assertIn(report["unanswerable_reason"], report["warnings"])
        self.assertIn("NOTHING CAN BE DERIVED", render(report))

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--root", str(self.root), "report", "--version", "439",
                         "--walk", WALK, "--json"])
        self.assertEqual(2, code)
        self.assertIn("holds no session", json.loads(out.getvalue())["unanswerable_reason"])

    def test_an_answer_exits_zero_in_both_forms(self) -> None:
        self.write(*flat_corpus())
        for extra in ([], ["--json"]):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["--root", str(self.root), "report", "--version", "439",
                             "--walk", WALK, *extra])
            self.assertEqual(0, code)
            self.assertIn("/feed/timeline/", out.getvalue())

    def test_a_bad_version_refuses_through_one_channel(self) -> None:
        """`GroupingError`, not a leaked `ObservationError` and not a traceback."""

        with self.assertRaises(GroupingError) as caught:
            classify("banana", self.root, walk=WALK)
        self.assertIn("banana", str(caught.exception))
        self.assertNotIsInstance(caught.exception, ObservationError)

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--root", str(self.root), "report", "--version", "banana",
                         "--walk", WALK])
        self.assertEqual(2, code)
        self.assertIn("banana", err.getvalue() + out.getvalue())

    def test_summary_reads_the_store_the_caller_names(self) -> None:
        """`classify` takes `path=`; a report producer that could not would be a
        second way of saying where the evidence is."""

        elsewhere = self.root / "moved.jsonl"
        self.write(*flat_corpus())
        elsewhere.write_text(
            store_path("439", self.root).read_text(encoding="utf-8"), encoding="utf-8"
        )
        store_path("439", self.root).unlink()
        self.assertIn("holds no session", summary("439", self.root, walk=WALK)["unanswerable_reason"])
        self.assertEqual(
            "", summary("439", self.root, walk=WALK, path=elsewhere)["unanswerable_reason"]
        )

    def test_the_page_states_what_each_toggle_governs(self) -> None:
        arm = {"/feed/timeline/": 20, "/feed/reels_tray/": 3, "/clips/discover": 4}
        rows = flat_corpus(**{"disable_feed-fwd": arm, "disable_feed-rev": arm})
        self.write(*with_blocks(rows, **{"disable_feed-fwd": 20,
                                         "disable_feed-rev": 20}))
        report = summary("439", self.root, walk=WALK)
        self.assertEqual(["/feed/timeline/"], report["by_toggle"]["disable_feed"])
        self.assertEqual([], report["by_toggle"]["disable_adds"])
        self.assertIn("nothing observable", render(report))

    def test_an_unreadable_toggle_is_not_reported_as_governing_nothing(self) -> None:
        """Two different facts; the report must not say one in the words of the other."""

        self.write(*with_blocks(flat_corpus(), **{"disable_explore-fwd": 2}))
        report = summary("439", self.root, walk=WALK)
        self.assertEqual(
            {"disable_explore": "its two sessions report blocks 2, 0 and disagree"},
            report["unreadable_toggles"],
        )
        self.assertIn("UNREADABLE — its two sessions report blocks 2, 0", render(report))
        idle = [item for item in report["warnings"] if "govern nothing observable" in item]
        self.assertTrue(idle)
        self.assertNotIn("disable_explore", idle[0])


class WalkScopeTests(RootedTestCase):
    """A comparison names the protocol it rests on, or it is not a comparison.

    The defect this class is about is not hypothetical and is not subtle once it
    is written down: the noise floor is "the largest difference two runs of one
    state produced", and that sentence is only true while the two runs did the
    same thing. `test_pooling_two_walks_swallows_a_real_erasure` is the harm,
    demonstrated on a corpus where the effect is unmissable; everything else here
    is the machinery that stops it.
    """

    #: A path the arm erases outright, under both walks, so the verdict cannot
    #: come from anything but the pooling.
    ERASED_PATH = "/clips/discover"

    def two_walks(self) -> tuple[dict, ...]:
        """The same experiment run under two protocols, with its own spans.

        One pass sees `/clips/discover` four times with everything off; three
        rounds sees it twelve. `disable_reels` erases it in both. Nothing about
        the *effect* differs between the walks — only the scale of the counts,
        which is exactly what a walk changes and a toggle does not.
        """

        rows: list[dict] = []
        for walk, seen, span in (("one-pass", 4, 120), ("three-round", 12, 360)):
            for order in ("fwd", "rev"):
                rows.append(row(
                    f"{walk}-base-{order}",
                    toggles=BASE,
                    walk=walk,
                    span_seconds=span,
                    counts={"/feed/timeline/": seen, self.ERASED_PATH: seen},
                ))
                rows.append(row(
                    f"{walk}-reels-{order}",
                    toggles=state("disable_reels"),
                    walk=walk,
                    span_seconds=span,
                    counts={"/feed/timeline/": seen},
                ))
        return tuple(rows)

    def test_each_walk_answers_on_its_own(self) -> None:
        self.write(*self.two_walks())
        for walk in ("one-pass", "three-round"):
            with self.subTest(walk=walk):
                found = next(
                    item
                    for item in classify("439", self.root, walk=walk).classifications
                    if item.endpoint == self.ERASED_PATH
                )
                self.assertEqual(ERASED, found.verdict)
                self.assertEqual("disable_reels", found.toggle)

    def test_pooling_two_walks_swallows_a_real_erasure(self) -> None:
        """The harm, demonstrated rather than described.

        Every row above, relabelled to one walk and with the spans taken away so
        that nothing can contradict the label. The baseline now reads 4, 4, 12, 12
        — a spread of 8 that no toggle caused — the floor becomes 8, and a fall of
        4 to zero in every session of the arm no longer clears it. The erasure is
        reported as `unaffected`: not refused, not caveated, but positively stated
        to be nothing.
        """
        self.write(*(
            {**{k: v for k, v in item.items() if k != "span_seconds"},
             "walk": "one-pass"}
            for item in self.two_walks()
        ))
        found = next(
            item
            for item in classify("439", self.root, walk="one-pass").classifications
            if item.endpoint == self.ERASED_PATH
        )
        self.assertEqual(UNAFFECTED, found.verdict)
        self.assertEqual(8, found.noise_floor)

    def test_and_the_spans_catch_it_when_the_label_is_wrong(self) -> None:
        """The same pooled corpus with its real spans left in. `classify` refuses
        rather than deriving a floor across two protocols, because a floor that is
        wrong in this direction looks exactly like an answer."""

        self.write(*({**item, "walk": "one-pass"} for item in self.two_walks()))
        with self.assertRaises(GroupingError) as caught:
            classify("439", self.root, walk="one-pass")
        message = str(caught.exception)
        self.assertIn("do not agree that they ran it", message)
        self.assertIn("two groups", message)

    def test_sessions_on_another_walk_are_excluded_and_named(self) -> None:
        """Excluded, not dropped. They are not worse evidence, they are evidence
        about a different protocol."""

        self.write(*self.two_walks())
        grouped = classify("439", self.root, walk="one-pass")
        self.assertEqual(
            {"one-pass-base-fwd", "one-pass-base-rev", "one-pass-reels-fwd",
             "one-pass-reels-rev"},
            {item.session_id
             for state_ in (grouped.baseline, *grouped.arms)
             for item in state_.sessions},
        )
        named = [item for item in grouped.warnings if "another walk" in item]
        self.assertEqual(1, len(named), grouped.warnings)
        self.assertIn("three-round-base-fwd", named[0])
        self.assertNotIn("one-pass-base-fwd", named[0])

    def test_the_control_for_that_warning(self) -> None:
        """Absent when the store holds one walk, or it is saying nothing."""

        self.write(*flat_corpus())
        self.assertEqual(
            [], [i for i in classify("439", self.root, walk=WALK).warnings
                 if "another walk" in i]
        )

    def test_a_walk_nobody_recorded_refuses_and_lists_the_ones_that_exist(self) -> None:
        """It can only select, so naming the wrong one refuses instead of
        answering — which is what stops a required argument being the
        operator-supplies-the-answer shape."""

        self.write(*self.two_walks())
        with self.assertRaises(GroupingError) as caught:
            classify("439", self.root, walk="two-round")
        message = str(caught.exception)
        self.assertIn("'two-round'", message)
        self.assertIn("one-pass", message)
        self.assertIn("three-round", message)

    def test_an_empty_walk_is_refused_through_the_one_channel(self) -> None:
        self.write(*flat_corpus())
        for bad in ("", "   ", None, 7):
            with self.subTest(walk=bad):
                with self.assertRaises(GroupingError) as caught:
                    classify("439", self.root, walk=bad)
                self.assertIn("driving protocol", str(caught.exception))
                self.assertNotIsInstance(caught.exception, ObservationError)

    def test_sessions_naming_no_walk_refuse_and_say_how_to_repair_them(self) -> None:
        """The twenty-four committed rows' shape. Not deleted and not back-filled:
        a capture states which blocks were active and how long it ran, and never
        which script drove it, so the only repair is the person who ran them
        saying so — from the committed captures, which reproduce every count."""

        self.write(*(
            {k: v for k, v in item.items() if k != "walk"} for item in flat_corpus()
        ))
        with self.assertRaises(GroupingError) as caught:
            classify("439", self.root, walk=WALK)
        message = str(caught.exception)
        self.assertIn("name no walk", message)
        self.assertIn("base-fwd", message)
        self.assertIn("manifest/captures/", message)
        self.assertIn("--walk", message)

    def test_a_store_mixing_stated_and_unstated_rows_still_answers(self) -> None:
        """The unstated rows are excluded by the selector and named; they do not
        poison the walks that *are* stated. That is the difference from an
        unstated toggle state, which refuses the whole corpus — toggles are the
        axis this compares along, and a session belonging to no state cannot be
        placed in the experiment at all."""

        rows = [{k: v for k, v in item.items() if k != "walk"} for item in flat_corpus()]
        rows += [
            row("late-base-fwd", toggles=BASE, at="2026-08-12T09:00:00+00:00",
                counts={"/feed/timeline/": 6, "/feed/reels_tray/": 3,
                        "/clips/discover": 4}),
            row("late-base-rev", toggles=BASE, at="2026-08-12T09:30:00+00:00",
                counts={"/feed/timeline/": 6, "/feed/reels_tray/": 3,
                        "/clips/discover": 4}),
            row("late-reels-fwd", toggles=state("disable_reels"),
                at="2026-08-12T10:00:00+00:00",
                counts={"/feed/timeline/": 6, "/feed/reels_tray/": 3}),
            row("late-reels-rev", toggles=state("disable_reels"),
                at="2026-08-12T10:30:00+00:00",
                counts={"/feed/timeline/": 6, "/feed/reels_tray/": 3}),
        ]
        self.write(*rows)
        grouped = classify("439", self.root, walk=WALK)
        self.assertEqual(
            {"late-base-fwd", "late-base-rev", "late-reels-fwd", "late-reels-rev"},
            {item.session_id
             for state_ in (grouped.baseline, *grouped.arms)
             for item in state_.sessions},
        )
        named = [item for item in grouped.warnings if "name no walk" in item]
        self.assertEqual(1, len(named), grouped.warnings)
        self.assertIn("base-fwd", named[0])

    def test_the_answer_carries_the_walk_it_is_about_in_both_forms(self) -> None:
        """A page that does not say which protocol it rests on is a page whose
        first sentence is missing."""

        self.write(*flat_corpus())
        report = summary("439", self.root, walk=WALK)
        self.assertEqual(WALK, report["walk"])
        self.assertIn(f"walk {WALK}", render(report))
        bounded = [item for item in report["warnings"] if f"bounded by the walk ({WALK})" in item]
        self.assertEqual(1, len(bounded), report["warnings"])

    def test_a_refusal_still_says_which_walk_was_asked_for(self) -> None:
        self.write()
        report = summary("439", self.root, walk="three-round")
        self.assertEqual("three-round", report["walk"])
        self.assertIn("walk three-round", render(report))

    def test_the_walk_check_says_when_it_could_not_have_fired(self) -> None:
        """The positive control, carried into the report.

        The ordinary fixtures here have no capture spans at all, so nothing could
        have contradicted their walk. A page that did not say so would be claiming
        a check it never ran.
        """
        self.write(*flat_corpus())
        inert = [item for item in classify("439", self.root, walk=WALK).warnings
                 if "only partly evidenced" in item]
        self.assertEqual(1, len(inert))
        self.assertIn("could not have contradicted", inert[0])

    def test_and_it_is_silent_when_every_session_is_evidenced(self) -> None:
        """The control for the control. A warning that is always printed is not a
        warning."""

        self.write(*(
            {**item, "span_seconds": 120 + index % 3}
            for index, item in enumerate(flat_corpus())
        ))
        self.assertEqual(
            [], [item for item in classify("439", self.root, walk=WALK).warnings
                 if "only partly evidenced" in item]
        )

    def test_the_report_prints_each_state_s_spans(self) -> None:
        """Printed rather than judged: two numbers from one state are a reader's
        own check that these really were two runs of one thing."""

        self.write(*(
            {**item, "span_seconds": 120} for item in flat_corpus()
        ))
        page = render(summary("439", self.root, walk=WALK))
        self.assertIn("spans 120s, 120s", page)

    def test_an_unmeasured_span_is_not_printed_as_zero(self) -> None:
        self.write(*flat_corpus())
        self.assertIn("spans unmeasured, unmeasured",
                      render(summary("439", self.root, walk=WALK)))

    def test_partition_refuses_sessions_naming_two_walks(self) -> None:
        """The route round the whole change, closed where the information still
        exists.

        `classify` selects by walk before it calls `partition`, so this can never
        fire from there — but both `partition` and `noise_floors` are exported, and
        `noise_floors(partition(read(version, root)), endpoints)` is one line with
        no argument anybody could get wrong. A rule enforced only at a module's
        front door is a rule its own exports walk around.
        """
        rows = [ObservationSession.from_dict(item) for item in self.two_walks()]
        with self.assertRaises(GroupingError) as caught:
            partition(rows)
        message = str(caught.exception)
        self.assertIn("'one-pass'", message)
        self.assertIn("'three-round'", message)
        self.assertIn("noise floor", message)
        # An unstated walk is a third value and not a wildcard.
        with self.assertRaises(GroupingError) as caught:
            partition([rows[0], replace(rows[1], walk=None)])
        self.assertIn("no walk", str(caught.exception))

    def test_partition_still_groups_one_walk(self) -> None:
        """The control: a rule that refused every input would pass the test above
        and would take `classify` with it."""

        rows = [
            ObservationSession.from_dict(item)
            for item in self.two_walks() if item["walk"] == "one-pass"
        ]
        self.assertEqual(2, len(partition(rows)))
        self.assertEqual((), partition([]))

    def test_the_floor_that_pooling_would_have_produced(self) -> None:
        """What the export used to hand back, pinned so the size of it is on record.

        Same twelve sessions, one walk selected against all of them pooled. This is
        the number the whole change is about: nobody chose it, it looks derived,
        and it is four to sixteen times too big.
        """
        rows = [ObservationSession.from_dict(item) for item in self.two_walks()]
        one_walk = noise_floors(
            partition([item for item in rows if item.walk == "one-pass"]),
            ["/feed/timeline/", self.ERASED_PATH],
        )
        self.assertEqual({"/feed/timeline/": 0, self.ERASED_PATH: 0}, one_walk)
        pooled = noise_floors(
            partition([replace(item, walk="one-pass") for item in rows]),
            ["/feed/timeline/", self.ERASED_PATH],
        )
        self.assertEqual({"/feed/timeline/": 8, self.ERASED_PATH: 8}, pooled)

    def test_the_walk_argument_has_no_default(self) -> None:
        """Required at the call site and not merely refused at runtime.

        A default of `""` still refuses when it is reached, so every behavioural
        test passes with one added — and every existing caller silently stops
        naming the experiment it is asking about.
        """
        import inspect  # noqa: PLC0415

        for function in (classify, summary):
            with self.subTest(function=function.__name__):
                parameter = inspect.signature(function).parameters["walk"]
                self.assertIs(inspect.Parameter.empty, parameter.default)
                self.assertIs(inspect.Parameter.KEYWORD_ONLY, parameter.kind)

    def test_the_command_line_will_not_report_without_a_walk(self) -> None:
        self.write(*flat_corpus())
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            main(["--root", str(self.root), "report", "--version", "439"])
        self.assertEqual(2, caught.exception.code)
        self.assertIn("--walk", err.getvalue())


class CommittedCorpusTests(unittest.TestCase):
    """The real 439 counts, read from the committed store and never written to.

    Twelve sessions over six states, each measured twice — once with the arms run
    forward and once back-to-front — and every row re-derived from the redacted
    captures committed beside it, so this corpus is checkable rather than merely
    present.

    It was rewritten once. The rows first landed without block counts, so the
    BLOCKED half refused by name and this class pinned that refusal. Regenerating
    from the captures changed exactly one field and made the blocked half readable.
    What is pinned now is the answer itself, endpoint by endpoint, because that is
    what a human would act on.

    **And the committed rows now name no walk**, because none existed when they
    were recorded. `classify` refuses them by name — `test_the_committed_store_is
    _refused_until_somebody_says_what_was_walked` is that refusal, asserted rather
    than worked around — so the answer below is pinned over the same counts with a
    walk **supplied by this test**. That is a fixture transformation and not a
    back-fill: nothing is written into `manifest/`, and the name here is a label
    for the comparison rather than a claim about which script the owner ran. The
    real rows get a real walk when the person who walked them re-records them from
    `manifest/captures/`, which is the one thing a capture cannot supply.
    """

    #: A label for the comparison below, not a claim about the corpus. Deliberately
    #: not a plausible protocol name: reading it as the answer to "what was walked?"
    #: has to be visibly wrong.
    LABEL = "relabelled-by-this-test"

    @classmethod
    def relabelled(cls, root: Path) -> Path:
        """The committed 439 rows, in a temporary store, all naming one walk."""

        rows = read("439", REPOSITORY)
        path = store_path("439", root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(
                json.dumps(replace(item, walk=cls.LABEL).to_dict(), sort_keys=True)
                + "\n"
                for item in rows
            ),
            encoding="utf-8",
        )
        return path

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.relabelled(self.root)
        # The manifest is read for the "declared today" column only, and it is the
        # repository's real one here because that is the thing being described.
        (self.root / "manifest" / "hooks.json").write_bytes(
            (REPOSITORY / "manifest" / "hooks.json").read_bytes()
        )
        self.grouped = classify("439", self.root, walk=self.LABEL)

    def test_the_committed_store_is_refused_until_somebody_says_what_was_walked(
        self,
    ) -> None:
        """The 24 rows' fate, asserted where a reader will find it.

        Not deleted, not back-filled, and not quietly pooled into an "unstated"
        bucket that answers in full — that last one would hand back exactly the
        property naming the walk buys. They stay readable and are named, and the
        refusal says the fix.
        """

        with self.assertRaises(GroupingError) as caught:
            classify("439", REPOSITORY, walk=self.LABEL)
        message = str(caught.exception)
        self.assertIn("name no walk", message)
        self.assertIn("439-isolate-feed", message)
        self.assertIn("manifest/captures/", message)
        # And the rows themselves are untouched and still readable: refusing a
        # comparison is not the same as refusing to read.
        self.assertEqual(12, len(read("439", REPOSITORY)))
        self.assertEqual((), walks("439", REPOSITORY))

    def test_the_corpus_is_still_twelve_sessions_over_six_states(self) -> None:
        self.assertEqual(2, len(self.grouped.baseline.sessions))
        self.assertEqual(5, len(self.grouped.arms))
        self.assertEqual(
            {"disable_feed", "disable_explore", "disable_reels", "disable_stories",
             "disable_adds"},
            {arm.arm for arm in self.grouped.arms},
        )

    def test_the_reels_paths_are_erased_and_nothing_else_is(self) -> None:
        erased = {
            item.endpoint: item.toggle
            for item in self.grouped.classifications
            if item.verdict == ERASED
        }
        self.assertEqual(
            {"/clips/discover": "disable_reels",
             "/clips/discover/stream/": "disable_reels"},
            erased,
        )

    def test_ten_watched_paths_were_never_requested(self) -> None:
        never = sorted(
            item.endpoint
            for item in self.grouped.classifications
            if item.verdict == NEVER_REQUESTED
        )
        self.assertEqual(10, len(never), never)
        self.assertIn("delivery/background_prefetch", never)

    def test_two_paths_are_blocked_and_the_toggle_is_derived_not_declared(self) -> None:
        """The answer a human acts on, and nothing in reaching it reads a name.

        `/feed/timeline/` and `disable_feed` share a word; `/feed/reels_tray/` and
        `disable_stories` share none. Both are attributed the same way — by the
        block-accounting identity, arithmetic over two measurements — which is the
        whole point of grouping by measurement rather than by what things are called.
        """
        blocked = {
            item.endpoint: item.toggle
            for item in self.grouped.classifications
            if item.verdict == BLOCKED
        }
        self.assertEqual(
            {"/feed/timeline/": "disable_feed", "/feed/reels_tray/": "disable_stories"},
            blocked,
        )

    def test_the_explore_arm_is_unreadable_because_its_two_sessions_disagree(self) -> None:
        """Not "never counted" — a different fact, and the report confused the two once.

        Both sessions of `disable_explore` ran the same state and asked for
        `/discover/topical_explore` six or seven times; one reported a single block
        event and the other reported none at all. So Instagram's error event can be
        **absent for a block that certainly happened**, and the replication rule
        refuses to classify from it rather than taking the run that agreed.
        """
        self.assertEqual(
            {"disable_explore"}, set(dict(self.grouped.unreadable)),
            "only the explore arm should be unreadable now",
        )
        self.assertIn("disagree", dict(self.grouped.unreadable)["disable_explore"])
        found = next(
            item for item in self.grouped.classifications
            if item.endpoint == "/discover/topical_explore"
        )
        self.assertEqual(UNCLASSIFIABLE, found.verdict)

    def test_an_uncounted_session_still_attributes_nothing(self) -> None:
        """The guard that used to be pinned by the corpus, now pinned directly.

        The corpus no longer exercises it — every arm is counted — so it is asserted
        against a session built for the purpose. The earlier version of this test
        claimed the corpus proved it and caught none of thirty-one mutations.
        """
        arm = self.grouped.arms[0]
        uncounted = replace(arm, sessions=tuple(
            replace(s, blocks=None) for s in arm.sessions
        ))
        self.assertFalse(uncounted.counted)
        self.assertEqual(
            (), grouping._attribute(uncounted, ["/feed/timeline/"]),
            "a session with no block count must attribute nothing",
        )

    def test_the_timeline_surge_is_explained_by_the_block_that_causes_it(self) -> None:
        """20 and 23 requests against a baseline of 6 and 7, every one of them blocked.

        The surge is not incidental: a blocked request is retried, so the count
        rises *because* of the block. Pinned together so a future reading cannot
        take the surge as evidence on its own — `/feed/reels_tray/` is blocked just
        as certainly and its count barely moves.
        """
        found = next(
            item
            for item in self.grouped.classifications
            if item.endpoint == "/feed/timeline/"
        )
        self.assertEqual(BLOCKED, found.verdict)
        self.assertEqual("disable_feed", found.toggle)
        self.assertEqual((20, 23), found.observed["disable_feed"])
        self.assertEqual((6, 7), found.observed["baseline"])

    def test_reels_media_stream_is_unclassifiable_for_the_baseline_it_lacks(self) -> None:
        found = next(
            item
            for item in self.grouped.classifications
            if item.endpoint == "/feed/reels_media_stream/"
        )
        self.assertEqual(UNCLASSIFIABLE, found.verdict)
        self.assertIn("not reliably requested", found.reason)
        self.assertEqual((1, 0), found.observed["baseline"])

    def test_nothing_here_wrote_to_the_store(self) -> None:
        """The 36-fabricated-rows defence, asserted rather than intended."""

        before = (REPOSITORY / "manifest" / "observations" / "439.jsonl").read_bytes()
        with contextlib.suppress(GroupingError):
            classify("439", REPOSITORY, walk=self.LABEL)
        summary("439", REPOSITORY, walk=self.LABEL)
        # And the relabelling this class does for its own fixtures reads the real
        # store too, which is the operation most likely to grow a write by accident.
        self.relabelled(self.root)
        self.assertEqual(
            before, (REPOSITORY / "manifest" / "observations" / "439.jsonl").read_bytes()
        )


if __name__ == "__main__":
    unittest.main()

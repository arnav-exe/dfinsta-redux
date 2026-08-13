"""What the phone was asked for, and the one thing this store must never say.

The property worth defending here is a *refusal*. :func:`never_observed` answers
"which watched paths were never requested?", and the honest answer to that
question when nothing was measured is not `()` — `()` is what it says when every
watched path was seen. Two opposite states sharing one answer is the
absence-reported-as-a-pass shape this project has shipped repeatedly, most
recently as a skip for "absent" that swallowed "unreadable" and made a corrupt
corpus read as "every hook release-ready".

So most of this file is about corpora that must NOT produce a clean-looking
answer: a store with no sessions, a store whose sessions all saw nothing, a store
that is a directory, a store that is not UTF-8, and a watch list that was only
ever carried by a session which saw nothing.

The second property is the same one about the **experiment**. A zero measured
with `/feed/timeline/` blocked is a fact about our configuration, not about
Instagram, so a session states which blocks were active — as the *build*
reported them, never as the operator typed them — and sessions measured under
different states are never unioned. The corpora that must not produce a clean
answer therefore also include: a capture that cannot say what was active, a
capture that says two different things, a store whose only evidence predates
builds saying anything, and a store holding two states at once.

**Nothing here writes into `manifest/observations/`.** Every root is a temporary
directory passed explicitly. A test in this repository once wrote into a
committed corpus and shipped 36 fabricated rows, and the defence that actually
holds is that no test ever names the real root.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import os
import re
import tempfile
import unittest
from pathlib import Path

from dfinsta_pipeline import observation
from dfinsta_pipeline.observation import (
    BLOCK_MESSAGE,
    BLOCK_TAG,
    BLOCKED_DIRECTIVE,
    KNOWN_REPORTS,
    REPORTS_MARK,
    REPORTS_BLOCKED,
    Refusals,
    UNATTRIBUTED,
    UNWALKED,
    _WALK,
    BlockCount,
    OBSERVATIONS,
    SCHEMA_VERSION,
    TAG,
    TOGGLE_DIRECTIVE,
    ObservationError,
    ObservationSession,
    ToggleState,
    _record_parser,
    append,
    blocked_and_never_observed,
    blocked_endpoints,
    evidential,
    main,
    never_observed,
    parse,
    read,
    render,
    stated,
    states,
    store_path,
    summary,
    walk_dispute,
    walk_evidence,
    walks,
)

REPOSITORY = Path(__file__).resolve().parent.parent

BUILD = "b" * 64

#: The five keys `throwIfBlocked` reads, in the two states the protocol turns on:
#: the all-off exploration session, and the shipped configuration.
KEYS = ("disable_feed", "disable_explore", "disable_reels", "disable_stories",
        "disable_adds")
ALL_OFF = ToggleState.of({key: False for key in KEYS})
ALL_ON = ToggleState.of({key: True for key in KEYS})
#: One toggle on: the isolation session of step 5.
FEED_ON = ToggleState.of({key: key == "disable_feed" for key in KEYS})


def line(payload: str, *, stamp: str = "08-08 17:31:02.412 12875 12875") -> str:
    """One logcat line in the threadtime form a real capture has."""

    return f"{stamp} I {TAG}: {payload}\n"


def header(state: ToggleState = ALL_OFF) -> str:
    """The directive an observing build emits once, before any path line."""

    return line(f"{TOGGLE_DIRECTIVE} {state.text}")


#: What a capture with no block header in it counts. A *measured* zero, which is
#: the default here because it is what reading any of the fixtures below would
#: find; `blocks=None` has to be asked for, because it means nobody counted.
NO_BLOCKS = BlockCount(0)

#: The driving protocol these fixtures pretend to have run. Any name would do —
#: what the tests are about is that two sessions naming the same one are
#: comparable and two naming different ones are not.
WALK = "one-pass-three-surfaces"


#: Twelve capture spans from a scripted three-round walk, in seconds.
#:
#: **A named synthetic, and the provenance matters.** These were measured on
#: 2026-08-11 from twelve real `three-round-v2` sessions on 440 — one script, one
#: build, one sitting. That whole corpus was discarded the same day: the session
#: driver picked bottom-nav tabs by `content-desc`, `Reels` also matched a content
#: node near the top of the feed, and half the sessions tapped (379, 220) instead
#: of the tab. The fault was about *where the taps landed*, not about how long the
#: script ran, so the spans are still a faithful description of what a walk with
#: fixed sleeps produces — but nothing committed holds them any more, so they are
#: written down here rather than read.
#:
#: They are the case the whole floor exists for. Each side of the 271/273 split is
#: **zero seconds wide**, so against the derived scale alone a two-second
#: difference over a 271-second walk was infinitely sharper than the variation, and
#: `grouping report --version 440 --walk three-round-v2` returned NOTHING CAN BE
#: DERIVED over a corpus that was clean in every respect this check can see.
THREE_ROUND_JITTER = [271, 271, 271] + [273] * 9

#: The same walk with one more round in it: 271s becomes 361s, a 33% difference.
#:
#: **Wholly constructed** — nobody has walked a four-round protocol — and it is the
#: binding limit on `_MIN_SEPARATION` rather than the loosest one. Three rounds
#: against four is the smallest protocol change a person would plausibly make, so a
#: floor that cannot see it is a floor that has stopped meaning anything.
ONE_MORE_ROUND = [round(span * 4 / 3) for span in THREE_ROUND_JITTER[:6]]

#: 440's twelve one-pass spans, from the same withdrawn corpus and carried for the
#: same reason: they are the only record of **one walk measured on two builds**.
#: 439 ran 122-153s and 440 ran 109-116s on the identical protocol, which is the
#: control that stops this check reading a device or a version as a protocol. The
#: property is worth keeping even though the store is not.
ONE_PASS_ON_ANOTHER_BUILD = [109, 114] + [116] * 10



def whole_second_floor(fraction: float, at_least: int) -> tuple[int, int]:
    """The smallest span whose floor is a whole second and at least `at_least`.

    The boundary cases below need `_MIN_SEPARATION x span` to land exactly on a
    second, or "the gap is exactly the floor" cannot be written down. Searched
    rather than written, so those tests describe the *shape* of the rule at
    whatever the fraction is and only the two anchors respond to its value.
    """

    for span in range(1, 10 ** 7):
        floor = fraction * span
        if abs(floor - round(floor)) < 1e-9 and round(floor) >= at_least:
            return span, round(floor)
    raise AssertionError(f"no whole-second floor for {fraction}")


def committed_spans() -> dict[str, list[int]]:
    """Every group of committed sessions that really was one walk, by span.

    Read from `manifest/captures/`, never from the store's `span_seconds` — the
    rows written before the field carry none, and a helper that quietly returned
    fewer groups than it names would make the tests below pass by measuring less.

    Four groups, all real and all committed: 439 and 440, each walked once with
    `one-pass-v1` and once with `three-round-v2`. The 440 sessions withdrawn on
    2026-08-11 for a navigation fault were re-walked on 2026-08-12 with the nav
    found by resource id, so **the jitter anchor is a measurement again** rather
    than the synthetic that stood in for it — see `THREE_ROUND_JITTER`.

    The spread within a group is what the rule's derived term is made of, and it
    varies a lot: 31s across 439's one-pass twelve, 1s across 440's three-round
    twelve. A check that only worked on tight groups would pass the second and be
    useless on the first.
    """

    groups: dict[str, list[int]] = {}
    for version in ("439", "440"):
        for row in read(version, REPOSITORY):
            capture = REPOSITORY / "manifest" / "captures" / f"{row.session_id}.log"
            span = parse(capture.read_text(encoding="utf-8")).span_seconds
            assert span is not None, row.session_id
            groups.setdefault(f"{version} {row.walk}", []).append(span)
    return groups



def session(
    session_id: str = "s1",
    *,
    version: str = "441",
    surface: str = "feed_tab",
    watched: tuple[str, ...] = ("/feed/timeline/", "/feed/reels_tray/"),
    toggles: ToggleState | None = ALL_OFF,
    walk: str | None = WALK,
    span_seconds: int | None = None,
    blocks: BlockCount | None = NO_BLOCKS,
    refusals: "Refusals | None" = None,
    counts: dict[str, int] | None = None,
) -> ObservationSession:
    return ObservationSession(
        schema_version=SCHEMA_VERSION,
        version=version,
        build_sha256=BUILD,
        recorded_at="2026-08-09T10:00:00+00:00",
        session_id=session_id,
        surface=surface,
        watched=watched,
        toggles=toggles,
        walk=walk,
        span_seconds=span_seconds,
        blocks=blocks,
        refusals=refusals,
        counts=dict(counts or {}),
    )


class RootedTestCase(unittest.TestCase):
    """A temporary root. Never this repository's."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()

    def store(self, version: str = "441") -> Path:
        return store_path(version, self.root)

    def write(self, *rows: dict, version: str = "441") -> Path:
        path = self.store(version)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )
        return path


# ===========================================================================
#   parse
# ===========================================================================


class ParseTests(unittest.TestCase):
    def test_the_threadtime_form_from_a_real_capture_is_counted(self) -> None:
        capture = header() + (
            "08-08 17:31:02.412 12875 12875 I DFInstaObserve: /feed/timeline/\n"
            "08-08 17:31:02.900 12875 12875 I DFInstaObserve: /feed/timeline/\n"
            "08-08 17:31:03.100 12875 12875 I DFInstaObserve: /feed/reels_tray/\n"
        )
        self.assertEqual(
            {"/feed/timeline/": 2, "/feed/reels_tray/": 1}, parse(capture).counts
        )

    def test_the_bare_form_the_app_emits_is_counted(self) -> None:
        """`Log.i(TAG, literal)` is what the contract fixes; the prefix is logcat's."""
        capture = (
            f"I DFInstaObserve: {TOGGLE_DIRECTIVE} {ALL_OFF.text}\n"
            + "I DFInstaObserve: /feed/x/\n" * 2
        )
        self.assertEqual({"/feed/x/": 2}, parse(capture).counts)

    def test_lines_from_other_tags_are_ignored(self) -> None:
        capture = (
            "08-08 17:31:02.412 12875 12875 D SomeOtherTag: /feed/timeline/\n"
            "08-08 17:31:02.500 12875 12875 I DFInstaProbe: tigon_url_block\n"
            "--------- beginning of main\n"
        )
        self.assertEqual({}, parse(capture).counts)
        self.assertIsNone(parse(capture).toggles)

    def test_a_line_quoting_the_tag_inside_another_payload_is_not_a_request(self) -> None:
        """Re-narration, which `probes.count_signal` already paid for once.

        A crash dump repeating one of these lines is another component talking
        about DFInsta, not DFInsta seeing a request. Counting it manufactures
        traffic that never happened — and this rule's whole job is to distinguish
        traffic from none.
        """
        quoted = (
            "08-08 17:31:02.412 12875 12875 E IgFunctionalErrorEvent: "
            "I DFInstaObserve: /feed/timeline/\n"
        )
        self.assertEqual({}, parse(quoted).counts)

    def test_a_quoted_directive_does_not_state_the_toggle_state(self) -> None:
        """The same re-narration rule, on the line that decides the experiment.

        A capture whose configuration came out of somebody else's error payload
        would attribute every count to a state DFInsta never reported.
        """
        quoted = (
            "08-08 17:31:02.412 12875 12875 E IgFunctionalErrorEvent: "
            f"I DFInstaObserve: {TOGGLE_DIRECTIVE} {ALL_OFF.text}\n"
        )
        self.assertIsNone(parse(quoted).toggles)

    def test_the_same_literal_in_tag_position_is_counted(self) -> None:
        """The positive control for the test above.

        Without it, a regex that matched nothing at all would pass that test.
        """
        real = header() + (
            "08-08 17:31:02.412 12875 12875 I DFInstaObserve: /feed/timeline/\n"
        )
        self.assertEqual({"/feed/timeline/": 1}, parse(real).counts)
        self.assertEqual(ALL_OFF, parse(real).toggles)

    def test_an_empty_capture_counts_nothing_rather_than_refusing(self) -> None:
        """A capture with no lines is a vacuous session, decided later, not here.

        And it states no toggle state, because the observe pass never ran to say
        one. That is the *only* shape in which an unknown state may be recorded.
        """
        for text in ("", "\n\n"):
            with self.subTest(text=text):
                self.assertEqual({}, parse(text).counts)
                self.assertIsNone(parse(text).toggles)
                self.assertFalse(parse(text).stated)

    def test_a_crlf_capture_reads_the_same(self) -> None:
        """`adb` on Windows. A stray `\\r` would make the literal unmatchable."""
        capture = (
            f"08-08 17:31:02.412 1 1 I DFInstaObserve: {TOGGLE_DIRECTIVE} "
            f"{ALL_OFF.text}\r\n"
            "08-08 17:31:02.412 1 1 I DFInstaObserve: /feed/x/\r\n"
        )
        self.assertEqual({"/feed/x/": 1}, parse(capture).counts)
        # The state as well, or a `\r` would ride along on the last toggle's value
        # and make this capture's state unequal to every other capture's.
        self.assertEqual(ALL_OFF, parse(capture).toggles)

    def test_a_tag_line_with_no_literal_is_refused_by_line(self) -> None:
        """Dropping it would subtract a request that did happen from a count whose
        only purpose is being compared with zero."""
        with self.assertRaises(ObservationError) as caught:
            parse(f"I DFInstaObserve: {TOGGLE_DIRECTIVE} {ALL_OFF.text}\n"
                  "I DFInstaObserve: \n")
        self.assertIn("line 2", str(caught.exception))

    def test_a_padded_literal_is_refused_rather_than_silently_unmatchable(self) -> None:
        """It would be counted against a path no build is watching, and the
        session would then refuse for a reason two steps from the cause."""
        with self.assertRaises(ObservationError) as caught:
            parse("I DFInstaObserve:   /feed/x/  \n")
        self.assertIn("padded", str(caught.exception))

    def test_the_tag_is_the_one_the_app_side_emits(self) -> None:
        self.assertEqual("DFInstaObserve", TAG)


# ===========================================================================
#   the toggle state — read from the build, never from the operator
# ===========================================================================


class ToggleStateTests(unittest.TestCase):
    def test_the_line_the_app_emits_is_read_verbatim(self) -> None:
        """The exact shape `guards.render_observe_class` produces, pinned here.

        The app writes the keys in the order the guard reads them, which is rule
        order — so this is not sorted, and must not have to be.
        """
        state = ToggleState.parse(
            "disable_feed=1 disable_explore=0 disable_reels=1 disable_stories=1 "
            "disable_adds=0"
        )
        self.assertEqual(
            {"disable_feed": True, "disable_explore": False, "disable_reels": True,
             "disable_stories": True, "disable_adds": False},
            state.as_dict(),
        )
        self.assertEqual(("disable_feed", "disable_reels", "disable_stories"), state.on)
        self.assertEqual(("disable_adds", "disable_explore"), state.off)
        self.assertTrue(state.blocking)

    def test_the_same_state_in_another_order_is_the_same_state(self) -> None:
        """Rule order moves when a rule moves, and the app emits in rule order.

        A state that stopped comparing equal to itself across a manifest edit
        would split one experiment into two groups and answer both from half the
        sessions — silently, because both halves look like corpora.
        """
        first = ToggleState.parse("disable_feed=1 disable_adds=0")
        second = ToggleState.parse("disable_adds=0 disable_feed=1")
        self.assertEqual(first, second)
        self.assertEqual(first.text, second.text)
        self.assertEqual({first, second}, {first})

    def test_states_naming_different_keys_are_different_states(self) -> None:
        """A build that grew a sixth toggle did not run the same experiment, and
        `never_observed` must not answer one version's question from the other's
        sessions."""
        self.assertNotEqual(
            ToggleState.parse("disable_feed=0"),
            ToggleState.parse("disable_feed=0 disable_shop=0"),
        )

    def test_an_all_off_state_is_not_blocking(self) -> None:
        """The control for `blocking`: the report's circularity caution has to be
        able to be absent, or its presence says nothing."""
        self.assertFalse(ALL_OFF.blocking)
        self.assertEqual((), ALL_OFF.on)
        self.assertTrue(FEED_ON.blocking)

    def test_the_canonical_text_round_trips(self) -> None:
        for state in (ALL_OFF, ALL_ON, FEED_ON):
            with self.subTest(state=state.text):
                self.assertEqual(state, ToggleState.parse(state.text))

    def test_a_token_that_is_not_key_equals_zero_or_one_is_refused(self) -> None:
        """Read verbatim from what the build reported. A token nobody can read is
        a build and a host that disagree about the contract, and guessing which
        way is exactly the assumption this field exists to remove."""
        for token in ("disable_feed", "disable_feed=true", "disable_feed=2",
                      "disable_feed=", "=1", "disable_feed=1=0"):
            with self.subTest(token=token):
                with self.assertRaises(ObservationError):
                    ToggleState.parse(token)

    def test_a_state_naming_nothing_is_refused(self) -> None:
        with self.assertRaises(ObservationError) as caught:
            ToggleState.parse("")
        self.assertIn("names no toggle", str(caught.exception))

    def test_a_state_naming_one_key_twice_is_refused(self) -> None:
        with self.assertRaises(ObservationError) as caught:
            ToggleState.parse("disable_feed=1 disable_feed=0")
        self.assertIn("twice", str(caught.exception))

    def test_a_non_boolean_value_is_refused(self) -> None:
        """`1 == True` in Python, so an int would compare equal here and round-trip
        through JSON as `1` — one state with two spellings, in the field whose
        whole job is telling two states apart."""
        with self.assertRaises(ObservationError):
            ToggleState.of({"disable_feed": 1})
        with self.assertRaises(ObservationError):
            ToggleState.of({"disable_feed": "yes"})

    def test_a_key_that_is_not_a_preference_name_is_refused(self) -> None:
        for name in ("", "9lives", "disable feed", "disable-feed"):
            with self.subTest(name=name):
                with self.assertRaises(ObservationError):
                    ToggleState.of({name: True})


class ToggleDirectiveTests(unittest.TestCase):
    """What a capture must say about its own configuration before it says anything.

    The app restates the directive on **every checked request**, ahead of the
    path lines that request produces. It used to say it once per process behind a
    static flag, and that failed on the first real session and failed silently:
    the protocol is `adb logcat -c` immediately before walking, Instagram's
    process is usually already alive, so the one line went into the buffer that
    was then cleared and the flag stayed set — 22 path lines and no statement of
    what was active, with nothing marking the omission.
    """

    def real_session(self, state: ToggleState = FEED_ON, requests: int = 22) -> str:
        """What a walk actually looks like: the state restated before every path."""

        return "".join(header(state) + line("/feed/timeline/") for _ in range(requests))

    def test_the_directive_states_the_capture_and_is_not_itself_a_request(self) -> None:
        """It travels under the same tag, so the one thing it must not become is a
        count against a path no build was watching."""
        capture = header(FEED_ON) + line("/feed/timeline/")
        self.assertEqual(FEED_ON, parse(capture).toggles)
        self.assertEqual({"/feed/timeline/": 1}, parse(capture).counts)

    def test_the_repeated_directive_is_one_statement_and_not_twenty_two(self) -> None:
        """A 22-request session states the same thing 22 times. Collapsed, and the
        paths still counted once each — a directive counted as a request would put
        22 phantom hits against a literal nothing was watching."""
        capture = self.real_session()
        self.assertEqual(FEED_ON, parse(capture).toggles)
        self.assertEqual({"/feed/timeline/": 22}, parse(capture).counts)

    def test_any_capture_that_counts_a_path_states_its_toggle_state(self) -> None:
        """**The invariant**, over every capture shape this module has a name for.

        Not "the parser handles these cases" — the property itself: parse either
        refuses, or every count it returns came with a statement of what was
        active. The once-per-process build satisfied every individual case above
        while violating this in the field, so this is asserted as one claim over
        many shapes rather than as a list of shapes.
        """
        shapes = {
            "the real walk": self.real_session(),
            "all off": self.real_session(ALL_OFF, 3),
            "one request": header(ALL_OFF) + line("/feed/timeline/"),
            "directive only": header(ALL_OFF),
            "nothing at all": "",
            "other tags only": "08-08 17:31:02.412 1 1 D Other: /feed/timeline/\n",
            "the cleared buffer": line("/feed/timeline/") * 22,
            "cleared mid-request": line("/feed/timeline/") + self.real_session(),
            "state changed mid-walk": (
                self.real_session(ALL_OFF, 3) + self.real_session(FEED_ON, 3)
            ),
            "quoted directive": (
                "08-08 17:31:02.412 1 1 E IgFunctionalErrorEvent: "
                f"I DFInstaObserve: {TOGGLE_DIRECTIVE} {ALL_OFF.text}\n"
                + line("/feed/timeline/")
            ),
        }
        refused: list[str] = []
        counted: list[str] = []
        for label, text in shapes.items():
            with self.subTest(shape=label):
                try:
                    capture = parse(text)
                except ObservationError:
                    refused.append(label)
                    continue
                if capture.counts:
                    counted.append(label)
                    self.assertIsNotNone(
                        capture.toggles,
                        f"{label}: counts with no toggle state is the field failure "
                        "this line repeats to prevent",
                    )
        # Both branches have to be reachable, or the invariant is satisfied by a
        # parser that refuses everything — or by one that counts nothing.
        self.assertTrue(refused)
        self.assertTrue(counted)

    def test_a_path_before_any_directive_is_refused(self) -> None:
        """The cut-off capture, and the one the field failure produced.

        Every path line the build reports has a directive in front of it, so a
        path with none belongs to a request this capture did not see begin. Its
        counts belong to a configuration nobody can name — and the alternative to
        refusing is attributing them to whichever state appears later in the file.
        """
        capture = line("/feed/timeline/") + header(FEED_ON) + line("/feed/timeline/")
        with self.assertRaises(ObservationError) as caught:
            parse(capture)
        self.assertIn("before any !toggles", str(caught.exception))
        self.assertIn("line 1", str(caught.exception))

    def test_a_capture_with_paths_and_no_directive_at_all_is_refused(self) -> None:
        """Not an "all off" default. This is the whole point of the field: an
        observing build that never said what was active produced counts nobody can
        read, and "probably nothing was blocked" is the operator's guess wearing
        a measurement's clothes.

        This is the exact capture the flag version produced in the field — 22
        path lines, no statement — so the host refuses what the build could not
        say.
        """
        with self.assertRaises(ObservationError) as caught:
            parse(line("/feed/timeline/") * 22)
        self.assertIn(TOGGLE_DIRECTIVE, str(caught.exception))

    def test_two_directives_that_agree_are_one_capture(self) -> None:
        """The ordinary case now, and the control for the test below."""
        capture = header(ALL_OFF) + line("/feed/timeline/") + header(ALL_OFF) + line(
            "/feed/timeline/"
        )
        self.assertEqual(ALL_OFF, parse(capture).toggles)
        self.assertEqual({"/feed/timeline/": 2}, parse(capture).counts)

    def test_a_toggle_changed_halfway_through_a_session_is_refused(self) -> None:
        """Two experiments in one file, and no line says which one a count is in.

        Keeping the first state would attribute the second half's counts to the
        first half's configuration; keeping the last would do the reverse. This is
        detectable *only* because the directive repeats — the once-per-process
        version could not see it at all.
        """
        capture = self.real_session(ALL_OFF, 3) + self.real_session(FEED_ON, 3)
        with self.assertRaises(ObservationError) as caught:
            parse(capture)
        self.assertIn("two toggle states", str(caught.exception))
        self.assertIn(ALL_OFF.text, str(caught.exception))
        self.assertIn(FEED_ON.text, str(caught.exception))

    def test_an_unknown_directive_is_refused_rather_than_ignored(self) -> None:
        """Fails closed on a build newer than this host.

        Ignoring it reads the capture as though nothing new had been said;
        counting it manufactures a request for `!version`.
        """
        with self.assertRaises(ObservationError) as caught:
            parse(line("!version 442"))
        self.assertIn("!version", str(caught.exception))

    def test_a_directive_naming_no_toggle_is_refused_by_line(self) -> None:
        with self.assertRaises(ObservationError) as caught:
            parse(line(TOGGLE_DIRECTIVE) + line("/feed/timeline/"))
        self.assertIn("line 1", str(caught.exception))

    def test_a_malformed_directive_is_refused_by_line(self) -> None:
        with self.assertRaises(ObservationError) as caught:
            parse(line(f"{TOGGLE_DIRECTIVE} disable_feed=on"))
        self.assertIn("line 1", str(caught.exception))


# ===========================================================================
#   the record
# ===========================================================================


class SessionTests(unittest.TestCase):
    def test_total_is_derived_from_the_counts(self) -> None:
        item = session(counts={"/feed/timeline/": 9, "/feed/reels_tray/": 2})
        self.assertEqual(11, item.total)
        self.assertFalse(item.vacuous)

    def test_a_session_that_saw_nothing_is_vacuous(self) -> None:
        """The non-vacuity control, and it is `total > 0` with no constant in it."""
        self.assertTrue(session(counts={}).vacuous)
        self.assertEqual(0, session(counts={}).total)

    def test_one_observation_is_enough_to_stop_being_vacuous(self) -> None:
        """No minimum-N anywhere. A single observed request is the session's own
        positive control: it proves the build was observing and the app ran."""
        self.assertFalse(session(counts={"/feed/timeline/": 1}).vacuous)

    def test_observed_and_unobserved_split_the_watch_list(self) -> None:
        item = session(counts={"/feed/timeline/": 3})
        self.assertEqual(("/feed/timeline/",), item.observed)
        self.assertEqual(("/feed/reels_tray/",), item.unobserved)

    def test_a_round_trip_through_json_is_the_same_session(self) -> None:
        item = session(counts={"/feed/timeline/": 3})
        again = ObservationSession.from_dict(json.loads(json.dumps(item.to_dict())))
        self.assertEqual(item, again)
        self.assertEqual(ALL_OFF, again.toggles)

    def test_the_toggle_state_survives_the_store_as_a_state_and_not_as_text(self) -> None:
        """Written as an object and read back as a `ToggleState`.

        Two sessions are grouped by comparing this field, so a row that read back
        as a dict — or as the canonical string — would compare unequal to every
        session built in memory and quietly form a group of one.
        """
        item = session(toggles=FEED_ON, counts={"/feed/timeline/": 1})
        row = json.loads(json.dumps(item.to_dict()))
        self.assertEqual(
            {"disable_feed": True, "disable_explore": False, "disable_reels": False,
             "disable_stories": False, "disable_adds": False},
            row["toggles"],
        )
        self.assertEqual(FEED_ON, ObservationSession.from_dict(row).toggles)

    def test_an_unknown_toggle_state_is_spelled_by_the_key_being_absent(self) -> None:
        """And the row round-trips without acquiring one.

        `append` rewrites the whole file, so a `to_dict` that wrote `null` for an
        unknown state would edit the one committed row every time a new session
        was recorded beside it — an edit to an append-only store, performed by a
        writer nobody asked to change it.
        """
        item = session(toggles=None, counts={})
        self.assertNotIn("toggles", item.to_dict())
        self.assertIsNone(ObservationSession.from_dict(item.to_dict()).toggles)

    def test_a_null_toggle_state_is_refused_rather_than_read_as_unknown(self) -> None:
        """The recorded-zero rule, one field over: absence has one spelling.

        A row saying `null` looks like a build that answered "nothing", and this
        store must not blur an answer nobody gave with one somebody gave.
        """
        row = session(counts={"/feed/timeline/": 1}).to_dict()
        row["toggles"] = None
        with self.assertRaises(ObservationError) as caught:
            ObservationSession.from_dict(row)
        self.assertIn("second spelling of absent", str(caught.exception))

    def test_a_toggle_state_that_is_not_a_state_is_refused(self) -> None:
        """No coercion from a raw mapping. A state is normalised — sorted, with
        real booleans — and a dict slipping through would compare unequal to the
        same state read back out of the store, which is the one comparison every
        answer is grouped by."""
        with self.assertRaises(ObservationError) as caught:
            session(toggles={"disable_feed": True})
        self.assertIn("ToggleState", str(caught.exception))
        for value in ("disable_feed=1", ["disable_feed"], 1):
            with self.subTest(value=value):
                row = session(counts={"/feed/timeline/": 1}).to_dict()
                row["toggles"] = value
                with self.assertRaises(ObservationError):
                    ObservationSession.from_dict(row)

    def test_a_stated_total_that_disagrees_with_the_counts_is_refused(self) -> None:
        """The reason the derived field is written at all: a hand-edit that
        changes one count and forgets the total is caught, not believed."""
        row = session(counts={"/feed/timeline/": 3}).to_dict()
        row["total"] = 300
        with self.assertRaises(ObservationError) as caught:
            ObservationSession.from_dict(row)
        self.assertIn("disagrees with itself", str(caught.exception))

    def test_a_row_with_no_total_at_all_still_reads(self) -> None:
        """The positive control for the check above: it must not reject a row
        that simply omits the derived field."""
        row = session(counts={"/feed/timeline/": 3}).to_dict()
        del row["total"]
        self.assertEqual(3, ObservationSession.from_dict(row).total)

    def test_a_count_for_a_path_the_session_was_not_watching_is_refused(self) -> None:
        """The build and the watch list disagree about what was being watched.

        Nothing that session did not see can be relied on after that, and this is
        the only thing standing between a mis-generated observe payload and a
        confident statement about a path it was never looking for.
        """
        with self.assertRaises(ObservationError) as caught:
            session(counts={"/somewhere/else/": 1})
        self.assertIn("not watching", str(caught.exception))

    def test_a_recorded_zero_is_refused(self) -> None:
        """`parse` never produces one, so a zero is a second spelling of absent —
        and blurring those two is precisely what this store must not do."""
        for value in (0, -1):
            with self.subTest(value=value):
                with self.assertRaises(ObservationError):
                    session(counts={"/feed/timeline/": value})

    def test_a_boolean_count_is_refused(self) -> None:
        """`True` is an `int` in Python and would sum to 1 request that no line
        in any capture attests to."""
        with self.assertRaises(ObservationError):
            session(counts={"/feed/timeline/": True})

    def test_an_empty_watch_list_is_refused(self) -> None:
        with self.assertRaises(ObservationError) as caught:
            session(watched=())
        self.assertIn("watched nothing", str(caught.exception))

    def test_a_blank_or_repeated_watch_entry_is_refused(self) -> None:
        with self.assertRaises(ObservationError):
            session(watched=("/feed/x/", "  "))
        with self.assertRaises(ObservationError) as caught:
            session(watched=("/feed/x/", "/feed/x/"))
        self.assertIn("more than once", str(caught.exception))

    def test_every_field_that_makes_the_row_joinable_is_required(self) -> None:
        """A measurement naming no build, no time and no surface is a number
        nobody can join to anything — which was found here once already."""
        with self.assertRaises(ObservationError) as caught:
            session().__class__(
                schema_version=1, version="441", build_sha256="short",
                recorded_at="x", session_id="s", surface="f",
                watched=("/a/",), toggles=ALL_OFF, walk=WALK, counts={},
            )
        self.assertIn("SHA-256", str(caught.exception))
        for field in ("recorded_at", "session_id", "surface"):
            with self.subTest(field=field):
                arguments = {
                    "schema_version": 1, "version": "441", "build_sha256": BUILD,
                    "recorded_at": "2026-08-09T10:00:00Z", "session_id": "s",
                    "surface": "f", "watched": ("/a/",), "toggles": ALL_OFF,
                    "walk": WALK, "counts": {},
                }
                arguments[field] = "  "
                with self.assertRaises(ObservationError) as caught:
                    ObservationSession(**arguments)
                self.assertIn(field, str(caught.exception))

    def test_an_unparseable_timestamp_is_refused(self) -> None:
        """Parsed, not checked for emptiness.

        A sibling record store checked every other field that way, and
        `--recorded-at banana` exited 0 into an append-only file nothing ever
        deletes from. This store has the same contract.
        """
        for stamp in ("banana", "2026-08-09", "09/08/2026 10:00", "2026-13-01T00:00:00Z"):
            with self.subTest(stamp=stamp):
                with self.assertRaises(ObservationError) as caught:
                    ObservationSession(
                        schema_version=1, version="441", build_sha256=BUILD,
                        recorded_at=stamp, session_id="s", surface="f",
                        watched=("/a/",), toggles=ALL_OFF, walk=WALK, counts={},
                    )
                self.assertIn(stamp, str(caught.exception))

    def test_a_naive_timestamp_is_refused(self) -> None:
        """It cannot be ordered against one written on another machine, and two
        sessions being orderable is what makes them comparable."""
        with self.assertRaises(ObservationError) as caught:
            ObservationSession(
                schema_version=1, version="441", build_sha256=BUILD,
                recorded_at="2026-08-09T10:00:00", session_id="s", surface="f",
                watched=("/a/",), toggles=ALL_OFF, walk=WALK, counts={},
            )
        self.assertIn("no UTC offset", str(caught.exception))

    def test_both_offset_spellings_are_accepted_and_neither_is_rewritten(self) -> None:
        """The positive control, and the record keeps what a human typed."""
        for stamp in ("2026-08-09T10:00:00Z", "2026-08-09T10:00:00+00:00",
                      "2026-08-09T12:00:00+02:00"):
            with self.subTest(stamp=stamp):
                item = ObservationSession(
                    schema_version=1, version="441", build_sha256=BUILD,
                    recorded_at=stamp, session_id="s", surface="f",
                    watched=("/a/",), toggles=ALL_OFF, walk=WALK, counts={},
                )
                self.assertEqual(stamp, item.recorded_at)

    def test_a_padded_timestamp_is_stored_stripped(self) -> None:
        """Validating the stripped value and recording the padded one wrote a form
        the next read refuses — the same defect, one module over."""
        item = ObservationSession(
            schema_version=1, version="441", build_sha256=BUILD,
            recorded_at="  2026-08-09T10:00:00+00:00  ", session_id="s", surface="f",
            watched=("/a/",), toggles=ALL_OFF, walk=WALK, counts={},
        )
        self.assertEqual("2026-08-09T10:00:00+00:00", item.recorded_at)
        self.assertEqual("2026-08-09T10:00:00+00:00", item.to_dict()["recorded_at"])

    def test_the_counts_are_copied_so_the_checks_cannot_be_undone_afterwards(self) -> None:
        """Every count rule above runs once, at construction, on a mapping the
        caller can still be holding.

        Left uncopied, a count added after the fact produced a row `append`
        wrote and this module's own `read` then refused — a store its writer made
        and its reader rejects. `watched` was copied and `counts` was not.
        """
        live = {"/feed/timeline/": 1}
        # Constructed directly: the `session` helper here passes `dict(counts)`,
        # so writing this through it would prove the helper copies and nothing
        # about the record type — the shape of test this project keeps shipping.
        item = ObservationSession(
            schema_version=SCHEMA_VERSION, version="441", build_sha256=BUILD,
            recorded_at="2026-08-09T10:00:00Z", session_id="s", surface="f",
            watched=("/feed/timeline/",), toggles=ALL_OFF, walk=WALK, counts=live,
        )
        live["/never/watched/"] = 3
        self.assertEqual({"/feed/timeline/": 1}, dict(item.counts))
        self.assertEqual(1, item.total)
        self.assertNotIn("/never/watched/", item.to_dict()["counts"])

    def test_a_watch_list_given_as_a_list_is_stored_as_a_tuple(self) -> None:
        """Two sessions with the same watch list must compare equal however the
        caller spelled it, or a duplicate check silently stops matching."""
        item = ObservationSession(
            schema_version=1, version="441", build_sha256=BUILD,
            recorded_at="2026-08-09T10:00:00Z", session_id="s", surface="f",
            watched=["/a/", "/b/"], toggles=ALL_OFF, walk=WALK, counts={},
        )
        self.assertEqual(("/a/", "/b/"), item.watched)

    def test_a_non_numeric_version_is_refused(self) -> None:
        with self.assertRaises(ObservationError):
            session(version="v441")

    def test_an_unsupported_schema_is_refused(self) -> None:
        row = session().to_dict()
        row["schema_version"] = 2
        with self.assertRaises(ObservationError):
            ObservationSession.from_dict(row)

    def test_unknown_keys_are_refused_rather_than_ignored(self) -> None:
        row = session().to_dict()
        row["confidence"] = 0.9
        with self.assertRaises(ObservationError) as caught:
            ObservationSession.from_dict(row)
        self.assertIn("confidence", str(caught.exception))

    def test_wrongly_shaped_fields_are_refused_by_name(self) -> None:
        for key, value in (
            ("watched", "/feed/x/"),
            ("watched", 3),
            ("counts", ["/feed/x/"]),
        ):
            with self.subTest(key=key, value=value):
                row = session().to_dict()
                row[key] = value
                del row["total"]
                with self.assertRaises(ObservationError) as caught:
                    ObservationSession.from_dict(row)
                self.assertIn(key, str(caught.exception))
        with self.assertRaises(ObservationError):
            ObservationSession.from_dict(["not", "an", "object"])


# ===========================================================================
#   the store
# ===========================================================================


class StoreTests(RootedTestCase):
    def test_a_session_appended_reads_back(self) -> None:
        item = session(counts={"/feed/timeline/": 4})
        written = append(item, root=self.root)
        self.assertEqual(self.store(), written)
        self.assertEqual((item,), read("441", self.root))

    def test_appending_keeps_every_earlier_session_in_order(self) -> None:
        """Append-only. The whole-file rewrite must not become a rewrite."""
        first = session("s1", counts={"/feed/timeline/": 1})
        second = session("s2", surface="explore_tab", counts={"/feed/reels_tray/": 2})
        append(first, root=self.root)
        append(second, root=self.root)
        self.assertEqual([f.session_id for f in read("441", self.root)], ["s1", "s2"])
        self.assertEqual((first, second), read("441", self.root))

    def test_a_second_session_under_one_id_is_refused(self) -> None:
        """Two rows under one id is the state where nobody can say which capture
        the counts came from."""
        append(session("s1", counts={"/feed/timeline/": 1}), root=self.root)
        with self.assertRaises(ObservationError) as caught:
            append(session("s1", counts={"/feed/timeline/": 99}), root=self.root)
        self.assertIn("already recorded", str(caught.exception))
        self.assertEqual(1, read("441", self.root)[0].counts["/feed/timeline/"])

    def test_appending_to_a_malformed_store_refuses_instead_of_overwriting(self) -> None:
        """The store is read before it is written, so a writer cannot clobber a
        file it never looked at — which a whole-file rewrite otherwise would."""
        path = self.store()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json\n", encoding="utf-8")
        with self.assertRaises(ObservationError):
            append(session("s1", counts={"/feed/timeline/": 1}), root=self.root)
        self.assertEqual("{ not json\n", path.read_text(encoding="utf-8"))

    def test_a_failed_append_leaves_no_scratch_file_behind(self) -> None:
        append(session("s1", counts={"/feed/timeline/": 1}), root=self.root)
        with self.assertRaises(ObservationError):
            append(session("s1"), root=self.root)
        self.assertEqual(
            ["441.jsonl"], sorted(p.name for p in self.store().parent.iterdir())
        )

    def test_a_session_that_saw_something_and_states_no_state_is_not_written(self) -> None:
        """The writer's rule, and the constructor deliberately does not hold it.

        `manifest/observations/441.jsonl` is already in exactly this shape, so the
        record type has to be able to represent it or the committed row could not
        be read back. What must not happen again is *making* one: every count in
        such a row is unreadable, because a zero under a block we set is caused by
        us and nothing says whether one was set.
        """
        for count in (1, 4, 52):
            # Every non-vacuous size, because the rule is `total > 0` and no
            # constant. `total > 1` reads as maintenance, passes a suite that only
            # ever tries 4, and admits the one-request session that says nothing
            # about its configuration — `derive-the-threshold-never-declare-it`.
            with self.subTest(count=count):
                unstated = session("s1", toggles=None, counts={"/feed/timeline/": count})
                with self.assertRaises(ObservationError) as caught:
                    append(unstated, root=self.root)
                self.assertIn("states no toggle state", str(caught.exception))
                self.assertEqual((), read("441", self.root))

    def test_a_vacuous_session_that_states_no_state_is_written(self) -> None:
        """The control, and the honest record of a capture that saw nothing.

        A capture with no observe lines carries no directive either, because the
        pass that emits it never ran. Refusing the row as well would leave no way
        to record "this session measured nothing", which is the fact the operator
        most needs to keep.
        """
        append(session("s1", toggles=None, counts={}), root=self.root)
        self.assertEqual(1, len(read("441", self.root)))
        self.assertIsNone(read("441", self.root)[0].toggles)

    def test_a_missing_store_is_no_sessions(self) -> None:
        self.assertEqual((), read("441", self.root))

    def test_a_store_that_is_a_directory_is_refused_not_read_as_empty(self) -> None:
        """`is_file()` answers False for a directory as well as for an absent
        path. Collapsing those is `a-skip-for-absent-swallowed-unreadable`, in the
        one function whose empty answer means "nothing is wrong"."""
        self.store().parent.mkdir(parents=True, exist_ok=True)
        self.store().mkdir()
        with self.assertRaises(ObservationError) as caught:
            read("441", self.root)
        self.assertIn("not a regular file", str(caught.exception))

    def test_a_store_that_is_a_dangling_symlink_is_refused(self) -> None:
        """`exists()` answers False for it too — the third shape of unreadable
        wearing the answer "there is nothing here". Somebody meant a store to be
        there, which is not the same fact as no store at all."""
        self.store().parent.mkdir(parents=True, exist_ok=True)
        self.store().symlink_to(self.root / "nowhere.jsonl")
        with self.assertRaises(ObservationError) as caught:
            read("441", self.root)
        self.assertIn("points nowhere", str(caught.exception))

    def test_a_store_under_an_untraversable_directory_is_refused(self) -> None:
        """And the fourth: a permission error also answers False to `exists()`."""
        if os.geteuid() == 0:  # pragma: no cover - root ignores the mode bits
            self.skipTest("running as root; the mode bits would not be enforced")
        parent = self.store().parent
        parent.mkdir(parents=True, exist_ok=True)
        self.store().write_text("", encoding="utf-8")
        parent.chmod(0o000)
        self.addCleanup(parent.chmod, 0o755)
        with self.assertRaises(ObservationError) as caught:
            read("441", self.root)
        self.assertIn("Permission denied", str(caught.exception))

    def test_a_store_that_is_not_utf8_is_refused(self) -> None:
        self.store().parent.mkdir(parents=True, exist_ok=True)
        self.store().write_bytes(b"\xff\xfe not text\n")
        with self.assertRaises(ObservationError):
            read("441", self.root)

    def test_a_malformed_row_is_refused_by_line(self) -> None:
        path = self.write(session("s1", counts={"/feed/timeline/": 1}).to_dict())
        path.write_text(
            path.read_text(encoding="utf-8") + "not json\n", encoding="utf-8"
        )
        with self.assertRaises(ObservationError) as caught:
            read("441", self.root)
        self.assertIn(":2:", str(caught.exception))

    def test_blank_lines_are_skipped(self) -> None:
        path = self.write(session("s1", counts={"/feed/timeline/": 1}).to_dict())
        path.write_text("\n" + path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        self.assertEqual(1, len(read("441", self.root)))

    def test_a_session_filed_under_the_wrong_version_is_refused(self) -> None:
        """A 440 session read as 441 is a claim about a build never installed."""
        self.write(session("s1", version="440", counts={"/feed/timeline/": 1}).to_dict())
        with self.assertRaises(ObservationError) as caught:
            read("441", self.root)
        self.assertIn("440", str(caught.exception))

    def test_two_rows_with_one_id_in_the_file_are_refused(self) -> None:
        row = session("s1", counts={"/feed/timeline/": 1}).to_dict()
        self.write(row, row)
        with self.assertRaises(ObservationError) as caught:
            read("441", self.root)
        self.assertIn("appears twice", str(caught.exception))

    def test_a_bad_version_is_refused_before_a_path_is_built(self) -> None:
        with self.assertRaises(ObservationError):
            store_path("v441", self.root)


class ScopingTests(RootedTestCase):
    """Every read and write is decided by `root`, never by the process directory.

    Half-scoped is worse than unscoped because it looks right, and this module
    has one default that is relative — which is what makes an override possible to
    forget in the first place.
    """

    def test_the_default_store_path_is_relative_to_the_given_root(self) -> None:
        self.assertEqual(Path(OBSERVATIONS) / "441.jsonl", store_path("441"))
        self.assertEqual(self.root / OBSERVATIONS / "441.jsonl", store_path("441", self.root))

    def test_the_answer_does_not_change_with_the_process_directory(self) -> None:
        append(session("s1", counts={"/feed/timeline/": 2}), root=self.root)
        here = never_observed("441", self.root, toggles=ALL_OFF)

        decoy = tempfile.TemporaryDirectory()
        self.addCleanup(decoy.cleanup)
        decoy_root = Path(decoy.name).resolve()
        # Contradicts the corpus in every answer: nothing observed at all, so a
        # CWD-relative read would refuse rather than agree.
        append(
            session("decoy", watched=("/decoy/only/",), counts={}),
            root=decoy_root,
        )
        previous = os.getcwd()
        os.chdir(decoy_root)
        self.addCleanup(os.chdir, previous)

        self.assertEqual(here, never_observed("441", self.root, toggles=ALL_OFF))
        self.assertEqual(("/feed/reels_tray/",), here)


# ===========================================================================
#   never_observed — the refusal this module exists for
# ===========================================================================


class NeverObservedTests(RootedTestCase):
    def test_a_watched_path_no_session_saw_is_reported(self) -> None:
        append(session("s1", counts={"/feed/timeline/": 7}), root=self.root)
        self.assertEqual(
            ("/feed/reels_tray/",), never_observed("441", self.root, toggles=ALL_OFF)
        )

    def test_a_path_seen_in_any_session_is_not_reported(self) -> None:
        """The union across sessions *under one state*, not the intersection. A
        path the feed session never saw and the explore session did was
        observed."""
        append(session("s1", counts={"/feed/timeline/": 7}), root=self.root)
        append(
            session("s2", surface="explore_tab", counts={"/feed/reels_tray/": 1}),
            root=self.root,
        )
        self.assertEqual((), never_observed("441", self.root, toggles=ALL_OFF))

    def test_everything_seen_gives_an_empty_tuple(self) -> None:
        """The positive control for every refusal below.

        `()` has to be a real answer that a real corpus can produce, or the
        refusals prove nothing — they would just be the only branch anyone reaches.
        """
        append(
            session("s1", counts={"/feed/timeline/": 1, "/feed/reels_tray/": 1}),
            root=self.root,
        )
        self.assertEqual((), never_observed("441", self.root, toggles=ALL_OFF))

    def test_a_path_watched_only_by_a_vacuous_session_is_not_reported(self) -> None:
        """The heart of it.

        `s2` watched `/feed/gone/` and saw nothing whatever — which is equally
        well explained by a build that was not observing, an empty capture, and an
        app that never ran. Naming `/feed/gone/` from that session would be a
        confident negative claim resting on a measurement that may not have
        happened.
        """
        append(session("s1", counts={"/feed/timeline/": 3}), root=self.root)
        append(
            session(
                "s2",
                surface="reels_tab",
                watched=("/feed/gone/", "/feed/other/"),
                counts={},
            ),
            root=self.root,
        )
        self.assertEqual(
            ("/feed/reels_tray/",), never_observed("441", self.root, toggles=ALL_OFF)
        )

    def test_one_evidential_session_is_enough(self) -> None:
        """Deliberately no minimum-N. Two vacuous sessions do not weaken the one
        that observed something, and no constant decides how many are enough —
        `derive-the-threshold-never-declare-it`."""
        append(session("v1", counts={}), root=self.root)
        append(session("s1", counts={"/feed/timeline/": 1}), root=self.root)
        append(session("v2", counts={}), root=self.root)
        self.assertEqual(
            ("/feed/reels_tray/",), never_observed("441", self.root, toggles=ALL_OFF)
        )

    def test_a_store_with_no_sessions_refuses(self) -> None:
        with self.assertRaises(ObservationError) as caught:
            never_observed("441", self.root, toggles=ALL_OFF)
        self.assertIn("no observation evidence", str(caught.exception))

    def test_a_store_whose_sessions_are_all_vacuous_refuses(self) -> None:
        """Not `()`. That is the same answer it gives when every watched path was
        seen, so returning it would report "we measured nothing" in the words of
        "nothing is wrong"."""
        append(session("v1", counts={}), root=self.root)
        append(session("v2", surface="explore_tab", counts={}), root=self.root)
        with self.assertRaises(ObservationError) as caught:
            never_observed("441", self.root, toggles=ALL_OFF)
        self.assertIn("vacuous", str(caught.exception))

    def test_the_two_refusals_say_different_things(self) -> None:
        """"Nothing was recorded" and "everything recorded saw nothing" are
        different problems with different fixes, and a human reads the message."""
        empty = self.assertRaisesRegex(ObservationError, "no observation evidence")
        with empty:
            never_observed("441", self.root, toggles=ALL_OFF)
        append(session("v1", counts={}), root=self.root)
        with self.assertRaises(ObservationError) as caught:
            never_observed("441", self.root, toggles=ALL_OFF)
        self.assertNotIn("holds no session", str(caught.exception))

    def test_an_unreadable_store_refuses_rather_than_reporting_nothing(self) -> None:
        self.store().parent.mkdir(parents=True, exist_ok=True)
        self.store().write_text("{ not json\n", encoding="utf-8")
        with self.assertRaises(ObservationError):
            never_observed("441", self.root, toggles=ALL_OFF)

    def test_evidential_keeps_exactly_the_sessions_that_saw_something(self) -> None:
        sessions = (session("v", counts={}), session("s", counts={"/feed/timeline/": 1}))
        self.assertEqual(("s",), tuple(i.session_id for i in evidential(sessions)))

    def test_stated_keeps_exactly_the_sessions_that_say_what_was_active(self) -> None:
        sessions = (session("u", toggles=None), session("s", toggles=ALL_OFF))
        self.assertEqual(("s",), tuple(i.session_id for i in stated(sessions)))


# ===========================================================================
#   never_observed — and the refusal to blend two experiments
# ===========================================================================


class ToggleScopedAnswerTests(RootedTestCase):
    """Sessions measured under different toggle states answer different questions.

    The measurement that forced this: same build, same walk, only the five
    toggles changed. `/feed/injected_reels_media/` was requested 0 times with the
    blocks on and 3 times with them off, because blocking `/feed/timeline/`
    leaves no timeline response for it to be injected into. Union those two
    sessions and the answer is about neither configuration.
    """

    def corpus(self) -> None:
        """One all-off exploration session and one shipped-configuration session.

        Both non-vacuous, both watching the same list, and they disagree about
        exactly the path the real measurement disagreed about.
        """
        watched = ("/feed/timeline/", "/feed/injected_reels_media/")
        append(
            session(
                "explore-all-off",
                toggles=ALL_OFF,
                watched=watched,
                counts={"/feed/timeline/": 28, "/feed/injected_reels_media/": 3},
            ),
            root=self.root,
        )
        append(
            session(
                "isolation-feed-on",
                toggles=FEED_ON,
                watched=watched,
                counts={"/feed/timeline/": 28},
            ),
            root=self.root,
        )

    def test_each_state_answers_its_own_question(self) -> None:
        """The heart of it. With the blocks off the path was requested; with the
        feed blocked it was not, and that zero is ours."""
        self.corpus()
        self.assertEqual((), never_observed("441", self.root, toggles=ALL_OFF))
        self.assertEqual(
            ("/feed/injected_reels_media/",),
            never_observed("441", self.root, toggles=FEED_ON),
        )

    def test_the_two_sessions_are_never_unioned(self) -> None:
        """The mutation this class exists to catch: dropping the state filter.

        A union would answer `()` for both states — the all-off session saw the
        path, so the blocked session's zero would disappear into it — and `()` is
        a real answer that a real corpus produces, which is exactly why the test
        above cannot be the only one. Here the blocked state's answer is
        non-empty, so a filter that silently stopped filtering changes it.
        """
        self.corpus()
        blended = set()
        for state in states("441", self.root):
            blended |= set(never_observed("441", self.root, toggles=state))
        self.assertEqual({"/feed/injected_reels_media/"}, blended)
        self.assertNotEqual(
            never_observed("441", self.root, toggles=ALL_OFF),
            never_observed("441", self.root, toggles=FEED_ON),
        )

    def test_a_state_nobody_measured_refuses_and_names_the_ones_on_record(self) -> None:
        """The selector can only choose, never assert.

        Choosing a configuration nothing was measured under is a refusal that
        lists what *was* measured — so a typo cannot come back as an answer, and
        the operator can see which experiments exist without reading the store.
        """
        self.corpus()
        with self.assertRaises(ObservationError) as caught:
            never_observed("441", self.root, toggles=ALL_ON)
        self.assertIn("no session for 441 was measured with", str(caught.exception))
        self.assertIn(ALL_OFF.text, str(caught.exception))
        self.assertIn(FEED_ON.text, str(caught.exception))

    def test_the_states_on_record_are_the_evidential_stated_ones(self) -> None:
        self.corpus()
        append(session("vacuous", toggles=ALL_ON, counts={}), root=self.root)
        append(session("unstated", toggles=None, counts={}), root=self.root)
        # A state carried only by a vacuous session is not a state you can ask
        # about: that session is evidence about no path under any configuration.
        self.assertEqual((ALL_OFF, FEED_ON), states("441", self.root))

    def test_a_corpus_whose_evidence_states_no_state_refuses(self) -> None:
        """The committed 441 row's shape. It is not "probably all off".

        The refusal names the sessions, because the operator's next question is
        which capture to take again.
        """
        # Written directly, because `append` will not make one of these any more.
        # The committed row predates the rule and the reader has to keep reading it.
        self.write(session("old", toggles=None, counts={"/feed/timeline/": 28}).to_dict())
        with self.assertRaises(ObservationError) as caught:
            never_observed("441", self.root, toggles=ALL_OFF)
        self.assertIn("states which blocks were active", str(caught.exception))
        self.assertIn("old", str(caught.exception))
        self.assertEqual((), states("441", self.root))

    def test_an_unstated_session_is_excluded_rather_than_joined(self) -> None:
        """It must not widen the watch list either.

        `/feed/only_old_watched/` is watched only by the row that cannot say what
        was active. Counting it as "watched and never observed" would let a
        measurement nobody can read produce a finding.
        """
        self.write(
            session(
                "old",
                toggles=None,
                watched=("/feed/timeline/", "/feed/only_old_watched/"),
                counts={"/feed/timeline/": 28},
            ).to_dict(),
            session(
                "new",
                toggles=ALL_OFF,
                watched=("/feed/timeline/", "/feed/reels_tray/"),
                counts={"/feed/timeline/": 3},
            ).to_dict(),
        )
        self.assertEqual(
            ("/feed/reels_tray/",), never_observed("441", self.root, toggles=ALL_OFF)
        )

    def test_the_state_must_be_a_state_and_not_its_spelling(self) -> None:
        """A string would compare unequal to every recorded state and refuse with
        "nobody measured that" — a true sentence about the wrong problem."""
        self.corpus()
        with self.assertRaises(ObservationError) as caught:
            never_observed("441", self.root, toggles=ALL_OFF.text)
        self.assertIn("ToggleState", str(caught.exception))

    def test_the_answer_is_required_to_name_a_state(self) -> None:
        """No default, and deliberately not "the only state on record".

        A default that worked while one experiment existed would change meaning
        the day a second was filed — the caller's question would have been
        re-pointed by somebody else's session. Both functions, because the
        blocked-endpoint one is where the question is at its most circular.
        """
        self.corpus()
        for answer in (never_observed, blocked_and_never_observed):
            with self.subTest(answer=answer.__name__):
                with self.assertRaises(TypeError):
                    answer("441", self.root)  # type: ignore[call-arg]

    def test_the_four_refusals_say_four_different_things(self) -> None:
        """Each has a different fix: record something, walk the app, take a
        capture from a build that reports itself, ask about a state you measured.

        Asserted by naming the sentence each corpus must produce, not by counting
        distinct strings: with the third refusal deleted, that corpus falls
        through to the fourth, whose message embeds a different list of states —
        so four strings stay distinct while only three causes exist. Counting
        passed with a refusal removed; this does not.
        """
        seen: list[str] = []
        rows: list[dict] = []
        for row, expected in (
            (None, "no observation evidence"),
            (session("v1", toggles=ALL_OFF, counts={}), "are vacuous"),
            (session("old", toggles=None, counts={"/feed/timeline/": 1}),
             "states which blocks were active"),
            (session("s1", toggles=FEED_ON, counts={"/feed/timeline/": 1}),
             "no session for 441 was measured with"),
        ):
            with self.subTest(expected=expected):
                if row is not None:
                    rows.append(row.to_dict())
                    self.write(*rows)
                with self.assertRaises(ObservationError) as caught:
                    never_observed("441", self.root, toggles=ALL_OFF)
                self.assertIn(expected, str(caught.exception))
                seen.append(str(caught.exception))

        self.assertEqual(4, len(set(seen)), seen)
        # And the last one is answerable under the state that was measured, so
        # the corpus above is not simply broken.
        self.assertEqual(
            ("/feed/reels_tray/",), never_observed("441", self.root, toggles=FEED_ON)
        )


# ===========================================================================
#   reporting — the two views must say the same things
# ===========================================================================


class BlockedAndNeverObservedTests(RootedTestCase):
    """The surviving half of the deleted `reconsider.block_never_observed` rule.

    It asked which currently-blocked endpoints a version never once requested,
    in order to propose *withdrawing* the block through a reversal gate. The gate
    is gone; the question is not. What has to hold is that it stays a
    measurement — same refusal discipline as `never_observed`, and no claim about
    a path nothing was watching.
    """

    def manifest(self, *literals: str, hook_id: str = "tigon_url_block") -> Path:
        """`manifest/hooks.json` in the shape `guards.rules_from_manifest` reads.

        Written in the real shape rather than monkeypatched, so a change to the
        manifest key or the rule schema fails here rather than passing against a
        stub of a format nobody uses.
        """

        path = self.root / "manifest" / "hooks.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({
                "schema_version": 1,
                "hooks": [{
                    "hook_id": hook_id,
                    "strategy": "url_block",
                    "url_block_rules": [
                        {
                            "literals": [{"text": text, "match": "contains"}],
                            "toggles": ["disable_feed"],
                        }
                        for text in literals
                    ],
                }],
            }),
            encoding="utf-8",
        )
        return path

    def test_a_blocked_path_never_requested_is_reported(self) -> None:
        self.manifest("/feed/timeline/", "/feed/reels_tray/")
        append(session("s1", counts={"/feed/timeline/": 7}), root=self.root)

        self.assertEqual(
            ("/feed/reels_tray/",),
            blocked_and_never_observed("441", self.root, toggles=ALL_OFF),
        )

    def test_an_unblocked_path_never_requested_is_not_reported(self) -> None:
        """The discriminating case, and the reason this is not `never_observed`.

        `/feed/reels_tray/` was watched and never seen, so `never_observed` names
        it — correctly. Nothing blocks it, so there is no decision to hold up
        against the measurement, and naming it here would turn a watch list into
        a claim about what the project blocks.
        """
        self.manifest("/feed/timeline/")
        append(session("s1", counts={"/feed/timeline/": 7}), root=self.root)

        self.assertIn(
            "/feed/reels_tray/", never_observed("441", self.root, toggles=ALL_OFF)
        )
        self.assertEqual(
            (), blocked_and_never_observed("441", self.root, toggles=ALL_OFF)
        )

    def test_a_blocked_path_that_was_requested_is_not_reported(self) -> None:
        """The positive control: `()` must be reachable from a real corpus."""
        self.manifest("/feed/timeline/", "/feed/reels_tray/")
        append(
            session("s1", counts={"/feed/timeline/": 1, "/feed/reels_tray/": 2}),
            root=self.root,
        )

        self.assertEqual(
            (), blocked_and_never_observed("441", self.root, toggles=ALL_OFF)
        )

    def test_a_blocked_path_nobody_watched_is_absent_rather_than_reported(self) -> None:
        """Silence about it is about the watch list, not about the app.

        Absent from the answer *and* named in the report's warnings — see
        :class:`ReportTests`. An endpoint no build was looking for produces
        exactly the same zero as one the app never asks for.
        """
        self.manifest("/feed/timeline/", "/never/watched/")
        append(session("s1", counts={"/feed/timeline/": 7}), root=self.root)

        self.assertNotIn(
            "/never/watched/",
            blocked_and_never_observed("441", self.root, toggles=ALL_OFF),
        )

    def test_it_refuses_when_no_session_is_evidence_rather_than_answering_empty(self):
        """Inherited from `never_observed`, and deliberately not softened.

        `()` is what this returns when every blocked path was seen, so returning
        it here would report "we measured nothing" in the words of "nothing is
        wrong". The refusal must survive the intersection.
        """
        self.manifest("/feed/timeline/")
        append(session("s1", counts={}), root=self.root)

        with self.assertRaises(ObservationError) as caught:
            blocked_and_never_observed("441", self.root, toggles=ALL_OFF)
        self.assertIn("vacuous", str(caught.exception))

    def test_no_evidence_at_all_refuses_too(self) -> None:
        self.manifest("/feed/timeline/")
        with self.assertRaises(ObservationError) as caught:
            blocked_and_never_observed("441", self.root, toggles=ALL_OFF)
        self.assertIn("no observation evidence", str(caught.exception))

    def test_an_unreadable_manifest_refuses_through_this_modules_error(self) -> None:
        """One refusal channel. A `GuardError` escaping would miss every handler."""
        append(session("s1", counts={"/feed/timeline/": 1}), root=self.root)

        with self.assertRaises(ObservationError):
            blocked_and_never_observed("441", self.root, toggles=ALL_OFF)

        path = self.root / "manifest" / "hooks.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ObservationError) as caught:
            blocked_and_never_observed("441", self.root, toggles=ALL_OFF)
        self.assertIn("hooks.json", str(caught.exception))

    def test_a_manifest_declaring_no_block_refuses_rather_than_answering_empty(self):
        """"Nothing is blocked" and "every block was observed" are not one answer."""
        self.manifest()
        append(session("s1", counts={"/feed/timeline/": 1}), root=self.root)

        with self.assertRaises(ObservationError) as caught:
            blocked_endpoints(self.root)
        self.assertIn("url_block_rules", str(caught.exception))

    def test_the_literals_join_verbatim_with_the_watch_list(self) -> None:
        """No spelling rule, because both sides are rendered from the same field.

        A leading slash going unnormalised is how an entire grouping went
        invisible on 440. The defence here is having nothing to normalise, and
        this pins it: a manifest literal that differs only by its leading slash
        does NOT match a watched one, which is what makes the sameness load
        bearing rather than incidental.
        """
        self.manifest("feed/timeline_stream/")
        append(
            session(
                "s1",
                watched=("/feed/timeline/", "/feed/timeline_stream/"),
                counts={"/feed/timeline/": 1},
            ),
            root=self.root,
        )

        self.assertEqual(("feed/timeline_stream/",), blocked_endpoints(self.root))
        self.assertEqual(
            (), blocked_and_never_observed("441", self.root, toggles=ALL_OFF)
        )

    def test_the_answer_is_scoped_to_the_state_it_was_measured_under(self) -> None:
        """This is the question at its most circular, so it must not blend either.

        Under `disable_feed=1` the block is upstream of the request, and "we block
        it and never saw it asked for" is close to a tautology. Under the all-off
        session the same path was requested 3 times. One store, two states, two
        answers — and the tautological one is the one that must not be quotable
        as the other.
        """
        self.manifest("/feed/injected_reels_media/")
        watched = ("/feed/timeline/", "/feed/injected_reels_media/")
        append(
            session("off", toggles=ALL_OFF, watched=watched,
                    counts={"/feed/timeline/": 28, "/feed/injected_reels_media/": 3}),
            root=self.root,
        )
        append(
            session("on", toggles=FEED_ON, watched=watched,
                    counts={"/feed/timeline/": 28}),
            root=self.root,
        )

        self.assertEqual(
            (), blocked_and_never_observed("441", self.root, toggles=ALL_OFF)
        )
        self.assertEqual(
            ("/feed/injected_reels_media/",),
            blocked_and_never_observed("441", self.root, toggles=FEED_ON),
        )

    def test_the_answer_is_scoped_to_the_root_it_was_given(self) -> None:
        """Both halves — the store and the manifest — or `--root` is half-scoped."""
        self.manifest("/feed/timeline/", "/feed/reels_tray/")
        append(session("s1", counts={"/feed/timeline/": 7}), root=self.root)

        previous = os.getcwd()
        os.chdir(REPOSITORY)
        self.addCleanup(os.chdir, previous)

        self.assertEqual(
            ("/feed/reels_tray/",),
            blocked_and_never_observed("441", self.root, toggles=ALL_OFF),
        )


class ReportTests(RootedTestCase):
    def run_main(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def corpus(self) -> None:
        append(session("s1", counts={"/feed/timeline/": 12}), root=self.root)
        append(session("v1", surface="reels_tab", counts={}), root=self.root)

    def two_states(self) -> None:
        """The corpus the protocol produces: one all-off walk, one isolation run."""
        watched = ("/feed/timeline/", "/feed/injected_reels_media/")
        append(
            session("off", toggles=ALL_OFF, watched=watched,
                    counts={"/feed/timeline/": 28, "/feed/injected_reels_media/": 3}),
            root=self.root,
        )
        append(
            session("on", surface="feed_tab_blocked", toggles=FEED_ON, watched=watched,
                    counts={"/feed/timeline/": 28}),
            root=self.root,
        )

    def test_the_report_names_what_was_never_observed_under_each_state(self) -> None:
        self.corpus()
        report = summary("441", self.root)
        self.assertEqual(1, len(report["states"]))
        state = report["states"][0]
        self.assertEqual(ALL_OFF.text, state["toggles_text"])
        self.assertEqual(ALL_OFF.as_dict(), state["toggles"])
        self.assertEqual(["/feed/reels_tray/"], state["never_observed"])
        self.assertEqual({"/feed/timeline/": 12}, state["observed"])
        self.assertEqual(["s1"], state["session_ids"])
        self.assertEqual(2, report["session_count"])
        self.assertEqual(1, report["evidential_session_count"])
        self.assertEqual(1, report["stated_session_count"])
        self.assertEqual(["v1"], report["vacuous_session_ids"])
        self.assertEqual("", report["unanswerable_reason"])

    def test_there_is_no_whole_version_answer_to_read_by_mistake(self) -> None:
        """The blended field is gone rather than kept beside the per-state ones.

        A script reading `never_observed` off this report used to get a union of
        every evidential session; keeping the key would mean it still does, and
        the union of an all-off walk and a blocked walk describes no
        configuration. A missing key fails loudly; a blended one does not fail.
        """
        self.two_states()
        report = summary("441", self.root)
        for gone in ("never_observed", "observed", "surfaces",
                     "blocked_never_observed", "never_observed_refused"):
            self.assertNotIn(gone, report)
        # And the report is not simply empty: an absence assertion whose subject
        # never existed passes against a `summary` that returns `{}`.
        self.assertEqual(2, len(report["states"]))
        self.assertTrue(all(item["never_observed"] is not None for item in report["states"]))

    def test_two_states_are_reported_separately_and_disagree(self) -> None:
        """Same store, same watch list, two experiments and two answers."""
        self.two_states()
        report = summary("441", self.root)
        by_state = {item["toggles_text"]: item for item in report["states"]}
        self.assertEqual(
            [], by_state[ALL_OFF.text]["never_observed"]
        )
        self.assertEqual(
            ["/feed/injected_reels_media/"], by_state[FEED_ON.text]["never_observed"]
        )
        self.assertEqual(["off"], by_state[ALL_OFF.text]["session_ids"])
        self.assertEqual(["on"], by_state[FEED_ON.text]["session_ids"])
        self.assertEqual(["feed_tab"], by_state[ALL_OFF.text]["surfaces"])
        self.assertEqual(["feed_tab_blocked"], by_state[FEED_ON.text]["surfaces"])

    def test_a_state_with_a_block_on_is_marked_circular_in_both_forms(self) -> None:
        """The caution that makes a zero readable, named per state.

        A reader who takes `/feed/injected_reels_media/` off the blocked-state
        list and calls it "never requested" has repeated the exact mistake the
        field was added for — 0 with the blocks on, 3 with them off.
        """
        self.two_states()
        report = summary("441", self.root)
        circular = [w for w in report["warnings"] if "caused by our own blocks" in w]
        self.assertEqual(1, len(circular), report["warnings"])
        self.assertIn(FEED_ON.text, circular[0])
        self.assertIn("disable_feed", circular[0])
        # Named for the state it is about, and not for the other one.
        self.assertNotIn(ALL_OFF.text, circular[0])
        self.assertIn(circular[0], render(report))

        by_state = {item["toggles_text"]: item for item in report["states"]}
        self.assertEqual(circular[0], by_state[FEED_ON.text]["circular"])
        self.assertEqual("", by_state[ALL_OFF.text]["circular"])
        self.assertEqual(["disable_feed"], by_state[FEED_ON.text]["toggles_on"])
        self.assertEqual([], by_state[ALL_OFF.text]["toggles_on"])

    def section(self, text: str, state_text: str) -> str:
        """The part of the rendered report that belongs to one state.

        Searching the whole document would find `/feed/injected_reels_media/` in
        the *other* state's OBSERVED block — it was requested 3 times there —
        which is how a placement assertion comes to be about the wrong section.
        """
        heading = f"  TOGGLES  {state_text}"
        self.assertIn(heading, text)
        start = text.index(heading)
        rest = text[start + len(heading):]
        following = [
            rest.index(marker) for marker in ("\n  TOGGLES  ", "\n  WARNINGS")
            if marker in rest
        ]
        return rest[: min(following)] if following else rest

    def test_the_circularity_caution_is_printed_above_the_list_it_is_about(self) -> None:
        """Placement, because a caution the reader has already passed is not one.

        The most dangerous line in this report is a never-observed literal under a
        blocking state. With the caution only in the WARNINGS block at the bottom,
        the reader meets `/feed/injected_reels_media/` first and the reason it is
        meaningless twenty lines later.
        """
        self.two_states()
        text = render(summary("441", self.root))
        blocked = self.section(text, FEED_ON.text)
        self.assertLess(
            blocked.index("caused by our own blocks"),
            blocked.index("/feed/injected_reels_media/"),
        )
        # And the all-off section, whose answer is not circular, does not carry it.
        self.assertNotIn("caused by our own blocks", self.section(text, ALL_OFF.text))

    def test_every_state_names_itself_beside_its_own_answer(self) -> None:
        """Two `WATCHED AND NEVER OBSERVED` blocks back to back, with nothing
        saying which configuration each came from, is the confusion this whole
        change exists to prevent — in the one artefact a human reads before
        deciding. Every literal a state names is printed inside that state's own
        section, under a heading that names the state."""
        self.two_states()
        report = summary("441", self.root)
        text = render(report)
        for state in report["states"]:
            with self.subTest(state=state["toggles_text"]):
                section = self.section(text, state["toggles_text"])
                self.assertIn(", ".join(state["session_ids"]), section)
                self.assertIn(", ".join(state["surfaces"]), section)
                for literal in state["never_observed"]:
                    self.assertIn(literal, section)
        # The control for the slicing: the sections are really different, so an
        # assertion "inside this state's section" is not an assertion about the
        # whole document.
        self.assertNotEqual(
            self.section(text, ALL_OFF.text), self.section(text, FEED_ON.text)
        )
        self.assertNotIn(
            "/feed/injected_reels_media/",
            self.section(text, ALL_OFF.text).split("OBSERVED")[0],
        )

    def test_an_answer_unioning_two_builds_says_so(self) -> None:
        """A toggle name is not a rule. What `disable_feed` blocks comes from the
        manifest the build was rendered from, so two builds of one version can
        report the same state and block different literals — and the answer here
        unions them."""
        append(
            session("first", counts={"/feed/timeline/": 1}), root=self.root
        )
        other = ObservationSession(
            schema_version=SCHEMA_VERSION, version="441", build_sha256="c" * 64,
            recorded_at="2026-08-09T11:00:00Z", session_id="second", surface="feed_tab",
            watched=("/feed/timeline/", "/feed/reels_tray/"), toggles=ALL_OFF,
            walk=WALK, blocks=NO_BLOCKS, counts={"/feed/timeline/": 2},
        )
        append(other, root=self.root)

        report = summary("441", self.root)
        self.assertEqual([BUILD, "c" * 64], report["states"][0]["build_sha256s"])
        named = [w for w in report["warnings"] if "2 builds" in w]
        self.assertEqual(1, len(named), report["warnings"])

    def test_one_build_raises_no_such_warning(self) -> None:
        """The control: it must be absent for the ordinary corpus."""
        self.corpus()
        report = summary("441", self.root)
        self.assertEqual([BUILD], report["states"][0]["build_sha256s"])
        self.assertEqual([], [w for w in report["warnings"] if "builds" in w])

    def test_an_all_off_corpus_carries_no_circularity_caution(self) -> None:
        """The control. A caution printed on every report says nothing at all."""
        self.corpus()
        report = summary("441", self.root)
        self.assertEqual(
            [], [w for w in report["warnings"] if "caused by our own blocks" in w]
        )

    def test_a_session_that_states_no_state_is_named_and_excluded(self) -> None:
        """The committed 441 row's shape, beside a usable one.

        It must not vanish quietly: it is a real 52-request session, and a reader
        who cannot see that it was excluded will wonder where it went.
        """
        self.write(
            session("old", toggles=None, counts={"/feed/timeline/": 28}).to_dict(),
            session("new", toggles=ALL_OFF, counts={"/feed/timeline/": 3}).to_dict(),
        )
        report = summary("441", self.root)
        self.assertEqual(["old"], report["unstated_session_ids"])
        self.assertEqual(1, len(report["states"]))
        self.assertEqual(["new"], report["states"][0]["session_ids"])
        named = [w for w in report["warnings"] if "state no toggle state" in w]
        self.assertEqual(1, len(named), report["warnings"])
        self.assertIn("old", named[0])
        self.assertIn(named[0], render(report))
        # And its 28 requests are not in the answer's counts either.
        self.assertEqual({"/feed/timeline/": 3}, report["states"][0]["observed"])

    def test_a_corpus_that_states_nothing_says_so_once_and_answers_nothing(self) -> None:
        """The whole committed corpus today. `states` is empty and the reason is
        stated — not two wordings of it, which is how two spellings of one fact
        come to disagree."""
        self.write(session("old", toggles=None, counts={"/feed/timeline/": 28}).to_dict())
        report = summary("441", self.root)
        self.assertEqual([], report["states"])
        self.assertIn("states which blocks were active", report["unanswerable_reason"])
        self.assertEqual(
            [report["unanswerable_reason"]],
            [w for w in report["warnings"] if "states which blocks" in w],
        )
        self.assertIn(report["unanswerable_reason"], render(report))

    def test_the_report_carries_the_blocked_answer_and_its_own_refusal(self) -> None:
        """A second refusal string, not a reuse of the first.

        This can fail where `never_observed` succeeds — an unreadable manifest, or
        one declaring no block — and a reader told "all sessions are vacuous" when
        the real fault is a missing `url_block_rules` repairs the wrong thing.
        """
        self.corpus()

        without = summary("441", self.root)

        self.assertEqual([], without["states"][0]["blocked_never_observed"])
        self.assertIn(
            "hooks.json", without["states"][0]["blocked_never_observed_refused"]
        )
        # The control: the sibling answer held on the same corpus, so the refusal
        # above is about the manifest and not about the evidence.
        self.assertEqual(["/feed/reels_tray/"], without["states"][0]["never_observed"])
        self.assertEqual("", without["unanswerable_reason"])
        # And it is audible in the warnings too, because a report with no state at
        # all has no field to hang it on.
        self.assertTrue(any("hooks.json" in w for w in without["warnings"]))

        BlockedAndNeverObservedTests.manifest(self, "/feed/reels_tray/")
        with_manifest = summary("441", self.root)

        self.assertEqual(
            ["/feed/reels_tray/"], with_manifest["states"][0]["blocked_never_observed"]
        )
        self.assertEqual("", with_manifest["states"][0]["blocked_never_observed_refused"])
        self.assertEqual([], [w for w in with_manifest["warnings"] if "hooks.json" in w])

    def test_a_blocked_endpoint_nobody_watched_is_named_in_the_warnings(self) -> None:
        """Otherwise the answer above is quietly incomplete.

        A blocked path absent from every watch list produces the same zero as one
        the app never requests, and it is excluded from the finding. A reader
        asking "is that all of them?" has to be told.
        """
        self.corpus()
        BlockedAndNeverObservedTests.manifest(self, "/feed/reels_tray/", "/never/watched/")

        report = summary("441", self.root)

        self.assertEqual(
            ["/feed/reels_tray/"], report["states"][0]["blocked_never_observed"]
        )
        named = [w for w in report["warnings"] if "/never/watched/" in w]
        self.assertEqual(1, len(named), report["warnings"])
        self.assertNotIn("/feed/reels_tray/", named[0])

    def test_no_warning_names_a_blocked_endpoint_that_was_watched(self) -> None:
        """The control for the test above: the warning must be able to be absent."""
        self.corpus()
        BlockedAndNeverObservedTests.manifest(self, "/feed/reels_tray/")

        report = summary("441", self.root)

        self.assertEqual([], [w for w in report["warnings"] if "watch list" in w])

    def test_the_json_form_carries_every_warning_the_text_form_carries(self) -> None:
        """The recurring defect, from the other direction.

        The human banner and the machine field going out of step has shipped here:
        a script gated on JSON that had no field for the warning the human form
        printed. Every warning is generated once and rendered twice, and this is
        what holds that.
        """
        self.corpus()
        code, out, _ = self.run_main(["--root", str(self.root), "report", "--version", "441"])
        self.assertEqual(0, code)
        code, as_json, _ = self.run_main(
            ["--root", str(self.root), "report", "--version", "441", "--json"]
        )
        self.assertEqual(0, code)
        payload = json.loads(as_json)
        self.assertTrue(payload["warnings"])
        for warning in payload["warnings"]:
            self.assertIn(warning, out)

    #: Lines `render` prints whatever the report says. Everything else must be
    #: *carried* by a field of the report — see the test below for why that is
    #: the assertion and not `render(summary(...)) == text`.
    BOILERPLATE = frozenset({
        "=" * 68,
        "OBSERVED",
        "WARNINGS",
        "NOTHING CAN BE ANSWERED",
        "Every watched path was observed at least once.",
        # No `(n)` on either heading: a count beside the list it counts is a
        # second spelling of a length, and it also passed this test by
        # coincidence, because a corpus of one session carries the string "1".
        "WATCHED AND NEVER OBSERVED",
        "BLOCKED AND NEVER OBSERVED",
        "BLOCKED AND NEVER OBSERVED: refused",
        "Every blocked path this manifest declares was observed.",
        "This measures; it does not decide. A blocked path that was never once "
        "requested is a",
        "fact about this phone and these surfaces — what to do about it is a "
        "human's to decide.",
    })

    def test_no_line_of_the_human_form_is_missing_from_the_machine_form(self) -> None:
        """The parity claim, and the version of it that is not a tautology.

        `render(summary(...)) == text` and `summary(...) == json` both *look*
        structural and prove nothing: each side goes through the function being
        mutated, so blanking a field in `summary` or adding a line inside `render`
        satisfies them. Both mutations survived that test, which is the shape this
        repository calls an assertion that cannot fail.

        So the assertion is about **content**: every line the human sees either is
        declared boilerplate or contains a value that is in the machine form. A
        caution computed inside `render` and given no JSON field carries no such
        value and fails here — which is precisely the defect this project shipped,
        the machine view going quieter than the human one.

        Run over four corpora, because the paths that most need to be loud are the
        ones where nothing was measured and where two experiments were.
        """
        for label, build in (
            ("populated", self.corpus),
            ("two states", self.two_states),
            ("all vacuous", lambda: append(session("v1", counts={}), root=self.root)),
            ("unstated", lambda: self.write(
                session("old", toggles=None, counts={"/feed/timeline/": 1}).to_dict()
            )),
            ("empty", lambda: None),
        ):
            with self.subTest(corpus=label):
                if self.store().parent.exists():
                    for existing in self.store().parent.glob("*.jsonl"):
                        existing.unlink()
                build()
                _, as_json, _ = self.run_main(
                    ["--root", str(self.root), "report", "--version", "441", "--json"]
                )
                report = json.loads(as_json)
                _, text, _ = self.run_main(
                    ["--root", str(self.root), "report", "--version", "441"]
                )

                carried = {report["version"], report["unanswerable_reason"]}
                carried |= set(report["warnings"])
                carried |= set(report["vacuous_session_ids"])
                carried |= set(report["unstated_session_ids"])
                carried |= set(report["unwalked_session_ids"])
                carried |= set(report["walks"])
                carried |= {str(report[key]) for key in
                            ("session_count", "evidential_session_count",
                             "stated_session_count")}
                for state in report["states"]:
                    carried |= {state["toggles_text"]}
                    carried |= set(state["never_observed"]) | set(state["observed"])
                    carried |= set(state["surfaces"]) | set(state["session_ids"])
                    carried |= set(state["walks"])
                    carried |= set(state["blocked_never_observed"])
                    carried |= {state["blocked_never_observed_refused"]}
                    carried |= {str(value) for value in state["observed"].values()}
                carried.discard("")

                for line_of in text.splitlines():
                    stripped = line_of.strip()
                    if not stripped or stripped in self.BOILERPLATE:
                        continue
                    self.assertTrue(
                        any(value in stripped for value in carried),
                        f"{label}: the text says {stripped!r} and no field of the "
                        f"JSON carries it",
                    )

    def test_the_machine_form_never_goes_silent_about_what_it_could_not_say(self) -> None:
        """Content, per corpus, and stated rather than derived from the code.

        Asserting `summary(...) == json.loads(...)` cannot catch a `summary` that
        blanks a field, because it blanks it on both sides. These are the exact
        sentences a script gating on this file needs to find.
        """
        report = summary("441", self.root)
        self.assertIn("no observation evidence", report["unanswerable_reason"])
        self.assertTrue(any("no observation evidence" in w for w in report["warnings"]))

        append(session("v1", counts={}), root=self.root)
        report = summary("441", self.root)
        self.assertIn("vacuous", report["unanswerable_reason"])
        self.assertTrue(any("vacuous" in w for w in report["warnings"]))

        append(session("s1", counts={"/feed/timeline/": 3}), root=self.root)
        report = summary("441", self.root)
        self.assertEqual("", report["unanswerable_reason"])
        self.assertEqual(["/feed/reels_tray/"], report["states"][0]["never_observed"])
        self.assertTrue(any("vacuous" in w for w in report["warnings"]))

    def test_a_vacuous_session_is_reported_in_both_forms(self) -> None:
        self.corpus()
        report = summary("441", self.root)
        self.assertTrue(any("vacuous" in line for line in report["warnings"]))
        self.assertIn("v1", "\n".join(report["warnings"]))
        self.assertIn("v1", render(report))

    def test_the_surfaces_bound_is_stated_on_every_state_that_has_an_answer(self) -> None:
        """A zero means "not on this surface" until somebody says which surfaces
        were walked. It is the reading a human is most likely to get wrong.

        A vacuous session's surface must **not** appear. `reels_tab` here saw
        nothing at all, so naming it answers "would a session have seen this?"
        with a session that would not have seen anything — and the two places the
        surface list is printed must never disagree about it, which asserting only
        the presence of `feed_tab` could not catch.
        """
        self.corpus()
        report = summary("441", self.root)
        self.assertTrue(any("bounded by the surfaces" in w for w in report["warnings"]))
        self.assertEqual(["feed_tab"], report["states"][0]["surfaces"])
        self.assertIn("feed_tab", render(report))
        self.assertNotIn("reels_tab", render(report))
        bound = next(w for w in report["warnings"] if "bounded by the surfaces" in w)
        self.assertNotIn("reels_tab", bound)

    def test_a_refusal_reaches_both_forms_and_neither_reads_as_clean(self) -> None:
        """The dangerous shape: nothing to report because nothing was measured."""
        append(session("v1", counts={}), root=self.root)
        report = summary("441", self.root)
        self.assertEqual([], report["states"])
        self.assertIn("vacuous", report["unanswerable_reason"])

        _, text, _ = self.run_main(["--root", str(self.root), "report", "--version", "441"])
        self.assertIn("NOTHING CAN BE ANSWERED", text)
        self.assertNotIn("Every watched path was observed", text)
        _, as_json, _ = self.run_main(
            ["--root", str(self.root), "report", "--version", "441", "--json"]
        )
        self.assertIn("vacuous", json.loads(as_json)["unanswerable_reason"])

    def test_an_empty_store_says_so_in_both_forms(self) -> None:
        report = summary("441", self.root)
        self.assertTrue(any("no observation evidence" in w for w in report["warnings"]))
        _, text, _ = self.run_main(["--root", str(self.root), "report", "--version", "441"])
        self.assertIn("no observation evidence", text)
        _, as_json, _ = self.run_main(
            ["--root", str(self.root), "report", "--version", "441", "--json"]
        )
        for warning in json.loads(as_json)["warnings"]:
            self.assertIn(warning, text)

    def test_a_clean_report_still_says_what_bounds_it(self) -> None:
        """The banner must be absent sometimes, or its presence proves nothing —
        but the surfaces bound is never absent when there is an answer."""
        append(
            session("s1", counts={"/feed/timeline/": 1, "/feed/reels_tray/": 1}),
            root=self.root,
        )
        report = summary("441", self.root)
        self.assertEqual([], report["states"][0]["never_observed"])
        self.assertEqual("", report["unanswerable_reason"])
        self.assertEqual([], [w for w in report["warnings"] if "vacuous" in w])
        self.assertIn("Every watched path was observed", render(report))


class CommandLineTests(RootedTestCase):
    def run_main(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def capture(self, text: str) -> Path:
        path = self.root / "logcat.txt"
        path.write_text(text, encoding="utf-8")
        return path

    def record_argv(self, capture: Path, *extra: str) -> list[str]:
        return [
            "--root", str(self.root), "record",
            "--version", "441",
            "--build-sha256", BUILD,
            "--recorded-at", "2026-08-09T10:00:00Z",
            "--session-id", "441-feed-1",
            "--surface", "feed_tab",
            "--walk", WALK,
            "--capture", str(capture),
            *extra,
        ]

    def test_a_capture_becomes_a_recorded_session(self) -> None:
        capture = self.capture(
            header(ALL_OFF) + line("/feed/timeline/")
            + header(ALL_OFF) + line("/feed/timeline/")
        )
        code, out, _ = self.run_main(
            self.record_argv(capture, "--watched", "/feed/timeline/",
                             "--watched", "/feed/reels_tray/")
        )
        self.assertEqual(0, code)
        self.assertIn("2 request(s)", out)
        recorded = read("441", self.root)
        self.assertEqual(1, len(recorded))
        self.assertEqual({"/feed/timeline/": 2}, dict(recorded[0].counts))
        self.assertEqual(
            ("/feed/reels_tray/",), never_observed("441", self.root, toggles=ALL_OFF)
        )

    def test_the_recorded_state_comes_from_the_capture_and_is_printed(self) -> None:
        """The operator never types it, and the operator is told what was recorded.

        A state read out of the build and then never shown is a field nobody can
        check against the phone they were just holding.
        """
        capture = self.capture(header(FEED_ON) + line("/feed/timeline/"))
        code, out, _ = self.run_main(
            self.record_argv(capture, "--watched", "/feed/timeline/")
        )
        self.assertEqual(0, code)
        self.assertEqual(FEED_ON, read("441", self.root)[0].toggles)
        self.assertIn(FEED_ON.text, out)

    def test_a_session_measured_with_a_block_on_says_so_when_it_is_recorded(self) -> None:
        """The caution at the moment the number is produced, not only where it is
        read. And it must be absent for an all-off capture, or it says nothing."""
        blocked = self.capture(header(FEED_ON) + line("/feed/timeline/"))
        _, out, _ = self.run_main(
            self.record_argv(blocked, "--watched", "/feed/timeline/")
        )
        self.assertIn("CIRCULAR", out)
        self.assertIn("disable_feed", out)

        off = self.capture(header(ALL_OFF) + line("/feed/timeline/"))
        _, out, _ = self.run_main(
            self.record_argv(off, "--session-id", "441-feed-2", "--watched",
                             "/feed/timeline/")
        )
        self.assertNotIn("CIRCULAR", out)

    def test_the_recorded_state_is_a_function_of_the_capture_alone(self) -> None:
        """The property the whole design rests on, asserted as the property.

        `retirement` shipped a rule reading `effective_from = --version + 1` where
        `--version` came from the same person in the same command; an adversarial
        pass broke it in one line. The equivalent here is an operator-supplied
        state, and the guarantee is that what lands in the row is exactly what
        `parse` read out of the capture — so nothing the command line carries can
        change it, whatever the flag is called.
        """
        text = header(FEED_ON) + line("/feed/timeline/")
        capture = self.capture(text)
        code, _, _ = self.run_main(
            self.record_argv(capture, "--watched", "/feed/timeline/")
        )
        self.assertEqual(0, code)
        self.assertEqual(parse(text).toggles, read("441", self.root)[0].toggles)
        self.assertEqual(FEED_ON, read("441", self.root)[0].toggles)

    def test_the_record_command_offers_no_option_that_could_carry_a_state(self) -> None:
        """And the structural half, because the test above passes against a flag
        nobody used in it.

        An earlier version of this test asserted that `--toggles` specifically was
        rejected; an adversarial pass renamed it `--blocks-active` and the whole
        suite stayed green. A denylist of one flag name is the shape `retirement`
        replaced with an allowlist — so this asks what the parser accepts at all.
        """
        parser = argparse.ArgumentParser()
        parser.add_argument("--root", type=Path, default=Path("."))
        sub = parser.add_subparsers(dest="command", required=True)
        record = _record_parser(sub)
        offered = sorted(action.dest for action in record._actions)
        self.assertEqual(
            [],
            [name for name in offered
             if re.search(r"toggle|block|state|pref|switch", name)],
            f"the record command offers {offered}; a state supplied here would be "
            "the operator asserting what the session measured",
        )
        # The control: this test must be able to fail, and it must be looking at
        # a parser that really does offer the record options.
        self.assertIn("build_sha256", offered)

    def test_a_capture_that_states_nothing_is_refused_and_writes_nothing(self) -> None:
        """The field failure, at the command line: 22 path lines, no statement.

        Exit 2 and an empty store, rather than a row whose 22 counts nobody can
        read.
        """
        capture = self.capture(line("/feed/timeline/") * 22)
        code, _, err = self.run_main(
            self.record_argv(capture, "--watched", "/feed/timeline/")
        )
        self.assertEqual(2, code)
        self.assertIn(TOGGLE_DIRECTIVE, err)
        self.assertEqual((), read("441", self.root))

    def test_a_watch_list_can_come_from_a_file(self) -> None:
        watch = self.root / "watched.txt"
        watch.write_text("/feed/timeline/\n\n/feed/reels_tray/\n", encoding="utf-8")
        capture = self.capture(header(ALL_OFF) + line("/feed/timeline/"))
        code, _, _ = self.run_main(
            self.record_argv(capture, "--watched-from", str(watch))
        )
        self.assertEqual(0, code)
        self.assertEqual(
            ("/feed/timeline/", "/feed/reels_tray/"), read("441", self.root)[0].watched
        )

    def test_a_vacuous_capture_is_recorded_and_announced_as_vacuous(self) -> None:
        """Worth recording — it is the honest record of a capture that saw
        nothing — and worth saying it will never be counted.

        It states no toggle state either, because the pass that would have said
        one never ran. That is the only shape in which an unstated row is written.
        """
        capture = self.capture("08-08 17:31:02.412 1 1 D Other: nothing here\n")
        code, out, _ = self.run_main(
            self.record_argv(capture, "--watched", "/feed/timeline/")
        )
        self.assertEqual(0, code)
        self.assertIn("VACUOUS", out)
        self.assertIn("toggles: not stated", out)
        self.assertEqual(1, len(read("441", self.root)))
        self.assertIsNone(read("441", self.root)[0].toggles)
        with self.assertRaises(ObservationError):
            never_observed("441", self.root, toggles=ALL_OFF)

    def test_recording_with_no_watch_list_is_refused(self) -> None:
        capture = self.capture(header(ALL_OFF) + line("/feed/timeline/"))
        code, _, err = self.run_main(self.record_argv(capture))
        self.assertEqual(2, code)
        self.assertIn("no watch list", err)
        self.assertEqual((), read("441", self.root))

    def test_a_capture_naming_an_unwatched_path_is_refused(self) -> None:
        capture = self.capture(header(ALL_OFF) + line("/feed/surprise/"))
        code, _, err = self.run_main(
            self.record_argv(capture, "--watched", "/feed/timeline/")
        )
        self.assertEqual(2, code)
        self.assertIn("not watching", err)
        self.assertEqual((), read("441", self.root))

    def test_a_malformed_capture_is_refused_rather_than_partly_counted(self) -> None:
        capture = self.capture(
            header(ALL_OFF) + line("/feed/timeline/") + "I DFInstaObserve: \n"
        )
        code, _, err = self.run_main(
            self.record_argv(capture, "--watched", "/feed/timeline/")
        )
        self.assertEqual(2, code)
        self.assertTrue(err.startswith("refused:"), err)

    def test_reporting_a_version_with_no_store_exits_zero(self) -> None:
        """A measurement is not a gate. A non-zero exit would make a port fail
        because a device session has not been taken yet."""
        code, _, _ = self.run_main(["--root", str(self.root), "report", "--version", "441"])
        self.assertEqual(0, code)

    def test_recording_with_an_unusable_timestamp_is_refused_and_writes_nothing(self) -> None:
        """The `--recorded-at banana` shape, in the module that learned it."""
        capture = self.capture(header(ALL_OFF) + line("/feed/timeline/"))
        argv = self.record_argv(capture, "--watched", "/feed/timeline/")
        argv[argv.index("--recorded-at") + 1] = "banana"
        code, _, err = self.run_main(argv)
        self.assertEqual(2, code)
        self.assertIn("ISO 8601", err)
        self.assertEqual((), read("441", self.root))

    def test_a_bad_version_is_refused_with_exit_two(self) -> None:
        code, _, err = self.run_main(["--root", str(self.root), "report", "--version", "nope"])
        self.assertEqual(2, code)
        self.assertIn("not a version number", err)


class CommittedCorpusTests(unittest.TestCase):
    """The one row on record, and what it is allowed to be used for.

    It says 52 requests across 4 of 16 watched paths, and it does **not** say
    which blocks were active — it was recorded on 2026-08-08, before the build
    reported itself. From the design note written the same week: it was walked
    with the blocks on, which is the configuration in which `/feed/timeline/`
    being blocked stops `/feed/injected_reels_media/` from ever being requested.

    This test used to pin the twelve literals that session "never observed". That
    list was a measurement of our own configuration, and it is no longer an
    answer this module will give.
    """

    def test_the_committed_session_is_readable_and_is_not_evidence(self) -> None:
        sessions = read("441", REPOSITORY)
        self.assertTrue(sessions, "the committed session went missing")
        # Non-vacuous — it really did see traffic — and still unusable, which is
        # the whole point: vacuity was never the only way to be unreadable.
        self.assertTrue(evidential(sessions), "every committed session is vacuous")
        self.assertEqual((), stated(sessions))
        seen = {k: v for s in sessions for k, v in s.counts.items()}
        self.assertGreater(seen.get("/feed/timeline/", 0), 20)

    def test_no_toggle_scoped_question_is_answered_from_it(self) -> None:
        """Including the two states somebody is most likely to try.

        A refusal that only covered "all off" would let the same circular list
        out under any other spelling of the experiment.
        """
        for state in (ALL_OFF, ALL_ON, FEED_ON):
            with self.subTest(state=state.text):
                with self.assertRaises(ObservationError) as caught:
                    never_observed("441", REPOSITORY, toggles=state)
                self.assertIn("states which blocks were active", str(caught.exception))
                with self.assertRaises(ObservationError):
                    blocked_and_never_observed("441", REPOSITORY, toggles=state)
        self.assertEqual((), states("441", REPOSITORY))

    def test_the_report_says_what_is_wrong_with_it_rather_than_going_quiet(self) -> None:
        report = summary("441", REPOSITORY)
        self.assertEqual([], report["states"])
        self.assertEqual(1, report["evidential_session_count"])
        self.assertEqual(0, report["stated_session_count"])
        self.assertEqual(["441-long-multisurface"], report["unstated_session_ids"])
        self.assertIn("states which blocks were active", report["unanswerable_reason"])

    def test_reading_the_committed_row_does_not_rewrite_it(self) -> None:
        """`append` rewrites the whole file from `to_dict`, so a round trip that
        added `"toggles": null` would edit an append-only store the next time a
        session was recorded beside this one — and would turn "nobody said" into
        "the build said nothing"."""
        path = REPOSITORY / OBSERVATIONS / "441.jsonl"
        committed = path.read_text(encoding="utf-8").splitlines()
        rows = read("441", REPOSITORY)
        self.assertEqual(len(committed), len(rows))
        for original, row in zip(committed, rows):
            self.assertEqual(original, json.dumps(row.to_dict(), sort_keys=True))

    def test_a_stated_session_would_write_the_field(self) -> None:
        """The control for the round trip above: `to_dict` omitting `toggles`
        always would satisfy it, and would also delete the feature."""
        row = session(toggles=FEED_ON, counts={"/feed/timeline/": 1}).to_dict()
        self.assertIn("toggles", json.dumps(row, sort_keys=True))

    def test_every_committed_row_round_trips_and_states_what_it_counted(self) -> None:
        """Twelve 439 rows, byte for byte, each carrying a measured block count.

        They were re-derived from `manifest/captures/` once the counter existed;
        that regeneration changed exactly one field and left everything else
        identical, which is why the captures are committed at all — the store is
        now checkable against something rather than merely present.

        `blocks` must be a real count and never `None`: a row that cannot say what
        it counted makes the arm it belongs to unreadable, and reading a missing
        count as zero would turn "nobody looked" into "nothing happened".
        """
        path = REPOSITORY / OBSERVATIONS / "439.jsonl"
        committed = path.read_text(encoding="utf-8").splitlines()
        rows = read("439", REPOSITORY)
        # Twenty-four: twelve on each walk. They are kept apart by `walk`, never
        # pooled — the two do not agree about `/feed/timeline/` or
        # `/clips/discover`, which is the whole reason the field exists.
        self.assertEqual(24, len(rows))
        self.assertEqual({"one-pass-v1": 12, "three-round-v2": 12},
                         {w: sum(1 for r in rows if r.walk == w)
                          for w in {r.walk for r in rows}})
        for original, row in zip(committed, rows):
            self.assertIsNotNone(row.blocks, row.session_id)
            self.assertEqual(original, json.dumps(row.to_dict(), sort_keys=True))
        # Not vacuous: some arm actually saw blocks, so a counter that always
        # returned zero could not satisfy this.
        self.assertTrue(any(r.blocks and r.blocks.total for r in rows))

    def test_each_committed_row_can_be_re_derived_from_its_committed_capture(self) -> None:
        """The property the captures exist for, asserted rather than asserted about.

        A store nothing can be checked against is a store that has to be trusted.
        Every row's counts must fall out of parsing the redacted capture named for
        its session id.
        """
        for row in read("439", REPOSITORY):
            capture = REPOSITORY / "manifest" / "captures" / f"{row.session_id}.log"
            with self.subTest(session=row.session_id):
                self.assertTrue(capture.is_file(), f"no capture for {row.session_id}")
                again = parse(capture.read_text(encoding="utf-8"))
                self.assertEqual(dict(row.counts), dict(again.counts))
                self.assertEqual(row.toggles, again.toggles)


class BlockCountingTests(unittest.TestCase):
    """The second signal: what Instagram reported when the guard threw.

    Every negative here has a positive twin. A regex that matched nothing would
    satisfy "the echo is not counted" on its own, so each of those tests also
    asserts the header beside it *was* counted.
    """

    def capture(self, *payloads: str, state: ToggleState = ALL_OFF) -> str:
        return header(state) + "".join(payloads)

    def error(self, payload: str, *, level: str = "E") -> str:
        return f"08-10 20:05:11.310 20544 20544 {level} {BLOCK_TAG}: {payload}\n"

    def test_the_header_is_counted_once_per_event(self) -> None:
        text = self.capture(
            self.error("FEED_NOT_LOADING"),
            self.error(BLOCK_MESSAGE),
            self.error("FEED_NOT_LOADING"),
            self.error(BLOCK_MESSAGE),
        )
        self.assertEqual(2, parse(text).blocks.total)

    def test_the_payload_echo_of_the_same_event_is_not_a_second_event(self) -> None:
        """Both spellings. A denylist of one field name would have missed the other."""
        text = self.capture(
            self.error(BLOCK_MESSAGE),
            self.error(f"\t NETWORK_FAILURE_REASON = {BLOCK_MESSAGE.split(': ')[1]}"),
            self.error(f"\t FAILURE_REASON = {BLOCK_MESSAGE.split(': ')[1]}"),
        )
        self.assertEqual(1, parse(text).blocks.total, "the header, and only it")

    def test_the_stack_frame_naming_the_guard_is_not_an_event(self) -> None:
        text = self.capture(
            self.error(BLOCK_MESSAGE),
            self.error("\tat com.dfinstagram.hooks.throwIfBlocked(dex-id-abc:352)"),
        )
        self.assertEqual(1, parse(text).blocks.total)

    def test_narration_quoting_the_message_is_not_an_event(self) -> None:
        """Re-narration, arriving from a third direction. `probes` paid for this once."""
        text = self.capture(
            self.error(
                "After 3 seconds, same action, then App responded with: network issues: "
                "Network request IgApi discover/topical_explore/ failed with 0, error "
                f"message: fault_message: {BLOCK_MESSAGE.split(': ')[1]}."
            )
        )
        self.assertEqual(0, parse(text).blocks.total)
        self.assertEqual(
            1,
            parse(self.capture(self.error(BLOCK_MESSAGE))).blocks.total,
            "the control: the real header in the same shape of capture is counted",
        )

    def test_a_message_with_anything_after_it_is_not_the_header(self) -> None:
        """The end anchor. Every other negative here fails earlier in the pattern.

        The five above (both echo spellings, the stack frame, the narration, the
        wrong tag position) all differ from the header *before* its last character,
        so a pattern with no `$` satisfies all of them and still counts this.
        """
        trailing = self.capture(
            self.error(BLOCK_MESSAGE + " while loading /feed/timeline/")
        )
        self.assertEqual(0, parse(trailing).blocks.total)
        self.assertEqual(
            1,
            parse(self.capture(self.error(BLOCK_MESSAGE))).blocks.total,
            "the control: the same line without the tail is the event",
        )

    def test_the_tag_is_anchored_in_tag_position(self) -> None:
        """Another component quoting the line is not the guard throwing."""
        text = self.capture(
            f"08-10 20:05:11.310 1 1 W SomeOtherTag: {BLOCK_TAG}: {BLOCK_MESSAGE}\n"
        )
        self.assertEqual(0, parse(text).blocks.total)

    def test_the_line_above_names_the_feature(self) -> None:
        text = self.capture(
            self.error("FEED_NOT_LOADING"),
            self.error(BLOCK_MESSAGE),
            self.error("STORY_NOT_LOADING"),
            self.error(BLOCK_MESSAGE),
            self.error(BLOCK_MESSAGE),
        )
        blocks = parse(text).blocks
        self.assertEqual(3, blocks.total)
        self.assertEqual(
            {"FEED_NOT_LOADING": 1, "STORY_NOT_LOADING": 1, UNATTRIBUTED: 1},
            blocks.features,
            "the third follows a header, which names no feature",
        )

    def test_an_unrelated_line_above_is_not_read_as_a_feature(self) -> None:
        text = self.capture(
            "08-10 20:05:11.306 20544 20544 W BackgroundStartupDetector: cold\n",
            self.error(BLOCK_MESSAGE),
        )
        self.assertEqual({UNATTRIBUTED: 1}, parse(text).blocks.features)

    def test_a_block_before_any_directive_refuses(self) -> None:
        """Its configuration is unnamed, and a phantom block in a baseline is the
        one number that must not be inventable."""
        with self.assertRaises(ObservationError) as caught:
            parse(self.error(BLOCK_MESSAGE) + header())
        self.assertIn(TOGGLE_DIRECTIVE, str(caught.exception))

    def test_a_capture_with_no_block_reports_a_measured_zero(self) -> None:
        """Not `None`. Reading a capture and finding none is evidence."""
        capture = parse(self.capture(line("/feed/timeline/")))
        self.assertEqual(0, capture.blocks.total)
        self.assertEqual((), capture.blocks.by_feature)


class BlockCountShapeTests(unittest.TestCase):
    def test_a_breakdown_that_does_not_sum_to_the_total_refuses(self) -> None:
        with self.assertRaises(ObservationError) as caught:
            BlockCount.of(20, {"FEED_NOT_LOADING": 19})
        self.assertIn("summing to", str(caught.exception))

    def test_a_zero_in_the_breakdown_refuses(self) -> None:
        with self.assertRaises(ObservationError):
            BlockCount.of(1, {"FEED_NOT_LOADING": 1, "STORY_NOT_LOADING": 0})

    def test_a_negative_total_refuses(self) -> None:
        with self.assertRaises(ObservationError):
            BlockCount(-1)

    def test_a_feature_that_is_not_a_category_refuses(self) -> None:
        with self.assertRaises(ObservationError) as caught:
            BlockCount.of(1, {"feed not loading": 1})
        self.assertIn("feature category", str(caught.exception))

    def test_the_unattributed_key_is_accepted_and_a_real_one_cannot_collide(self) -> None:
        self.assertEqual(1, BlockCount.of(1, {UNATTRIBUTED: 1}).total)
        with self.assertRaises(ObservationError):
            BlockCount.of(1, {"(anything else)": 1})

    def test_it_round_trips_through_json(self) -> None:
        original = BlockCount.of(20, {"FEED_NOT_LOADING": 20})
        self.assertEqual(original, BlockCount.from_dict(
            json.loads(json.dumps(original.as_dict()))
        ))

    def test_an_unknown_key_refuses(self) -> None:
        with self.assertRaises(ObservationError):
            BlockCount.from_dict({"total": 1, "by_feature": {}, "extra": 1})


class BlockRecordTests(RootedTestCase):
    def test_absent_means_nobody_counted_and_null_is_refused(self) -> None:
        """One spelling for absent, in the one field whose zero is evidence."""
        row = session(counts={"/feed/timeline/": 1}, blocks=None).to_dict()
        self.assertNotIn("blocks", row)
        with self.assertRaises(ObservationError) as caught:
            ObservationSession.from_dict({**row, "blocks": None})
        self.assertIn("second spelling of absent", str(caught.exception))

    def test_a_measured_zero_is_written_and_read_back(self) -> None:
        row = session(counts={"/feed/timeline/": 1}, blocks=BlockCount(0)).to_dict()
        self.assertEqual({"total": 0, "by_feature": {}}, row["blocks"])
        self.assertEqual(BlockCount(0), ObservationSession.from_dict(row).blocks)

    def test_an_int_is_not_coerced_into_a_count(self) -> None:
        """`blocks=0` would be indistinguishable, once stored, from a measurement."""
        with self.assertRaises(ObservationError) as caught:
            session(blocks=0)
        self.assertIn("BlockCount", str(caught.exception))

    def test_the_writer_refuses_a_session_that_counted_nothing(self) -> None:
        with self.assertRaises(ObservationError) as caught:
            append(session(counts={"/feed/timeline/": 1}, blocks=None), root=self.root)
        self.assertIn("states no block count", str(caught.exception))
        self.assertFalse(self.store().exists(), "and nothing was written")

    def test_the_writer_accepts_one_that_counted(self) -> None:
        """The control. A writer that refused everything would pass the test above."""
        append(session(counts={"/feed/timeline/": 1}), root=self.root)
        self.assertEqual(BlockCount(0), read("441", self.root)[0].blocks)

    def test_recording_from_a_capture_carries_the_count_through(self) -> None:
        capture = self.root / "capture.log"
        capture.write_text(
            header()
            + line("/feed/timeline/")
            + f"08-10 20:05:11.310 1 1 E {BLOCK_TAG}: FEED_NOT_LOADING\n"
            + f"08-10 20:05:11.310 1 1 E {BLOCK_TAG}: {BLOCK_MESSAGE}\n",
            encoding="utf-8",
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main([
                "--root", str(self.root), "record", "--version", "441",
                "--build-sha256", BUILD, "--recorded-at", "2026-08-10T10:00:00Z",
                "--session-id", "s1", "--surface", "feed_tab", "--walk", WALK,
                "--watched", "/feed/timeline/", "--capture", str(capture),
            ])
        self.assertEqual(0, code)
        self.assertIn("blocks:  1 (FEED_NOT_LOADING 1)", out.getvalue())
        recorded = read("441", self.root)[0]
        self.assertEqual(1, recorded.blocks.total)
        self.assertEqual({"FEED_NOT_LOADING": 1}, recorded.blocks.features)



# ===========================================================================
#   the walk
# ===========================================================================


class WalkFieldTests(RootedTestCase):
    """Which driving protocol produced a session, and what the field may hold.

    The walk is the one value in a row that an operator types, and this file's
    other sections exist because typed values are what this project got wrong
    before. So the tests here are about the two things that can still be
    defended: the field cannot hold something no other session can match, and the
    field cannot be left out of anything new.
    """

    def test_the_walk_survives_a_round_trip_through_the_store(self) -> None:
        append(session(walk="three-round-v2", counts={"/feed/timeline/": 1}),
               root=self.root)
        self.assertEqual("three-round-v2", read("441", self.root)[0].walk)

    def test_an_unstated_walk_is_absent_from_the_row_and_null_is_refused(self) -> None:
        """One spelling for absent, the third field to need the rule.

        `append` rewrites the whole file from `to_dict`, so a row that came back
        carrying `"walk": null` would edit the twenty-four committed rows the next
        time a session was recorded beside one of them — and would turn "nobody
        said" into "the walk was nothing".
        """
        row = session(walk=None, counts={"/feed/timeline/": 1}).to_dict()
        self.assertNotIn("walk", row)
        with self.assertRaises(ObservationError) as caught:
            ObservationSession.from_dict({**row, "walk": None})
        self.assertIn("second spelling of absent", str(caught.exception))

    def test_a_stated_walk_would_write_the_field(self) -> None:
        """The control for the test above: a `to_dict` that never wrote `walk`
        satisfies it and also deletes the feature."""

        self.assertIn(
            "three-round-v2",
            json.dumps(session(walk="three-round-v2").to_dict(), sort_keys=True),
        )

    def test_a_walk_that_is_not_a_join_key_is_refused(self) -> None:
        """It is compared for equality against other sessions' walks, so two
        spellings of one protocol would halve every group without saying so."""

        for bad in ("Three-Round", "three round", "-leading", "", "  ", "x" * 65,
                    "three/round", "(no walk stated)"):
            with self.subTest(walk=bad):
                with self.assertRaises(ObservationError) as caught:
                    session(walk=bad)
                self.assertIn("walk", str(caught.exception))

    def test_the_legal_shapes_are_accepted(self) -> None:
        """The control: a rule that refused everything would pass the test above."""

        for good in ("three-round-v2", "one_pass", "v2.1", "a", "0"):
            with self.subTest(walk=good):
                self.assertEqual(good, session(walk=good).walk)

    def test_a_walk_that_names_only_this_session_is_refused(self) -> None:
        """A walk is what says two sessions did the same thing. One that is unique
        to a session names an outcome, and every group it could form has one
        member — which `grouping` then refuses for a reason nobody could see."""

        with self.assertRaises(ObservationError) as caught:
            session("440-isolate-feed", walk="440-isolate-feed")
        self.assertIn("names itself", str(caught.exception))
        # And the control, because a rule that refused every walk would pass:
        self.assertEqual("one-pass", session("440-isolate-feed", walk="one-pass").walk)

    def test_new_evidence_without_a_walk_is_refused_at_the_write(self) -> None:
        """The record type still represents history it is no longer allowed to
        make — the twenty-four committed rows are in exactly this shape."""

        with self.assertRaises(ObservationError) as caught:
            append(session(walk=None, counts={"/feed/timeline/": 1}), root=self.root)
        message = str(caught.exception)
        self.assertIn("--walk", message)
        self.assertIn("noise floor", message)
        self.assertEqual((), read("441", self.root))

    def test_a_vacuous_session_without_a_walk_is_refused_too(self) -> None:
        """Unlike the toggle state, which a capture that saw nothing may omit.

        A vacuous capture cannot state its configuration, because the build only
        says it on a checked request — so `append` lets that through. Nothing
        stops an operator naming the walk they ran, whatever the capture holds, so
        there is no equivalent hole here.
        """
        with self.assertRaises(ObservationError):
            append(session(walk=None, counts={}), root=self.root)

    def test_the_walk_recorded_is_exactly_the_one_that_was_typed(self) -> None:
        """Stated rather than derived, and this is what that costs.

        Nothing in the capture names a protocol, so this value is worth what the
        person who typed it is worth. It is asserted here so that a future change
        which started *inferring* it — from the surface, from the session id, from
        the span — would fail rather than quietly become a guess wearing a
        guarantee.
        """
        capture = self.root / "c.log"
        capture.write_text(header(ALL_OFF) + line("/feed/timeline/"), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main([
                "--root", str(self.root), "record", "--version", "441",
                "--build-sha256", BUILD, "--recorded-at", "2026-08-09T10:00:00Z",
                "--session-id", "441-feed-1", "--surface", "feed_tab",
                "--walk", "three-round-v2",
                "--watched", "/feed/timeline/", "--capture", str(capture),
            ])
        self.assertEqual(0, code)
        self.assertEqual("three-round-v2", read("441", self.root)[0].walk)
        self.assertIn("walk:    three-round-v2", out.getvalue())
        # Printed as what it is, at the moment it is filed.
        self.assertIn("yours to state", out.getvalue().lower())

    def test_a_capture_with_no_timestamps_says_nothing_can_contradict_the_walk(
        self,
    ) -> None:
        """The span is the only thing that can catch a wrong `--walk`, and a
        capture that carries none is outside the check. Said at the write rather
        than discovered later."""

        capture = self.root / "c.log"
        capture.write_text(
            f"I {TAG}: {TOGGLE_DIRECTIVE} {ALL_OFF.text}\nI {TAG}: /feed/timeline/\n",
            encoding="utf-8",
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main([
                "--root", str(self.root), "record", "--version", "441",
                "--build-sha256", BUILD, "--recorded-at", "2026-08-09T10:00:00Z",
                "--session-id", "441-feed-2", "--surface", "feed_tab",
                "--walk", "three-round-v2",
                "--watched", "/feed/timeline/", "--capture", str(capture),
            ])
        self.assertEqual(0, code)
        self.assertIn("no timestamps", out.getvalue())
        self.assertIsNone(read("441", self.root)[0].span_seconds)

    def test_record_will_not_run_without_a_walk(self) -> None:
        """Loud, and at the moment the operator still knows which script they ran."""

        capture = self.root / "c.log"
        capture.write_text(header(ALL_OFF) + line("/feed/timeline/"), encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            main([
                "--root", str(self.root), "record", "--version", "441",
                "--build-sha256", BUILD, "--recorded-at", "2026-08-09T10:00:00Z",
                "--session-id", "441-feed-1", "--surface", "feed_tab",
                "--watched", "/feed/timeline/", "--capture", str(capture),
            ])
        self.assertEqual(2, caught.exception.code)
        self.assertIn("--walk", err.getvalue())

    def test_the_walk_is_the_only_option_that_states_a_condition(self) -> None:
        """The allowlist `_record_parser` exists for, re-asked with one member added.

        `--toggles` must still be absent — a build can report that one — and the
        way to be sure is to read the parser rather than a copy of the list.
        """
        options = {
            action.dest
            for action in _record_parser(
                argparse.ArgumentParser().add_subparsers()
            )._actions
        }
        self.assertIn("walk", options)
        self.assertEqual(
            {"help", "version", "build_sha256", "recorded_at", "session_id",
             "surface", "walk", "watched", "watched_from", "capture"},
            options,
        )

    def test_a_report_names_the_walks_each_state_was_measured_on(self) -> None:
        """The same rule the surface list follows: a reader's first question is
        "would this session have seen it?", and a third round reaches things one
        pass does not."""

        append(session("a", walk="one-pass", counts={"/feed/timeline/": 1}),
               root=self.root)
        append(session("b", walk="three-round", counts={"/feed/timeline/": 3}),
               root=self.root)
        report = summary("441", self.root)
        self.assertEqual(["one-pass", "three-round"], report["states"][0]["walks"])
        self.assertEqual(["one-pass", "three-round"], report["walks"])
        self.assertIn("one-pass, three-round", render(report))

    def test_a_report_names_the_sessions_that_state_no_walk(self) -> None:
        """And says why it is still answering from them, which `grouping` is not.

        A negative claim only gets safer as walks are pooled into it — a path gets
        more chances to be seen, never fewer — so this report answers and warns.
        A differential is the other way round.
        """
        self.write(session("old", walk=None, counts={"/feed/timeline/": 1}).to_dict())
        report = summary("441", self.root)
        self.assertEqual(["old"], report["unwalked_session_ids"])
        named = [item for item in report["warnings"] if "name no walk" in item]
        self.assertEqual(1, len(named), report["warnings"])
        self.assertIn("old", named[0])
        # Still answered, unlike a session with no toggle state.
        self.assertEqual(
            ("/feed/reels_tray/",), never_observed("441", self.root, toggles=ALL_OFF)
        )

    def test_the_control_for_that_warning(self) -> None:
        """It must be absent when every session names its walk, or it says nothing."""

        append(session("new", counts={"/feed/timeline/": 1}), root=self.root)
        report = summary("441", self.root)
        self.assertEqual([], report["unwalked_session_ids"])
        self.assertEqual([], [i for i in report["warnings"] if "name no walk" in i])

    def test_the_report_says_when_a_walk_is_contradicted_by_its_own_captures(
        self,
    ) -> None:
        """Asked once per claimed walk, never over the pool.

        Pooling two walks and then asking whether the spans split would find the
        split every time and call an honest corpus a liar. The question is only
        ever "do the sessions claiming *this* protocol agree that they ran it?".
        """
        for index, span in enumerate((120, 121, 122, 360, 361, 362)):
            append(session(f"s{index}", walk="one-pass", span_seconds=span,
                           counts={"/feed/timeline/": 1}), root=self.root)
        report = summary("441", self.root)
        named = [item for item in report["warnings"] if "is contradicted" in item]
        self.assertEqual(1, len(named), report["warnings"])
        self.assertIn("'one-pass'", named[0])

    def test_two_honestly_different_walks_are_not_contradicted(self) -> None:
        """The control, and the one that would fail if the check ran over the pool:
        exactly the same four spans, labelled as the two protocols they are."""

        for index, (walk, span) in enumerate(
            (("one-pass", 120), ("one-pass", 121), ("one-pass", 122),
             ("three-round", 360), ("three-round", 361), ("three-round", 362))
        ):
            append(session(f"s{index}", walk=walk, span_seconds=span,
                           counts={"/feed/timeline/": 1}), root=self.root)
        report = summary("441", self.root)
        self.assertEqual([], [i for i in report["warnings"] if "is contradicted" in i])

    def test_recording_says_so_when_the_new_row_disputes_its_siblings(self) -> None:
        """At the write, where the operator still remembers which script they ran.

        `grouping` refuses on the same evidence later, and by then the fix is
        archaeology.
        """
        # Five sessions cannot dispute anything — the check needs three on each
        # side of a split — so the contradiction becomes visible on the sixth.
        for index, span in enumerate((120, 121, 122, 360, 361)):
            append(session(f"s{index}", walk="one-pass", span_seconds=span,
                           counts={"/feed/timeline/": 1}), root=self.root)
        capture = self.root / "c.log"
        capture.write_text(
            line(f"{TOGGLE_DIRECTIVE} {ALL_OFF.text}", stamp="08-08 17:00:00.000 1 1")
            + line("/feed/timeline/", stamp="08-08 17:06:01.000 1 1"),
            encoding="utf-8",
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main([
                "--root", str(self.root), "record", "--version", "441",
                "--build-sha256", BUILD, "--recorded-at", "2026-08-09T10:00:00Z",
                "--session-id", "late", "--surface", "feed_tab",
                "--walk", "one-pass",
                "--watched", "/feed/timeline/", "--capture", str(capture),
            ])
        self.assertEqual(0, code)
        self.assertIn("WALK DISPUTED", out.getvalue())
        # Recorded anyway: this store never refuses an honest measurement for
        # disagreeing with its neighbours, it says which of them to look at.
        self.assertEqual(6, len(read("441", self.root)))

    def test_the_control_for_the_record_time_dispute(self) -> None:
        """A session that agrees with its siblings must record silently, or the
        line above is printed over every capture and means nothing."""

        for index, span in enumerate((120, 121, 122, 123, 124)):
            append(session(f"s{index}", walk="one-pass", span_seconds=span,
                           counts={"/feed/timeline/": 1}), root=self.root)
        capture = self.root / "c.log"
        capture.write_text(
            line(f"{TOGGLE_DIRECTIVE} {ALL_OFF.text}", stamp="08-08 17:00:00.000 1 1")
            + line("/feed/timeline/", stamp="08-08 17:02:00.000 1 1"),
            encoding="utf-8",
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main([
                "--root", str(self.root), "record", "--version", "441",
                "--build-sha256", BUILD, "--recorded-at", "2026-08-09T10:00:00Z",
                "--session-id", "late", "--surface", "feed_tab",
                "--walk", "one-pass",
                "--watched", "/feed/timeline/", "--capture", str(capture),
            ])
        self.assertEqual(0, code)
        self.assertNotIn("WALK DISPUTED", out.getvalue())



class CaptureSpanTests(unittest.TestCase):
    """How long the capture ran — the only evidence a typed walk can be wrong.

    Measured over the lines the parser **read**, which is not a detail:
    `tools/redact_capture.py` commits a reduction of every capture and its whole
    guarantee is that `parse` answers identically for the reduction and the
    original. A span taken over lines the redaction is free to drop would break
    that for all twenty-four at once.
    """

    def test_the_span_is_the_time_between_the_first_and_last_line_read(self) -> None:
        capture = (
            line(f"{TOGGLE_DIRECTIVE} {ALL_OFF.text}", stamp="08-08 17:31:02.412 1 1")
            + line("/feed/timeline/", stamp="08-08 17:31:02.412 1 1")
            + line("/feed/timeline/", stamp="08-08 17:33:14.900 1 1")
        )
        self.assertEqual(132, parse(capture).span_seconds)

    def test_a_capture_with_no_timestamps_has_no_span_and_is_not_refused(self) -> None:
        """The bare contract form, and a hand-made fixture. `None`, not `0`."""

        capture = f"I {TAG}: {TOGGLE_DIRECTIVE} {ALL_OFF.text}\nI {TAG}: /feed/timeline/\n"
        read_back = parse(capture)
        self.assertIsNone(read_back.span_seconds)
        self.assertEqual({"/feed/timeline/": 1}, dict(read_back.counts))

    def test_one_timestamped_line_is_not_a_span(self) -> None:
        self.assertIsNone(parse(header(ALL_OFF)).span_seconds)

    def test_lines_landing_inside_one_second_measure_zero_not_absent(self) -> None:
        """`0` is a measurement and `None` is not, in the field whose whole job is
        to be compared against another one."""

        self.assertEqual(0, parse(header(ALL_OFF) + line("/feed/timeline/")).span_seconds)

    def test_lines_the_parser_ignores_do_not_move_the_span(self) -> None:
        """The redaction invariant, asserted directly rather than trusted.

        A real capture is 1.4 MB of the phone's whole log and the committed form
        keeps only what `parse` reads. If an unrelated line an hour later could
        stretch the span, every committed capture would parse differently from the
        original it was reduced from, and `redact_capture --verify` would refuse
        all of them.
        """
        core = (
            line(f"{TOGGLE_DIRECTIVE} {ALL_OFF.text}", stamp="08-08 17:31:02.412 1 1")
            + line("/feed/timeline/", stamp="08-08 17:31:12.412 1 1")
        )
        noisy = (
            "08-08 09:00:00.000 1 1 I SomeOtherTag: cold start\n"
            + core
            + "08-08 23:59:00.000 1 1 E IgFunctionalErrorEvent: something else\n"
        )
        self.assertEqual(parse(core), parse(noisy))
        self.assertEqual(10, parse(noisy).span_seconds)

    def test_out_of_order_lines_never_produce_a_negative_span(self) -> None:
        """logcat interleaves buffers, so two lines can arrive milliseconds out of
        order. A subtraction that could come out negative is one somebody would
        have to interpret."""

        capture = (
            line(f"{TOGGLE_DIRECTIVE} {ALL_OFF.text}", stamp="08-08 17:31:10.000 1 1")
            + line("/feed/timeline/", stamp="08-08 17:31:04.000 1 1")
            + line("/feed/timeline/", stamp="08-08 17:31:09.000 1 1")
        )
        self.assertEqual(6, parse(capture).span_seconds)

    def test_a_span_across_a_month_boundary_is_measured(self) -> None:
        """A logcat stamp carries no year but it does carry the month, and a walk
        that starts on the 31st is not a walk that took a month."""

        capture = (
            line(f"{TOGGLE_DIRECTIVE} {ALL_OFF.text}", stamp="08-31 23:59:00.000 1 1")
            + line("/feed/timeline/", stamp="09-01 00:01:00.000 1 1")
        )
        self.assertEqual(120, parse(capture).span_seconds)

    def test_a_stamp_inside_a_message_body_is_not_the_line_s_timestamp(self) -> None:
        """The re-narration failure, arriving at a third field.

        `_OBSERVE_LINE` and `_BLOCK_HEADER` each anchor on tag *position* because a
        line that merely contains their text is another component talking about
        DFInsta. A timestamp read by search rather than from the start of the line
        has the same hole: a watched literal that looks like a logcat stamp would
        become the time the line was written, and two of them would manufacture a
        span out of the app's own payload.
        """
        capture = (
            f"I {TAG}: {TOGGLE_DIRECTIVE} {ALL_OFF.text}\n"
            f"I {TAG}: 08-08 17:00:00.000 /x\n"
            f"I {TAG}: 08-08 17:05:00.000 /x\n"
        )
        self.assertIsNone(parse(capture).span_seconds)
        # And the lines were read: the span is absent because no line *started*
        # with a stamp, not because nothing was parsed.
        self.assertEqual(
            {"08-08 17:00:00.000 /x": 1, "08-08 17:05:00.000 /x": 1},
            dict(parse(capture).counts),
        )

    def test_an_impossible_month_is_ignored_rather_than_crashing(self) -> None:
        """A refusal channel that leaks an IndexError is not a refusal channel.

        The month indexes a table, so a corrupt stamp is the one input that can
        turn `parse` into a traceback rather than an `ObservationError`. It is not
        worth refusing over — the span is corroboration and not a term of the
        contract — but it is worth not crashing over.
        """
        capture = (
            line(f"{TOGGLE_DIRECTIVE} {ALL_OFF.text}", stamp="13-45 10:00:00.000 1 1")
            + line("/feed/timeline/", stamp="99-99 10:00:09.000 1 1")
        )
        self.assertIsNone(parse(capture).span_seconds)
        self.assertEqual({"/feed/timeline/": 1}, dict(parse(capture).counts))
        # The control: the same two lines with legal months are measured, so this
        # is the guard doing its job and not the parser failing to see them.
        legal = (
            line(f"{TOGGLE_DIRECTIVE} {ALL_OFF.text}", stamp="12-31 10:00:00.000 1 1")
            + line("/feed/timeline/", stamp="12-31 10:00:09.000 1 1")
        )
        self.assertEqual(9, parse(legal).span_seconds)

    def test_the_span_round_trips_and_null_is_refused(self) -> None:
        row = session(span_seconds=116, counts={"/feed/timeline/": 1}).to_dict()
        self.assertEqual(116, row["span_seconds"])
        self.assertEqual(116, ObservationSession.from_dict(row).span_seconds)
        self.assertNotIn("span_seconds", session(span_seconds=None).to_dict())
        for bad in (None, "116", 1.5, True):
            with self.subTest(value=bad):
                with self.assertRaises(ObservationError):
                    ObservationSession.from_dict({**row, "span_seconds": bad})
        with self.assertRaises(ObservationError):
            session(span_seconds=-1)

    def test_recording_carries_the_span_from_the_capture(self) -> None:
        """It is derived from the capture and never from a flag: there is
        deliberately no `--span`, because unlike the walk this one is in the
        evidence."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = root / "c.log"
            capture.write_text(
                line(f"{TOGGLE_DIRECTIVE} {ALL_OFF.text}", stamp="08-08 17:31:02.412 1 1")
                + line("/feed/timeline/", stamp="08-08 17:33:02.412 1 1"),
                encoding="utf-8",
            )
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = main([
                    "--root", str(root), "record", "--version", "441",
                    "--build-sha256", BUILD, "--recorded-at", "2026-08-09T10:00:00Z",
                    "--session-id", "s1", "--surface", "feed_tab", "--walk", WALK,
                    "--watched", "/feed/timeline/", "--capture", str(capture),
                ])
            self.assertEqual(0, code)
            self.assertEqual(120, read("441", root)[0].span_seconds)
            self.assertIn("over 120s of capture", out.getvalue())


class WalkDisputeTests(unittest.TestCase):
    """The check that makes a typed walk worth something, and its own control.

    The question has no constant in it: do the spans of the sessions claiming one
    walk split into two groups further apart than either group is wide? So the
    tests here move the *scale* and require the verdict not to move, which is the
    same defence `grouping`'s noise-floor tests use — a threshold hiding in this
    would answer differently at ten times the size.
    """

    def group(self, *spans: int | None, walk: str = "one-pass"):
        return tuple(
            session(f"s{index}", walk=walk, span_seconds=span,
                    counts={"/feed/timeline/": 1})
            for index, span in enumerate(spans)
        )

    def test_two_groups_further_apart_than_either_is_wide_are_a_dispute(self) -> None:
        found = walk_dispute(self.group(116, 117, 118, 350, 351, 352))
        self.assertIn("two groups", found)
        self.assertIn("s0 116s", found)
        self.assertIn("s3 350s", found)

    def test_spans_that_merely_vary_are_not_a_dispute(self) -> None:
        """The control. A rule that always disputed would pass the test above and
        would refuse every honest corpus."""

        self.assertEqual("", walk_dispute(self.group(109, 114, 116, 116, 117)))

    def test_the_verdict_is_the_same_at_ten_times_the_scale(self) -> None:
        """No constant. A `> 60` hidden anywhere in here would answer differently
        for a walk that takes twenty minutes than for one that takes two."""

        honest = (109, 112, 114, 116, 116, 117)
        split = (116, 117, 118, 350, 351, 352)
        for factor in (1, 10, 100):
            with self.subTest(factor=factor):
                self.assertEqual(
                    "", walk_dispute(self.group(*(v * factor for v in honest)))
                )
                self.assertIn(
                    "two groups",
                    walk_dispute(self.group(*(v * factor for v in split))),
                )

    def test_a_minority_apart_from_the_rest_is_not_a_dispute(self) -> None:
        """Three on each side, because a group of one or two has no range of its
        own to be the scale. A walk with a slow session is an operator who took a
        phone call, and a check that cried wolf on that would be turned off — this
        one was measured doing exactly that on 66 of the 495 four-session subsets
        of the committed 439 corpus before the minimum went from two to three."""

        self.assertEqual("", walk_dispute(self.group(116, 116, 117, 118, 119, 350)))
        self.assertEqual("", walk_dispute(self.group(116, 116, 117, 118, 349, 350)))

    def test_too_few_sessions_cannot_fire_and_the_control_says_so(self) -> None:
        """The positive control, as a value rather than as an intention.

        A check that silently could not have fired is the failure this project
        keeps repeating. And the control shares the minimum with the check itself
        rather than keeping a second copy: two copies desynchronised would report
        "fully evidenced" over a corpus the check structurally cannot read, which
        is that same failure arriving through the thing meant to prevent it.
        """
        for count in range(2, 6):
            with self.subTest(count=count):
                rows = self.group(*([116] * (count - 1)), 350)
                self.assertEqual("", walk_dispute(rows))
                self.assertIn("could not have contradicted", walk_evidence(rows))
        # And at the minimum it can fire, so the control must fall silent.
        self.assertEqual("", walk_evidence(self.group(116, 117, 118, 119, 120, 121)))
        self.assertIn("two groups",
                      walk_dispute(self.group(116, 117, 118, 350, 351, 352)))

    def test_a_session_with_no_span_is_outside_the_check_and_is_named(self) -> None:
        """Named even when the rest are enough to run the check, or a report would
        claim a check that covered one fewer session than the reader thinks."""

        rows = self.group(116, 117, 118, 119, 120, 121, None)
        self.assertEqual("", walk_dispute(rows))
        self.assertIn("s6", walk_evidence(rows))
        self.assertIn("outside the walk check", walk_evidence(rows))
        # The control: with every session timed it must say nothing.
        self.assertEqual("", walk_evidence(self.group(116, 117, 118, 119, 120, 121)))

    def test_the_check_and_its_control_share_one_minimum(self) -> None:
        """Asserted structurally, because the two copies of `4` this replaced went
        out of step under mutation and the control reported "fully evidenced" over
        a corpus that could not be read."""

        for count in range(1, 10):
            with self.subTest(count=count):
                rows = self.group(*range(100, 100 + count))
                could_fire = walk_evidence(rows) == ""
                self.assertEqual(
                    could_fire, count >= 6,
                    "the control disagrees with the check about what is readable",
                )

    def test_the_committed_captures_agree_that_they_are_one_walk(self) -> None:
        """The real evidence, and the control that matters most.

        One group survives with its captures: 439's twelve, one script, and none of
        it may dispute. A rule that split it would be measuring the phone rather
        than the driving, and would refuse a corpus that is entirely sound.
        """
        groups = committed_spans()
        self.assertEqual(
            ["439 one-pass-v1", "439 three-round-v2", "440 one-pass-v1", "440 three-round-v2"],
            sorted(groups),
        )
        for name, spans in sorted(groups.items()):
            with self.subTest(group=name):
                self.assertEqual(12, len(spans), name)
                self.assertEqual("", walk_dispute(self.group(*spans)), name)

    def test_one_walk_on_two_builds_pools_without_disputing(self) -> None:
        """The version is not the protocol, and this is the control for that.

        439 ran the one-pass walk in 122-153s and 440 ran the same script in
        109-116s. Pooled they must not dispute: a rule that split them would be
        reading the build. 439's half is real and committed; 440's is the named
        synthetic `ONE_PASS_ON_ANOTHER_BUILD`, because that store was withdrawn —
        the property is worth keeping even though the corpus is gone.
        """
        pooled = committed_spans()["439 one-pass-v1"] + ONE_PASS_ON_ANOTHER_BUILD
        self.assertEqual(24, len(pooled))
        self.assertEqual("", walk_dispute(self.group(*pooled)))

    def test_the_jitter_corpus_is_not_disputed(self) -> None:
        """Anchor one, and the reason `_MIN_SEPARATION` exists.

        `THREE_ROUND_JITTER` — see its comment for where it came from and why it is
        written down rather than read. Both sides of the 271/273 split are zero
        seconds wide, so the derived term reads it as infinitely sharp and on that
        alone `grouping` refused the whole corpus over two seconds in 271.

        The ratio is asserted as well as the verdict. Without it a floor of 90%
        would satisfy the verdict too, and this test is one of the two things
        standing between the constant and a silent retune.
        """
        self.assertEqual([271, 271, 271] + [273] * 9, sorted(THREE_ROUND_JITTER))
        self.assertEqual("", walk_dispute(self.group(*THREE_ROUND_JITTER)))
        self.assertLessEqual(2 / 271, observation._MIN_SEPARATION)

    def test_a_second_walk_filed_under_one_name_is_caught(self) -> None:
        """Anchor two, with the hard half real.

        439's twelve committed spans are the faster group, and they carry a genuine
        31-second internal spread — so the derived term is 31 here, not 0, and a
        rule that only worked on tight groups would pass a tidy fixture and be
        useless on this. The slower group is `THREE_ROUND_JITTER`, which makes this
        the point where the two anchors are checked against each other: 122-153s
        against 271-273s separates by 118s over 153s, which is 77%.
        """
        base = committed_spans()["439 one-pass-v1"]
        self.assertEqual(31, max(base) - min(base),
                         "premise: the faster group must be the noisy one")
        found = walk_dispute(self.group(*base, *THREE_ROUND_JITTER))
        self.assertIn("two groups", found)
        self.assertIn("118s apart", found)
        self.assertIn("31s either group spans on its own", found)

    def test_it_is_caught_when_the_slower_group_is_noisy_too(self) -> None:
        """The twin, with the spread on the other side.

        The same twelve scaled by three — a slower group whose own spread is 93s,
        three times the faster one's. `widest` is then taken from the slower group
        rather than the faster, so this exercises the other branch of that `max`
        and the contamination must still clear it.
        """
        base = committed_spans()["439 one-pass-v1"]
        found = walk_dispute(self.group(*base, *(span * 3 for span in base)))
        self.assertIn("two groups", found)
        self.assertIn("213s apart", found)
        self.assertIn("93s either group spans on its own", found)

    def test_a_shorter_protocol_is_still_caught(self) -> None:
        """The other end of the bracket: three rounds against four.

        271s to 361s is 33%, the smallest protocol change anyone would plausibly
        make, and wholly constructed — nobody has walked four rounds. It is what
        stops `_MIN_SEPARATION` being raised until the check means nothing: this
        fails at any value above a third, and `test_the_jitter_corpus_is_not_disputed`
        fails at any value below 0.74%. Both ends are now constructions, which is
        weaker than it was and is said so in the constant's own comment.
        """
        three = THREE_ROUND_JITTER[:6]
        self.assertEqual(6, len(ONE_MORE_ROUND))
        self.assertIn("two groups", walk_dispute(self.group(*three, *ONE_MORE_ROUND)))
        self.assertGreater((ONE_MORE_ROUND[0] - three[0]) / three[0],
                           observation._MIN_SEPARATION)

    def test_the_floor_is_a_fraction_of_the_faster_group(self) -> None:
        """Which span the fraction is taken of, pinned four ways.

        `_MIN_SEPARATION x (the largest span below the cut)` reads "the slower group
        must run at least a twentieth longer than the faster one". Three other
        denominators are just as plausible to write and all of them pass the two
        anchors, because on those corpora every candidate lands on the same side.

        **The corpora here are derived from the constant rather than written down**,
        so this tests the *shape* of the rule at whatever the fraction happens to
        be, and only the two anchor tests respond to its value. A fixture calibrated
        to 5% would fail for 2% as well — and then moving the constant inside its
        own bracket would break tests that are not about the bracket, which hides
        the two that are. Each case still asserts its premises, because a derived
        fixture can stop discriminating just as quietly as a written one.
        """
        m = observation._MIN_SEPARATION

        # Tight groups. The gap clears a fraction of the faster group and not of the
        # slower, which separates "faster" from "slower" and from "largest in the
        # corpus". `step` is the smallest whole second that can do both.
        fast, step = whole_second_floor(m, math.ceil(1 / m))
        gap = step + 1
        self.assertLess(m * fast, gap, "premise: the gap must clear the faster group")
        self.assertLessEqual(gap, m * (fast + gap), "premise: and not the slower")
        self.assertIn(
            "two groups",
            walk_dispute(self.group(*[fast] * 3, *[fast + gap] * 3)),
        )

        # The faster group spread over `slow - spread .. slow`. The gap clears its
        # range and a fraction of its *smallest* member, but not a fraction of its
        # largest — separating "faster group" from "smallest in the corpus", and
        # from any floor stated in seconds rather than as a fraction.
        slow, reach = whole_second_floor(m, 2 * math.ceil(1 / m))
        spread = reach // 2
        self.assertGreater(reach, spread, "premise: the derived term must pass")
        self.assertLess(m * (slow - spread), reach,
                        "premise: a smallest-span floor would fire")
        self.assertLessEqual(reach, m * slow, "premise: the real floor must not")
        self.assertEqual("", walk_dispute(self.group(
            slow - spread, slow - spread // 2, slow, *[slow + reach] * 3,
        )))

    def test_both_terms_are_strict_and_a_split_that_only_ties_is_not_one(self) -> None:
        """`>` and not `>=`, on each term separately.

        A split exactly as wide as the variation, or exactly a fraction of the walk,
        is the case the rule is least sure about, and the two boundaries are where a
        `>=` would hide. Each corpus sits exactly on one boundary while clearing the
        other, so each pins one inequality — and both are derived from the constant
        for the reason given above.
        """
        m = observation._MIN_SEPARATION

        # Exactly a fraction of the faster group, with both groups zero wide.
        fast, step = whole_second_floor(m, math.ceil(1 / m))
        self.assertEqual(step, m * fast, "premise: the floor must be a whole second")
        self.assertEqual("", walk_dispute(self.group(*[fast] * 3, *[fast + step] * 3)))
        # One second more and the same corpus disputes, so this is a boundary and
        # not a rule that never fires here.
        self.assertIn(
            "two groups",
            walk_dispute(self.group(*[fast] * 3, *[fast + step + 1] * 3)),
        )

        # Exactly as wide as the variation, with the floor already cleared so that
        # only the derived term is deciding.
        base = 100
        tie = int(m * base) + 5
        self.assertGreater(tie, m * base, "premise: the floor must not be deciding")
        self.assertEqual("", walk_dispute(self.group(
            *[base] * 3, base + tie, base + tie, base + 2 * tie,
        )))
        self.assertIn("two groups", walk_dispute(self.group(
            *[base] * 3, base + tie + 1, base + tie + 1, base + 2 * tie,
        )))


class WalkFieldShapeTests(unittest.TestCase):
    """The coercions this field must not do, and the label absence wears.

    `toggles` refuses a raw mapping, `blocks` refuses an int and `span_seconds`
    refuses a float, each with a paragraph about a second spelling of one value.
    The walk is the field `grouping` joins rows on, so it is the last one that may
    quietly accept `123` and store `"123"`.
    """

    def test_a_walk_that_is_not_a_string_is_refused_rather_than_coerced(self) -> None:
        for bad in (123, 1.5, 0, ("one-pass",), ["one-pass"]):
            with self.subTest(walk=bad):
                with self.assertRaises(ObservationError) as caught:
                    session(walk=bad)
                self.assertIn("as a string", str(caught.exception))

    def test_a_hand_edited_numeric_walk_does_not_read_back_as_digits(self) -> None:
        """The store's own reader must refuse what its writer would never make."""

        row = session(walk="123", counts={"/feed/timeline/": 1}).to_dict()
        with self.assertRaises(ObservationError):
            ObservationSession.from_dict({**row, "walk": 123})

    def test_the_label_for_an_unstated_walk_cannot_be_typed_as_one(self) -> None:
        """`UNWALKED` is a label a report prints, never a value a row may hold.

        Spelled with brackets exactly so that `_WALK` forbids it — the trick
        `UNATTRIBUTED` already uses — because a session that named it would join
        the group the reports use to mean "nobody said".
        """
        self.assertFalse(_WALK.fullmatch(UNWALKED))
        with self.assertRaises(ObservationError):
            session(walk=UNWALKED)

    def test_a_report_spells_an_unstated_walk_with_the_label(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = store_path("441", root)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(session("old", walk=None,
                                   counts={"/feed/timeline/": 1}).to_dict(),
                           sort_keys=True) + "\n",
                encoding="utf-8",
            )
            report = summary("441", root)
            # The literal, on at least one side of every comparison. Reading
            # `UNWALKED` on both sides compares the code against itself: emptying
            # the constant satisfies all three, and two of them go vacuous into the
            # bargain, because `assertIn("", anything)` is true.
            self.assertEqual(["(no walk stated)"], report["states"][0]["walks"])
            self.assertIn("(no walk stated)", render(report))
            named = [item for item in report["warnings"] if "on walk(s)" in item]
            self.assertEqual(1, len(named), report["warnings"])
            self.assertIn("on walk(s) (no walk stated)", named[0])
            # And the constant is what produced it, so the two cannot drift apart.
            self.assertEqual("(no walk stated)", UNWALKED)


class WalkReportingTests(RootedTestCase):
    """What the two report forms say about walks, asserted on the strings.

    Every one of these was found by an adversarial pass mutating the *prose* the
    fields are placed into. A test that reads `report["walks"]` and then checks
    `render` for the same substring is satisfied by the field alone, and the
    sentence it was aimed at can be deleted underneath it.
    """

    def test_the_bound_is_stated_in_the_warning_and_not_only_in_the_field(
        self,
    ) -> None:
        append(session("a", walk="one-pass", counts={"/feed/timeline/": 1}),
               root=self.root)
        append(session("b", walk="three-round", counts={"/feed/timeline/": 3}),
               root=self.root)
        bound = [item for item in summary("441", self.root)["warnings"]
                 if "never-observed is bounded" in item]
        self.assertEqual(1, len(bound))
        self.assertIn("on walk(s) one-pass, three-round", bound[0])

    def test_a_vacuous_session_does_not_advertise_a_walk_nobody_can_ask_about(
        self,
    ) -> None:
        """`walks` is the discovery half of `grouping`'s required argument, so a
        walk listed here has to be one that can be asked for. A vacuous session is
        excluded from every answer, and offering its walk would send the reader to
        a refusal."""

        append(session("real", walk="one-pass", counts={"/feed/timeline/": 1}),
               root=self.root)
        append(session("empty", walk="three-round", counts={}), root=self.root)
        self.assertEqual(["one-pass"], summary("441", self.root)["walks"])
        self.assertEqual(("one-pass",), walks("441", self.root))

    def test_the_report_says_when_a_walk_could_not_have_been_contradicted(
        self,
    ) -> None:
        """The positive control, in the report that has no refusal to hang it on.

        On this repository's committed stores it can never fire — not one row
        carries a span — so a report that said nothing would be claiming a check
        it never ran.
        """
        for index in range(3):
            append(session(f"s{index}", walk="one-pass", span_seconds=120,
                           counts={"/feed/timeline/": 1}), root=self.root)
        inert = [item for item in summary("441", self.root)["warnings"]
                 if "only partly evidenced" in item]
        self.assertEqual(1, len(inert), summary("441", self.root)["warnings"])
        self.assertIn("could not have contradicted", inert[0])

    def test_and_it_falls_silent_once_the_check_can_fire(self) -> None:
        """The control for the control: a caution printed over every corpus is not
        a caution."""

        for index in range(6):
            append(session(f"s{index}", walk="one-pass", span_seconds=120 + index,
                           counts={"/feed/timeline/": 1}), root=self.root)
        self.assertEqual(
            [], [item for item in summary("441", self.root)["warnings"]
                 if "only partly evidenced" in item]
        )

    def test_the_record_time_dispute_is_asked_per_walk_and_not_over_the_pool(
        self,
    ) -> None:
        """The same rule `summary` follows, and it is written in two places.

        Pooling two walks and then asking whether the spans split finds the split
        every time — so a session filed under the second walk, beside a first walk
        that is nowhere near it, must record silently.
        """
        for index, span in enumerate((120, 121, 122)):
            append(session(f"short{index}", walk="one-pass", span_seconds=span,
                           counts={"/feed/timeline/": 1}), root=self.root)
        for index, span in enumerate((360, 361)):
            append(session(f"long{index}", walk="three-round", span_seconds=span,
                           counts={"/feed/timeline/": 1}), root=self.root)
        capture = self.root / "c.log"
        capture.write_text(
            line(f"{TOGGLE_DIRECTIVE} {ALL_OFF.text}", stamp="08-08 17:00:00.000 1 1")
            + line("/feed/timeline/", stamp="08-08 17:06:02.000 1 1"),
            encoding="utf-8",
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main([
                "--root", str(self.root), "record", "--version", "441",
                "--build-sha256", BUILD, "--recorded-at", "2026-08-09T10:00:00Z",
                "--session-id", "long2", "--surface", "feed_tab",
                "--walk", "three-round",
                "--watched", "/feed/timeline/", "--capture", str(capture),
            ])
        self.assertEqual(0, code)
        self.assertNotIn("WALK DISPUTED", out.getvalue())


class WalkDisputePrecisionTests(unittest.TestCase):
    """It must not refuse the corpus this repository actually holds.

    A check that cries wolf gets turned off — that is a recorded lesson here, from
    a leak scan whose hex and IPv4 patterns flagged every fixture on legitimate
    content. An adversarial pass measured this one doing the same thing: with two
    sessions a side, **66 of the 495 four-session subsets of the committed 439
    corpus were refused**, all of them one walk on one build, and among them the
    smallest corpus that yields a finding at all.

    So the property pinned here is not "it catches contamination" — that is next
    door — but "it refuses nothing in the real evidence, at any size". It is
    asserted over every subset rather than over the twelve, because the twelve
    passing was true before the fix as well.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.groups = committed_spans()

    def rows(self, spans):
        return tuple(
            session(f"s{index}", walk="one-pass", span_seconds=span,
                    counts={"/feed/timeline/": 1})
            for index, span in enumerate(spans)
        )

    def test_no_subset_of_any_committed_corpus_is_refused(self) -> None:
        from itertools import combinations  # noqa: PLC0415

        for name, spans in sorted(self.groups.items()):
            self.assertEqual(12, len(spans), name)
            for size in range(2, 13):
                refused = [
                    subset for subset in combinations(spans, size)
                    if walk_dispute(self.rows(subset))
                ]
                self.assertEqual(
                    [], refused,
                    f"{name}: {len(refused)} honest {size}-session subset(s) refused",
                )

    def test_the_smallest_corpus_that_yields_a_finding_is_not_refused(self) -> None:
        """A baseline pair and one arm pair, drawn from the real spans that used to
        break it: 153 and 122 for the all-off state, 141 and 122 for the adds arm.
        The first walk of a new version is slow, and that is not a protocol."""

        self.assertEqual("", walk_dispute(self.rows((153, 122, 141, 122))))

    def test_it_still_catches_a_second_walk_from_three_sessions_onward(self) -> None:
        """The positive twin, and the cost of the minimum stated as a test.

        Three sessions of the second walk are caught; two are not, and that is the
        trade `_MIN_PER_SIDE` bought. It is the safe direction — the field carries
        the residual either way, and a check an operator has learned to ignore
        carries nothing. The faster group is 439's real twelve; the slower is
        `THREE_ROUND_JITTER`, whose provenance is on the constant itself.
        """
        base = self.groups["439 one-pass-v1"]
        other = THREE_ROUND_JITTER
        for count in (3, 4, 6, 12):
            with self.subTest(count=count):
                self.assertIn("two groups",
                              walk_dispute(self.rows([*base, *other[:count]])))
        self.assertEqual("", walk_dispute(self.rows([*base, *other[:2]])))

    def test_a_one_shot_iterator_is_refused_by_both_of_the_pair(self) -> None:
        """The hazard is at the call site, so that is where it has to be refused.

        These two are asked about the *same* sessions. Whichever reads a generator
        first empties it, and the second then answers from nothing — `walk_evidence`
        returning `""`, which is its way of saying "this could have fired and found
        nothing", about a corpus it never saw. Materialising inside either function
        does not help and reads as though it does: `tuple(sessions)` drains the
        caller's generator exactly as iterating it would.

        Asserted as the **observable**, not as the presence of a `tuple()`: one
        iterator, used twice, and the second answer must not be a quiet "nothing is
        wrong".
        """
        rows = self.rows((116, 117, 118, 350, 351, 352))

        # The failure this prevents, demonstrated on a plain list first so the
        # expected answers are on record.
        self.assertIn("two groups", walk_dispute(rows))
        self.assertEqual("", walk_evidence(rows))

        for function in (walk_dispute, walk_evidence):
            with self.subTest(function=function.__name__):
                stream = (item for item in rows)
                with self.assertRaises(ObservationError) as caught:
                    function(stream)
                self.assertIn("one-shot iterator", str(caught.exception))
                self.assertIn(function.__name__, str(caught.exception))
                # Refused before anything was read, so the caller still has its
                # sessions and can hand them over as a list.
                self.assertEqual(6, len(list(stream)))

    def test_walk_evidence_reads_its_input_twice(self) -> None:
        """Which is why the refusal above covers it too.

        Its `timed` and `untimed` comprehensions are two passes. Over a drained
        iterator `untimed` comes out empty, and a corpus holding sessions the check
        cannot see reports itself as fully evidenced — the exact absence-as-a-pass
        this function exists to prevent, arriving through the function.
        """
        rows = (*self.rows((116, 117, 118, 119, 120, 121)),
                session("blind", walk="one-pass", counts={"/feed/timeline/": 1}))
        found = walk_evidence(rows)
        self.assertIn("blind", found)
        self.assertIn("outside the walk check", found)
        with self.assertRaises(ObservationError):
            walk_evidence(item for item in rows)


def _capture(*payloads: str, state: str = "disable_feed=1 disable_reels=0",
             reports: str = f"{REPORTS_MARK}{REPORTS_BLOCKED}") -> str:
    """A capture in the real threadtime shape, one line per payload.

    The toggle line is restated ahead of every payload, which is what the build
    does: `state()` runs on every checked request.
    """
    marked = f"{TOGGLE_DIRECTIVE} {reports} {state}".replace("  ", " ").strip()
    lines = []
    for index, payload in enumerate(payloads):
        stamp = f"08-13 10:00:{index % 60:02d}.100"
        lines.append(f"{stamp} 11944 12198 I {TAG}: {marked}")
        lines.append(f"{stamp} 11944 12198 I {TAG}: {payload}")
    return "\n".join(lines) + "\n"


class RefusalsFromTheGuardItselfTests(unittest.TestCase):
    """The block signal is ours now, and its absence has to stay an absence.

    Until 2026-08-13 a refusal was counted from `IgFunctionalErrorEvent`, which
    Instagram emits at its own discretion for an event our own `if` generates. It
    under-reports by feature: `/discover/topical_explore` was refused 7, 6, 12 and
    6 times across eight sessions and reported 1, 0, 1 and 0, while
    `/feed/timeline/` reported 20/20 and 23/23 in the very same captures.
    """

    def test_a_refusal_is_counted_under_the_literal_the_guard_named(self):
        capture = parse(_capture(
            "/feed/timeline/",
            f"{BLOCKED_DIRECTIVE} /feed/timeline/",
            "/feed/timeline/",
            f"{BLOCKED_DIRECTIVE} /feed/timeline/",
            "/feed/reels_tray/",
        ))
        self.assertEqual({"/feed/timeline/": 2, "/feed/reels_tray/": 1}, capture.counts)
        self.assertEqual({"/feed/timeline/": 2}, capture.refusals.as_dict())
        self.assertEqual(2, capture.refusals.total)
        self.assertEqual(2, capture.refusals.get("/feed/timeline/"))
        self.assertEqual(0, capture.refusals.get("/feed/reels_tray/"))

    def test_a_build_that_never_claimed_the_capability_reports_nothing_not_zero(self):
        """The distinction the whole change rests on.

        A capture from an older build holds no refusal lines, and so does a
        capture from a new build under a state that refused nothing. Reading the
        first as the second would turn 48 committed sessions into 48 measured
        zeroes — and `grouping` calls a toggle a blocking one by comparing an arm
        against a baseline of zero, so those rows would become the control.
        """
        older = parse(_capture("/feed/timeline/", reports=""))
        self.assertIsNone(older.refusals)
        current = parse(_capture("/feed/timeline/"))
        self.assertIsNotNone(current.refusals)
        self.assertEqual({}, current.refusals.as_dict())
        self.assertEqual(0, current.refusals.total)
        self.assertNotEqual(older.refusals, current.refusals)

    def test_a_refusal_before_any_toggle_line_is_refused(self):
        text = (
            f"08-13 10:00:00.100 1 2 I {TAG}: {BLOCKED_DIRECTIVE} /feed/timeline/\n"
            f"08-13 10:00:01.100 1 2 I {TAG}: {TOGGLE_DIRECTIVE} "
            f"{REPORTS_MARK}{REPORTS_BLOCKED} disable_feed=1\n"
        )
        with self.assertRaises(ObservationError) as caught:
            parse(text)
        self.assertIn("before any", str(caught.exception))

    def test_a_refusal_the_build_never_said_it_could_report_is_refused(self):
        """The guard and the class it logs through disagreeing is not a small thing.

        It means the reader's model of the build is wrong, so the refusals present
        cannot be trusted to be all of them — which is worse than none, because
        they look like a complete count.
        """
        with self.assertRaises(ObservationError) as caught:
            parse(_capture(f"{BLOCKED_DIRECTIVE} /feed/timeline/", reports=""))
        self.assertIn(f"{REPORTS_MARK}{REPORTS_BLOCKED}", str(caught.exception))

    def test_a_capability_this_host_does_not_know_is_refused(self):
        """Fails closed, like an unknown directive: the build is newer than the reader."""
        with self.assertRaises(ObservationError) as caught:
            parse(_capture("/feed/timeline/", reports=f"{REPORTS_MARK}wavelength"))
        self.assertIn("wavelength", str(caught.exception))

    def test_two_different_capability_sets_in_one_capture_are_refused(self):
        """Two builds' logs concatenated: one half could report and the other could not."""
        text = _capture("/feed/timeline/") + _capture("/feed/timeline/", reports="")
        with self.assertRaises(ObservationError) as caught:
            parse(text)
        self.assertIn("capabilities", str(caught.exception))

    def test_a_refusal_naming_nothing_or_a_padded_name_is_refused(self):
        for payload in (BLOCKED_DIRECTIVE, f"{BLOCKED_DIRECTIVE}  /feed/timeline/"):
            with self.subTest(payload=payload):
                with self.assertRaises(ObservationError):
                    parse(_capture(payload))

    def test_the_capability_mark_does_not_disturb_the_toggle_state(self):
        """The two token shapes share a line and neither reader sees the other's."""
        with_mark = parse(_capture("/feed/timeline/"))
        without = parse(_capture("/feed/timeline/", reports=""))
        self.assertEqual(without.toggles, with_mark.toggles)
        self.assertEqual("disable_feed=1 disable_reels=0", with_mark.toggles.text)

    def test_a_refusal_line_is_never_counted_as_a_request(self):
        """It begins with `!`, so it is a directive — and requests are what zeroes are read from."""
        capture = parse(_capture(f"{BLOCKED_DIRECTIVE} /feed/timeline/"))
        self.assertEqual({}, capture.counts)
        self.assertEqual({"/feed/timeline/": 1}, capture.refusals.as_dict())

    def test_a_recorded_zero_is_refused_the_way_every_other_count_is(self):
        with self.assertRaises(ObservationError) as caught:
            Refusals.of({"/feed/timeline/": 0})
        self.assertIn("second spelling of absent", str(caught.exception))

    def test_a_capture_built_by_hand_says_nothing_rather_than_nothing_happened(self):
        """The one place the absent-versus-empty rule is unguarded.

        `parse` always sets `refusals` explicitly, so the field's default is
        reached only by a caller constructing a `Capture` — a fixture, a test, a
        future producer. Defaulting it to an empty `Refusals` would make every one
        of those say "this configuration refused nothing", which is the measured
        statement the whole store is compared against.
        """
        from dfinsta_pipeline.observation import Capture

        self.assertIsNone(Capture(toggles=None).refusals)
        self.assertIsNone(Capture(toggles=ToggleState.of({"disable_feed": True})).refusals)

    def test_the_erasure_corroboration_does_not_claim_what_it_cannot_say(self):
        """Grammar is the tell, and the sentence was asserting corroboration.

        With a build that could not report, `refusals_text` returns "not reported"
        and it was being spliced into "refused it {…} time(s): an erased path
        cannot be refused" — printed twice, verbatim, by
        `grouping report --version 439`, over the entire committed corpus.
        """
        from dfinsta_pipeline import grouping

        report = grouping.summary("439", REPOSITORY, walk="one-pass-v1")
        printed = grouping.render(report)
        self.assertNotIn("refused it not reported time(s)", printed)
        self.assertIn("could not report refusals", printed)

    def test_a_blank_or_padded_literal_is_refused(self):
        for literal in ("", "  ", " /feed/timeline/"):
            with self.subTest(literal=literal):
                with self.assertRaises(ObservationError):
                    Refusals.of({literal: 1})


class SessionRefusalsTests(RootedTestCase):
    """The store keeps the distinction the parser makes."""

    def test_absence_is_the_key_being_missing_and_never_a_null(self):
        older = session(refusals=None)
        self.assertNotIn("refusals", older.to_dict())
        measured = session(
            refusals=Refusals.of({"/feed/timeline/": 2}), counts={"/feed/timeline/": 3}
        )
        self.assertEqual({"/feed/timeline/": 2}, measured.to_dict()["refusals"])
        self.assertEqual(measured, ObservationSession.from_dict(measured.to_dict()))
        self.assertEqual(older, ObservationSession.from_dict(older.to_dict()))

    def test_an_empty_object_is_a_measurement_and_survives_the_round_trip(self):
        nothing = session(refusals=Refusals.of({}))
        self.assertEqual({}, nothing.to_dict()["refusals"])
        back = ObservationSession.from_dict(nothing.to_dict())
        self.assertIsNotNone(back.refusals)
        self.assertEqual(nothing, back)

    def test_a_null_is_refused(self):
        row = session(refusals=Refusals.of({})).to_dict()
        row["refusals"] = None
        with self.assertRaises(ObservationError) as caught:
            ObservationSession.from_dict(row)
        self.assertIn("second spelling of absent", str(caught.exception))

    def test_a_refusal_outside_the_watch_list_is_refused(self):
        with self.assertRaises(ObservationError) as caught:
            session(refusals=Refusals.of({"/nowhere/": 1}))
        self.assertIn("was not watching", str(caught.exception))

    def test_more_refusals_than_requests_is_refused(self):
        """The two halves of a capture checking each other.

        The observe pass tests every watched literal with `contains` at the top of
        `throwIfBlocked`, so a request the guard refuses under a literal was
        reported under that same literal a few instructions earlier. More
        refusals than observations therefore cannot happen in one build.
        """
        with self.assertRaises(ObservationError) as caught:
            session(
                refusals=Refusals.of({"/feed/timeline/": 4}),
                counts={"/feed/timeline/": 3},
            )
        self.assertIn("more often than it observed", str(caught.exception))
        # And equal is fine: every request to a blocked path is refused.
        session(
            refusals=Refusals.of({"/feed/timeline/": 3}), counts={"/feed/timeline/": 3}
        )

    def test_a_raw_mapping_is_refused_rather_than_coerced(self):
        with self.assertRaises(ObservationError) as caught:
            session(refusals={"/feed/timeline/": 1})
        self.assertIn("pass a Refusals", str(caught.exception))

    def test_recording_a_capture_carries_the_refusals_through(self):
        capture = _capture(
            "/feed/timeline/", f"{BLOCKED_DIRECTIVE} /feed/timeline/", state="disable_feed=1"
        )
        path = Path(self.root) / "capture.log"
        path.write_text(capture, encoding="utf-8")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main([
                "--root", str(self.root), "record", "--version", "439",
                "--build-sha256", BUILD, "--recorded-at", "2026-08-13T10:00:00Z",
                "--session-id", "439-x", "--surface", "feed_tab", "--walk", WALK,
                "--watched", "/feed/timeline/", "--capture", str(path),
            ])
        self.assertEqual(0, code)
        self.assertIn("refused:", buffer.getvalue())
        rows = read("439", self.root)
        self.assertEqual({"/feed/timeline/": 1}, rows[0].refusals.as_dict())

    def test_a_capture_that_could_not_report_says_so_when_recorded(self):
        """Told at the moment it is filed, because nothing can back-fill it.

        Unlike the walk — which the captures could not state and the operator
        could — a refusal count cannot be reconstructed from a capture written by
        a build that never wrote one. The repair is another walk.
        """
        path = Path(self.root) / "old.log"
        path.write_text(_capture("/feed/timeline/", reports=""), encoding="utf-8")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            main([
                "--root", str(self.root), "record", "--version", "439",
                "--build-sha256", BUILD, "--recorded-at", "2026-08-13T10:00:00Z",
                "--session-id", "439-old", "--surface", "feed_tab", "--walk", WALK,
                "--watched", "/feed/timeline/", "--capture", str(path),
            ])
        self.assertIn("not reportable", buffer.getvalue())
        self.assertIsNone(read("439", self.root)[0].refusals)


class TheContractIsOneContractTests(unittest.TestCase):
    """The generator and the reader are separate modules and must not drift.

    `guards` writes these strings into smali and `observation` reads them out of
    logcat. Nothing at runtime connects the two, so a rename on one side would
    make every future capture parse to "this build reports nothing" — silently,
    and in the direction that reads as evidence.
    """

    def test_the_generator_and_the_reader_spell_the_signal_the_same_way(self):
        from dfinsta_pipeline import guards

        self.assertEqual(guards.OBSERVE_TAG, TAG)
        self.assertEqual(guards.TOGGLE_DIRECTIVE, TOGGLE_DIRECTIVE)
        self.assertEqual(guards.BLOCKED_DIRECTIVE, BLOCKED_DIRECTIVE)
        self.assertEqual(guards.REPORTS_MARK, REPORTS_MARK)
        self.assertEqual(set(guards.REPORTS), set(KNOWN_REPORTS))
        self.assertIn(REPORTS_BLOCKED, guards.REPORTS)

    def test_the_line_the_generator_emits_is_the_line_the_reader_parses(self):
        """End to end over the real emission, not over two copies of one constant."""
        from dfinsta_pipeline.guards import REPORTS_LINE

        payload = f"{REPORTS_LINE} disable_feed=1"
        capture = parse(f"08-13 10:00:00.100 1 2 I {TAG}: {payload}\n"
                        f"08-13 10:00:00.200 1 2 I {TAG}: /feed/timeline/\n")
        self.assertEqual("disable_feed=1", capture.toggles.text)
        self.assertIsNotNone(capture.refusals)


class TheCommittedCorpusPredatesTheSignalTests(unittest.TestCase):
    """Every capture in `manifest/captures/` was taken before the guard could speak.

    Recorded as a fact rather than assumed: this is what makes the block half of
    `grouping` unanswerable for 439 until it is re-walked, and a future reader
    finding these tests should be able to see that the refusal was measured and
    not merely declared.
    """

    def test_no_committed_capture_can_report_refusals(self):
        captures = sorted((Path(__file__).resolve().parent.parent / "manifest" / "captures").glob("*.log"))
        if not captures:
            self.skipTest("no committed captures")
        for capture in captures:
            with self.subTest(capture=capture.name):
                self.assertIsNone(parse(capture.read_text(encoding="utf-8")).refusals)


if __name__ == "__main__":
    unittest.main()

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

**Nothing here writes into `manifest/observations/`.** Every root is a temporary
directory passed explicitly. A test in this repository once wrote into a
committed corpus and shipped 36 fabricated rows, and the defence that actually
holds is that no test ever names the real root.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from dfinsta_pipeline.observation import (
    OBSERVATIONS,
    SCHEMA_VERSION,
    TAG,
    ObservationError,
    ObservationSession,
    append,
    blocked_and_never_observed,
    blocked_endpoints,
    evidential,
    main,
    never_observed,
    parse,
    read,
    render,
    store_path,
    summary,
)

REPOSITORY = Path(__file__).resolve().parent.parent

BUILD = "b" * 64


def session(
    session_id: str = "s1",
    *,
    version: str = "441",
    surface: str = "feed_tab",
    watched: tuple[str, ...] = ("/feed/timeline/", "/feed/reels_tray/"),
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
        capture = (
            "08-08 17:31:02.412 12875 12875 I DFInstaObserve: /feed/timeline/\n"
            "08-08 17:31:02.900 12875 12875 I DFInstaObserve: /feed/timeline/\n"
            "08-08 17:31:03.100 12875 12875 I DFInstaObserve: /feed/reels_tray/\n"
        )
        self.assertEqual(
            {"/feed/timeline/": 2, "/feed/reels_tray/": 1}, parse(capture)
        )

    def test_the_bare_form_the_app_emits_is_counted(self) -> None:
        """`Log.i(TAG, literal)` is what the contract fixes; the prefix is logcat's."""
        self.assertEqual({"/feed/x/": 2}, parse("I DFInstaObserve: /feed/x/\n" * 2))

    def test_lines_from_other_tags_are_ignored(self) -> None:
        capture = (
            "08-08 17:31:02.412 12875 12875 D SomeOtherTag: /feed/timeline/\n"
            "08-08 17:31:02.500 12875 12875 I DFInstaProbe: tigon_url_block\n"
            "--------- beginning of main\n"
        )
        self.assertEqual({}, parse(capture))

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
        self.assertEqual({}, parse(quoted))

    def test_the_same_literal_in_tag_position_is_counted(self) -> None:
        """The positive control for the test above.

        Without it, a regex that matched nothing at all would pass that test.
        """
        real = "08-08 17:31:02.412 12875 12875 I DFInstaObserve: /feed/timeline/\n"
        self.assertEqual({"/feed/timeline/": 1}, parse(real))

    def test_an_empty_capture_counts_nothing_rather_than_refusing(self) -> None:
        """A capture with no lines is a vacuous session, decided later, not here."""
        self.assertEqual({}, parse(""))
        self.assertEqual({}, parse("\n\n"))

    def test_a_crlf_capture_reads_the_same(self) -> None:
        """`adb` on Windows. A stray `\\r` would make the literal unmatchable."""
        self.assertEqual(
            {"/feed/x/": 1},
            parse("08-08 17:31:02.412 1 1 I DFInstaObserve: /feed/x/\r\n"),
        )

    def test_a_tag_line_with_no_literal_is_refused_by_line(self) -> None:
        """Dropping it would subtract a request that did happen from a count whose
        only purpose is being compared with zero."""
        with self.assertRaises(ObservationError) as caught:
            parse("I DFInstaObserve: /feed/x/\nI DFInstaObserve: \n")
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
                watched=("/a/",), counts={},
            )
        self.assertIn("SHA-256", str(caught.exception))
        for field in ("recorded_at", "session_id", "surface"):
            with self.subTest(field=field):
                arguments = {
                    "schema_version": 1, "version": "441", "build_sha256": BUILD,
                    "recorded_at": "2026-08-09T10:00:00Z", "session_id": "s",
                    "surface": "f", "watched": ("/a/",), "counts": {},
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
                        watched=("/a/",), counts={},
                    )
                self.assertIn(stamp, str(caught.exception))

    def test_a_naive_timestamp_is_refused(self) -> None:
        """It cannot be ordered against one written on another machine, and two
        sessions being orderable is what makes them comparable."""
        with self.assertRaises(ObservationError) as caught:
            ObservationSession(
                schema_version=1, version="441", build_sha256=BUILD,
                recorded_at="2026-08-09T10:00:00", session_id="s", surface="f",
                watched=("/a/",), counts={},
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
                    watched=("/a/",), counts={},
                )
                self.assertEqual(stamp, item.recorded_at)

    def test_a_padded_timestamp_is_stored_stripped(self) -> None:
        """Validating the stripped value and recording the padded one wrote a form
        the next read refuses — the same defect, one module over."""
        item = ObservationSession(
            schema_version=1, version="441", build_sha256=BUILD,
            recorded_at="  2026-08-09T10:00:00+00:00  ", session_id="s", surface="f",
            watched=("/a/",), counts={},
        )
        self.assertEqual("2026-08-09T10:00:00+00:00", item.recorded_at)
        self.assertEqual("2026-08-09T10:00:00+00:00", item.to_dict()["recorded_at"])

    def test_a_watch_list_given_as_a_list_is_stored_as_a_tuple(self) -> None:
        """Two sessions with the same watch list must compare equal however the
        caller spelled it, or a duplicate check silently stops matching."""
        item = ObservationSession(
            schema_version=1, version="441", build_sha256=BUILD,
            recorded_at="2026-08-09T10:00:00Z", session_id="s", surface="f",
            watched=["/a/", "/b/"], counts={},
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
        here = never_observed("441", self.root)

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

        self.assertEqual(here, never_observed("441", self.root))
        self.assertEqual(("/feed/reels_tray/",), here)


# ===========================================================================
#   never_observed — the refusal this module exists for
# ===========================================================================


class NeverObservedTests(RootedTestCase):
    def test_a_watched_path_no_session_saw_is_reported(self) -> None:
        append(session("s1", counts={"/feed/timeline/": 7}), root=self.root)
        self.assertEqual(("/feed/reels_tray/",), never_observed("441", self.root))

    def test_a_path_seen_in_any_session_is_not_reported(self) -> None:
        """The union across sessions, not the intersection. A path the feed
        session never saw and the explore session did was observed."""
        append(session("s1", counts={"/feed/timeline/": 7}), root=self.root)
        append(
            session("s2", surface="explore_tab", counts={"/feed/reels_tray/": 1}),
            root=self.root,
        )
        self.assertEqual((), never_observed("441", self.root))

    def test_everything_seen_gives_an_empty_tuple(self) -> None:
        """The positive control for every refusal below.

        `()` has to be a real answer that a real corpus can produce, or the
        refusals prove nothing — they would just be the only branch anyone reaches.
        """
        append(
            session("s1", counts={"/feed/timeline/": 1, "/feed/reels_tray/": 1}),
            root=self.root,
        )
        self.assertEqual((), never_observed("441", self.root))

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
        self.assertEqual(("/feed/reels_tray/",), never_observed("441", self.root))

    def test_one_evidential_session_is_enough(self) -> None:
        """Deliberately no minimum-N. Two vacuous sessions do not weaken the one
        that observed something, and no constant decides how many are enough —
        `derive-the-threshold-never-declare-it`."""
        append(session("v1", counts={}), root=self.root)
        append(session("s1", counts={"/feed/timeline/": 1}), root=self.root)
        append(session("v2", counts={}), root=self.root)
        self.assertEqual(("/feed/reels_tray/",), never_observed("441", self.root))

    def test_a_store_with_no_sessions_refuses(self) -> None:
        with self.assertRaises(ObservationError) as caught:
            never_observed("441", self.root)
        self.assertIn("no observation evidence", str(caught.exception))

    def test_a_store_whose_sessions_are_all_vacuous_refuses(self) -> None:
        """Not `()`. That is the same answer it gives when every watched path was
        seen, so returning it would report "we measured nothing" in the words of
        "nothing is wrong"."""
        append(session("v1", counts={}), root=self.root)
        append(session("v2", surface="explore_tab", counts={}), root=self.root)
        with self.assertRaises(ObservationError) as caught:
            never_observed("441", self.root)
        self.assertIn("vacuous", str(caught.exception))

    def test_the_two_refusals_say_different_things(self) -> None:
        """"Nothing was recorded" and "everything recorded saw nothing" are
        different problems with different fixes, and a human reads the message."""
        empty = self.assertRaisesRegex(ObservationError, "no observation evidence")
        with empty:
            never_observed("441", self.root)
        append(session("v1", counts={}), root=self.root)
        with self.assertRaises(ObservationError) as caught:
            never_observed("441", self.root)
        self.assertNotIn("holds no session", str(caught.exception))

    def test_an_unreadable_store_refuses_rather_than_reporting_nothing(self) -> None:
        self.store().parent.mkdir(parents=True, exist_ok=True)
        self.store().write_text("{ not json\n", encoding="utf-8")
        with self.assertRaises(ObservationError):
            never_observed("441", self.root)

    def test_evidential_keeps_exactly_the_sessions_that_saw_something(self) -> None:
        sessions = (session("v", counts={}), session("s", counts={"/feed/timeline/": 1}))
        self.assertEqual(("s",), tuple(i.session_id for i in evidential(sessions)))


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
            ("/feed/reels_tray/",), blocked_and_never_observed("441", self.root)
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

        self.assertIn("/feed/reels_tray/", never_observed("441", self.root))
        self.assertEqual((), blocked_and_never_observed("441", self.root))

    def test_a_blocked_path_that_was_requested_is_not_reported(self) -> None:
        """The positive control: `()` must be reachable from a real corpus."""
        self.manifest("/feed/timeline/", "/feed/reels_tray/")
        append(
            session("s1", counts={"/feed/timeline/": 1, "/feed/reels_tray/": 2}),
            root=self.root,
        )

        self.assertEqual((), blocked_and_never_observed("441", self.root))

    def test_a_blocked_path_nobody_watched_is_absent_rather_than_reported(self) -> None:
        """Silence about it is about the watch list, not about the app.

        Absent from the answer *and* named in the report's warnings — see
        :class:`ReportTests`. An endpoint no build was looking for produces
        exactly the same zero as one the app never asks for.
        """
        self.manifest("/feed/timeline/", "/never/watched/")
        append(session("s1", counts={"/feed/timeline/": 7}), root=self.root)

        self.assertNotIn("/never/watched/", blocked_and_never_observed("441", self.root))

    def test_it_refuses_when_no_session_is_evidence_rather_than_answering_empty(self):
        """Inherited from `never_observed`, and deliberately not softened.

        `()` is what this returns when every blocked path was seen, so returning
        it here would report "we measured nothing" in the words of "nothing is
        wrong". The refusal must survive the intersection.
        """
        self.manifest("/feed/timeline/")
        append(session("s1", counts={}), root=self.root)

        with self.assertRaises(ObservationError) as caught:
            blocked_and_never_observed("441", self.root)
        self.assertIn("vacuous", str(caught.exception))

    def test_no_evidence_at_all_refuses_too(self) -> None:
        self.manifest("/feed/timeline/")
        with self.assertRaises(ObservationError) as caught:
            blocked_and_never_observed("441", self.root)
        self.assertIn("no observation evidence", str(caught.exception))

    def test_an_unreadable_manifest_refuses_through_this_modules_error(self) -> None:
        """One refusal channel. A `GuardError` escaping would miss every handler."""
        append(session("s1", counts={"/feed/timeline/": 1}), root=self.root)

        with self.assertRaises(ObservationError):
            blocked_and_never_observed("441", self.root)

        path = self.root / "manifest" / "hooks.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ObservationError) as caught:
            blocked_and_never_observed("441", self.root)
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
        self.assertEqual((), blocked_and_never_observed("441", self.root))

    def test_the_answer_is_scoped_to_the_root_it_was_given(self) -> None:
        """Both halves — the store and the manifest — or `--root` is half-scoped."""
        self.manifest("/feed/timeline/", "/feed/reels_tray/")
        append(session("s1", counts={"/feed/timeline/": 7}), root=self.root)

        previous = os.getcwd()
        os.chdir(REPOSITORY)
        self.addCleanup(os.chdir, previous)

        self.assertEqual(
            ("/feed/reels_tray/",), blocked_and_never_observed("441", self.root)
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

    def test_the_report_names_what_was_never_observed(self) -> None:
        self.corpus()
        report = summary("441", self.root)
        self.assertEqual(["/feed/reels_tray/"], report["never_observed"])
        self.assertEqual({"/feed/timeline/": 12}, report["observed"])
        self.assertEqual(2, report["session_count"])
        self.assertEqual(1, report["evidential_session_count"])
        self.assertEqual(["v1"], report["vacuous_session_ids"])

    def test_the_report_carries_the_blocked_answer_and_its_own_refusal(self) -> None:
        """A second field with a second refusal string, not a reuse of the first.

        This can fail where `never_observed` succeeds — an unreadable manifest, or
        one declaring no block — and a reader told "all sessions are vacuous" when
        the real fault is a missing `url_block_rules` repairs the wrong thing.
        """
        self.corpus()

        without = summary("441", self.root)

        self.assertEqual([], without["blocked_never_observed"])
        self.assertIn("hooks.json", without["blocked_never_observed_refused"])
        # The control: the sibling field answered on the same corpus, so the
        # refusal above is about the manifest and not about the evidence.
        self.assertEqual(["/feed/reels_tray/"], without["never_observed"])
        self.assertEqual("", without["never_observed_refused"])

        BlockedAndNeverObservedTests.manifest(self, "/feed/reels_tray/")
        with_manifest = summary("441", self.root)

        self.assertEqual(["/feed/reels_tray/"], with_manifest["blocked_never_observed"])
        self.assertEqual("", with_manifest["blocked_never_observed_refused"])

    def test_a_blocked_endpoint_nobody_watched_is_named_in_the_warnings(self) -> None:
        """Otherwise the answer above is quietly incomplete.

        A blocked path absent from every watch list produces the same zero as one
        the app never requests, and it is excluded from the finding. A reader
        asking "is that all of them?" has to be told.
        """
        self.corpus()
        BlockedAndNeverObservedTests.manifest(self, "/feed/reels_tray/", "/never/watched/")

        report = summary("441", self.root)

        self.assertEqual(["/feed/reels_tray/"], report["blocked_never_observed"])
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
        "Every watched path was observed at least once.",
        "WATCHED AND NEVER OBSERVED: refused",
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

        Run over all three corpora, because the path that most needs to be loud is
        the one where nothing was measured.
        """
        for label, build in (
            ("populated", self.corpus),
            ("all vacuous", lambda: append(session("v1", counts={}), root=self.root)),
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

                carried = {report["version"], report["never_observed_refused"]}
                carried |= set(report["warnings"]) | set(report["never_observed"])
                carried |= set(report["observed"]) | set(report["surfaces"])
                carried |= set(report["vacuous_session_ids"])
                carried |= {str(report[key]) for key in
                            ("session_count", "evidential_session_count")}
                carried |= {str(value) for value in report["observed"].values()}
                carried.discard("")

                for line in text.splitlines():
                    stripped = line.strip()
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
        self.assertIn("no observation evidence", report["never_observed_refused"])
        self.assertTrue(any("no observation evidence" in w for w in report["warnings"]))

        append(session("v1", counts={}), root=self.root)
        report = summary("441", self.root)
        self.assertIn("vacuous", report["never_observed_refused"])
        self.assertTrue(any("vacuous" in w for w in report["warnings"]))

        append(session("s1", counts={"/feed/timeline/": 3}), root=self.root)
        report = summary("441", self.root)
        self.assertEqual("", report["never_observed_refused"])
        self.assertEqual(["/feed/reels_tray/"], report["never_observed"])
        self.assertTrue(any("vacuous" in w for w in report["warnings"]))

    def test_a_vacuous_session_is_reported_in_both_forms(self) -> None:
        self.corpus()
        report = summary("441", self.root)
        self.assertTrue(any("vacuous" in line for line in report["warnings"]))
        self.assertIn("v1", "\n".join(report["warnings"]))
        self.assertIn("v1", render(report))

    def test_the_surfaces_bound_is_stated_on_every_report_that_has_an_answer(self) -> None:
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
        self.assertEqual(["feed_tab"], report["surfaces"])
        self.assertIn("feed_tab", render(report))
        self.assertNotIn("reels_tab", render(report))
        bound = next(w for w in report["warnings"] if "bounded by the surfaces" in w)
        self.assertNotIn("reels_tab", bound)

    def test_a_refusal_reaches_both_forms_and_neither_reads_as_clean(self) -> None:
        """The dangerous shape: nothing to report because nothing was measured."""
        append(session("v1", counts={}), root=self.root)
        report = summary("441", self.root)
        self.assertEqual([], report["never_observed"])
        self.assertIn("vacuous", report["never_observed_refused"])

        _, text, _ = self.run_main(["--root", str(self.root), "report", "--version", "441"])
        self.assertIn("refused", text)
        self.assertNotIn("Every watched path was observed", text)
        _, as_json, _ = self.run_main(
            ["--root", str(self.root), "report", "--version", "441", "--json"]
        )
        self.assertIn("vacuous", json.loads(as_json)["never_observed_refused"])

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
        self.assertEqual([], report["never_observed"])
        self.assertEqual("", report["never_observed_refused"])
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
            "--capture", str(capture),
            *extra,
        ]

    def test_a_capture_becomes_a_recorded_session(self) -> None:
        capture = self.capture(
            "08-08 17:31:02.412 1 1 I DFInstaObserve: /feed/timeline/\n"
            "08-08 17:31:03.412 1 1 I DFInstaObserve: /feed/timeline/\n"
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
        self.assertEqual(("/feed/reels_tray/",), never_observed("441", self.root))

    def test_a_watch_list_can_come_from_a_file(self) -> None:
        watch = self.root / "watched.txt"
        watch.write_text("/feed/timeline/\n\n/feed/reels_tray/\n", encoding="utf-8")
        capture = self.capture("I DFInstaObserve: /feed/timeline/\n")
        code, _, _ = self.run_main(
            self.record_argv(capture, "--watched-from", str(watch))
        )
        self.assertEqual(0, code)
        self.assertEqual(
            ("/feed/timeline/", "/feed/reels_tray/"), read("441", self.root)[0].watched
        )

    def test_a_vacuous_capture_is_recorded_and_announced_as_vacuous(self) -> None:
        """Worth recording — it is the honest record of a capture that saw
        nothing — and worth saying it will never be counted."""
        capture = self.capture("08-08 17:31:02.412 1 1 D Other: nothing here\n")
        code, out, _ = self.run_main(
            self.record_argv(capture, "--watched", "/feed/timeline/")
        )
        self.assertEqual(0, code)
        self.assertIn("VACUOUS", out)
        self.assertEqual(1, len(read("441", self.root)))
        with self.assertRaises(ObservationError):
            never_observed("441", self.root)

    def test_recording_with_no_watch_list_is_refused(self) -> None:
        capture = self.capture("I DFInstaObserve: /feed/timeline/\n")
        code, _, err = self.run_main(self.record_argv(capture))
        self.assertEqual(2, code)
        self.assertIn("no watch list", err)
        self.assertEqual((), read("441", self.root))

    def test_a_capture_naming_an_unwatched_path_is_refused(self) -> None:
        capture = self.capture("I DFInstaObserve: /feed/surprise/\n")
        code, _, err = self.run_main(
            self.record_argv(capture, "--watched", "/feed/timeline/")
        )
        self.assertEqual(2, code)
        self.assertIn("not watching", err)
        self.assertEqual((), read("441", self.root))

    def test_a_malformed_capture_is_refused_rather_than_partly_counted(self) -> None:
        capture = self.capture("I DFInstaObserve: /feed/timeline/\nI DFInstaObserve: \n")
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
        capture = self.capture("I DFInstaObserve: /feed/timeline/\n")
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
    def test_the_first_real_session_is_committed_and_is_evidence(self) -> None:
        """Today's real state. This test used to assert the corpus was EMPTY.

        It stopped being true on 2026-08-08, when a measurement build was walked
        on the phone for the first time: 52 requests across 4 of 16 watched paths.
        That is what turned `block_never_observed` from a rule that reports itself
        skipped into one that produces findings, so the change is the point rather
        than a regression.

        Pinned as an exact set, not a count. `never_observed` is what a human is
        asked to act on, and a path silently entering or leaving that list is
        exactly the drift worth failing over.
        """
        sessions = read("441", REPOSITORY)
        self.assertTrue(sessions, "the committed session went missing")
        # It has to be non-vacuous, or the zeros below prove nothing.
        self.assertTrue(evidential(sessions), "every committed session is vacuous")

        self.assertEqual(
            [
                "/api/v1/clips/homecoming/",
                "/clips/discover/stream/",
                "/feed/injected_reels_media/",
                "/feed/injected_reels_media_www/",
                "/feed/reels_media/",
                "/feed/reels_media_stream/",
                "/feed/text_post_app_timeline/",
                "/feed/text_post_app_timeline_priming/",
                "/feed/timeline_stream/",
                "/profile_ads/get_profile_ads/",
                "delivery/background_prefetch",
                "delivery/reels_cache",
            ],
            sorted(never_observed("441", REPOSITORY)),
        )
        # And the control, so "never observed" cannot be an artefact of a capture
        # that recorded nothing at all: the busiest path was seen many times.
        seen = {k: v for s in sessions for k, v in s.counts.items()}
        self.assertGreater(seen.get("/feed/timeline/", 0), 20)


if __name__ == "__main__":
    unittest.main()

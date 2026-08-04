"""The recovery path for a wedged claim, exercised — which until now it was not.

A worker killed mid-stage leaves `operation_claims` holding a `pending` row owned
by a token whose process is gone. The next attempt refuses with *"Operation is
already claimed"*, which is non-retryable, so the run fails. `claims.py` is the
only thing in the tree that blanks that owner so a later attempt can take the key
over, and until this file nothing had ever run it. A recovery path that has never
been exercised is not a recovery path, so these tests are about the two ways this
module can fail the person holding a wedged run:

* **It refuses when it should release.** Then the run stays wedged and recovery is
  back to hand-written SQL against an append-only ledger, under pressure, which is
  the situation the module exists to remove. :class:`ReleaseTests` drives the whole
  sequence an operator actually performs — read the row, read the token off it,
  type it back, watch a second attempt take the key over and adopt what already
  completed — and the "already claimed" refusal *before* the release is the control
  that makes the release mean anything at all.

* **It releases when it should refuse.** The worse half. A release lets a second
  attempt work a key a live worker may still hold, and the module's entire safety
  is one string comparison. :class:`OwnerTokenTests` drives every near-miss a
  looser comparison would wave through — a prefix, a different case, surrounding
  whitespace, the empty string — and after each one re-reads the row and asserts
  it is *entirely* unchanged. A refusal that already released something is worse
  than no refusal, because it also reports success at nothing.

Every refusal is pinned by its exact message rather than by its type. `ClaimError`
is raised by four separate rules in `release_claim`, and deleting one does not stop
the others firing: a quarantined row with its quarantine check removed falls
through to the status refusal, whose message also contains the word "quarantined".
`assertRaises(ClaimError)` alone would pass with the check that matters gone.

The ledger is a real `Ledger` over a `tempfile.TemporaryDirectory` throughout, and
every state is reached by calling `begin_operation` / `record_effect` /
`complete_operation` / `quarantine_operation`. A fixture that hand-wrote
`operation_claims` rows would be testing this module against the ledger someone
imagined rather than against the one that actually wedges.

`tests/test_ledger_read_only.py` is the model for the `mode=ro` claims and
`tests/test_rulings.py` for the plan/refusal/positive-control shape.
"""

import contextlib
import dataclasses
import hashlib
import io
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dfinsta_pipeline import claims
from dfinsta_pipeline.claims import (
    RELEASABLE,
    Claim,
    ClaimError,
    main,
    pending_claims,
    read_claim,
    release_claim,
)
from dfinsta_pipeline.contracts import ArtifactRef
from dfinsta_pipeline.ledger import Ledger

OPERATION_KEY = "stage-build-wedged-1"
OPERATION_KIND = "phase_b_build_v1"
INPUT_SHA256 = "a" * 64

#: The token of the worker that was killed. Deliberately not a bare word: the
#: near-misses below are built by slicing and re-casing it, and a one-syllable
#: token would make a prefix indistinguishable from a typo.
OWNER_TOKEN = "worker-9f3a-attempt-1"

#: The token a *later* attempt claims with, once the first has been released.
LATER_TOKEN = "worker-c4d1-attempt-2"


def artifact_ref(producer_operation_id: str, payload: bytes = b"wedged run output") -> ArtifactRef:
    digest = hashlib.sha256(payload).hexdigest()
    return ArtifactRef(
        1,
        "patched_apk",
        digest,
        len(payload),
        f"cas://sha256/{digest}",
        producer_operation_id,
        (),
    )


class ClaimTestCase(unittest.TestCase):
    """A state root, a real ledger, and one `pending` claim owned by a dead worker."""

    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name).resolve() / "state"
        self.path = self.root / "ledger.sqlite3"
        self.ledger = Ledger(self.path)
        self.wedge(OPERATION_KEY)

    def wedge(self, operation_key: str, owner_token: str = OWNER_TOKEN) -> None:
        """The state a killed worker leaves: claimed, `pending`, nothing recorded."""
        self.ledger.begin_operation(
            operation_key, OPERATION_KIND, INPUT_SHA256, owner_token, retry_safe=False
        )

    def claim(self, operation_key: str = OPERATION_KEY) -> Claim:
        return read_claim(self.path, operation_key)

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        """`main` with its two streams kept apart, because which one is used matters."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = main(list(argv))
        return status, out.getvalue(), err.getvalue()

    def assert_legible_refusal(self, status: int, out: str, err: str) -> str:
        """A refusal is one line on stderr, and never a traceback.

        This project treats a traceback-as-refusal as a defect: it teaches whoever
        is on call to skim tracebacks, and the next one will be a real crash. The
        empty-stdout half matters too — a half-printed claim above an error reads
        like something happened.
        """
        self.assertEqual(status, 1)
        self.assertEqual(out, "")
        self.assertNotIn("Traceback", err)
        self.assertTrue(err.startswith("error: "), err)
        self.assertEqual(err.count("\n"), 1, err)
        return err[len("error: ") :].strip()


# ------------------------------------------------------------------ the read side


class ReadClaimTests(ClaimTestCase):
    """What an operator sees before deciding anything."""

    def test_a_pending_claim_reads_back_every_field_the_ledger_holds(self):
        """The owner token is the one field the release depends on being right.

        Compared against the row the ledger itself wrote, not against the constants
        this test passed in, so a reader that returned the columns in the wrong
        order — which a frozen dataclass built by `Claim(*row)` would do silently —
        fails here rather than at a release that aims at the wrong row.
        """
        claim = self.claim()
        self.assertIs(type(claim), Claim)
        self.assertEqual(claim.operation_key, OPERATION_KEY)
        self.assertEqual(claim.kind, OPERATION_KIND)
        self.assertEqual(claim.input_sha256, INPUT_SHA256)
        self.assertEqual(claim.owner_token, OWNER_TOKEN)
        self.assertEqual(claim.owner_attempt, 1)
        self.assertEqual(claim.status, RELEASABLE)
        self.assertIs(claim.releasable, True)

        with self.ledger._connection() as connection:
            row = connection.execute(
                "SELECT operation_key, kind, input_sha256, owner_token, owner_attempt, status "
                "FROM operation_claims WHERE operation_key = ?",
                (OPERATION_KEY,),
            ).fetchone()
        self.assertEqual(row, dataclasses.astuple(claim))

    def test_the_description_prints_the_token_that_must_be_typed_back(self):
        """`show` is the only place the token comes from, so it has to be complete.

        Printed as the exact `--release <token>` line rather than as a bare value:
        an operator who has to reconstruct the flag is an operator who guesses, and
        the guess this module is protecting against is aiming a release at a row
        nobody looked at.
        """
        described = self.claim().describe()
        self.assertIn(f"owner       {OWNER_TOKEN}", described)
        self.assertIn(f"--release {OWNER_TOKEN}", described)
        self.assertIn("Pending and owned", described)
        self.assertIn("no worker is still running this operation", described)

    def test_an_operation_key_that_does_not_exist_is_refused_not_invented(self):
        """A key with no row is a typo or the wrong state root, never an empty claim.

        Returning a blank `Claim` would describe an unclaimed, unowned operation —
        which is exactly what a *successfully released* one looks like — so an
        operator would read "already released" and stop looking.
        """
        with self.assertRaises(ClaimError) as caught:
            self.claim("stage-build-never-started")
        self.assertEqual(
            str(caught.exception),
            "No claim recorded for operation stage-build-never-started",
        )
        # The control: the same reader, same ledger, on a key that does exist.
        self.assertEqual(self.claim().operation_key, OPERATION_KEY)

    def test_a_missing_ledger_names_the_path_and_creates_nothing(self):
        """The wrong `--state-root` is the likeliest mistake at 3am.

        `Ledger(read_only=True)` refuses to create what it was asked to read, and
        this module must not undo that by opening the writable ledger first — an
        empty ledger answers "no claim recorded", which reads as "your run is fine".
        """
        absent = self.root.parent / "other-state" / "ledger.sqlite3"
        with self.assertRaises(ClaimError) as caught:
            read_claim(absent, OPERATION_KEY)
        self.assertEqual(str(caught.exception), f"No ledger at {absent}")
        self.assertFalse(absent.exists())
        self.assertFalse(absent.parent.exists())

    def test_a_directory_standing_in_for_the_ledger_is_refused(self):
        directory = self.root.parent / "directory-state" / "ledger.sqlite3"
        directory.mkdir(parents=True)
        with self.assertRaises(ClaimError) as caught:
            read_claim(directory, OPERATION_KEY)
        self.assertEqual(str(caught.exception), f"No ledger at {directory}")
        self.assertTrue(directory.is_dir())

    def test_pending_claims_lists_the_wedged_rows_and_only_those(self):
        """The list an operator wants first: what is stuck, before knowing the key.

        Both halves in one test. A row that is released, one that has recorded an
        effect, one that completed and one that is quarantined must all be absent —
        and the still-wedged row must still be present, because a filter that
        excluded everything would satisfy the four absences on its own.
        """
        for suffix, prepare in (
            ("released", lambda key: self.ledger.release_pending_operation(key, OWNER_TOKEN)),
            ("effect", lambda key: self.ledger.record_effect(key, OWNER_TOKEN, artifact_ref(key))),
            ("quarantined", lambda key: self.ledger.quarantine_operation(key, OWNER_TOKEN)),
        ):
            key = f"stage-build-{suffix}"
            self.wedge(key)
            prepare(key)
        completed = "stage-build-completed"
        self.wedge(completed)
        self.ledger.record_effect(completed, OWNER_TOKEN, artifact_ref(completed))
        self.ledger.complete_operation(completed, artifact_ref(completed))

        listed = pending_claims(self.path)
        self.assertEqual([item.operation_key for item in listed], [OPERATION_KEY])
        self.assertEqual(listed[0], self.claim())

    def test_the_documented_surface_is_all_importable(self):
        # `pending_claims` is public, is what the CLI's no-argument path calls, and
        # is deliberately checked here because it is NOT in `__all__`.
        for name in [*claims.__all__, "pending_claims", "RELEASABLE"]:
            with self.subTest(name=name):
                self.assertTrue(hasattr(claims, name), name)
        self.assertEqual(RELEASABLE, "pending")
        self.assertTrue(issubclass(ClaimError, RuntimeError))


# ------------------------------------------------------------------- the release


class ReleaseTests(ClaimTestCase):
    """The whole point of the module: a wedged run becomes a resumable one."""

    def test_the_wedge_is_real_before_the_release_and_gone_after_it(self):
        """The sequence an operator performs, with the wedge proven at both ends.

        The first `begin_operation` is the control. Without it "a later attempt
        succeeds" is worth nothing — a second attempt succeeds against an unwedged
        ledger too, so the test would pass with `release_claim` returning early and
        touching no row at all.
        """
        with self.assertRaisesRegex(ValueError, "Operation is already claimed"):
            self.ledger.begin_operation(
                OPERATION_KEY, OPERATION_KIND, INPUT_SHA256, LATER_TOKEN, retry_safe=False
            )

        released = release_claim(self.path, OPERATION_KEY, self.claim().owner_token)

        self.assertEqual(released.owner_token, "")
        self.assertEqual(released.status, RELEASABLE)
        self.assertIs(released.releasable, False)
        self.assertIn("Already released", released.describe())
        self.assertIn("owner       (released)", released.describe())
        # The returned claim is re-read from the ledger, not the one that was
        # checked: a stale copy would print the token it just blanked.
        self.assertEqual(released, self.claim())

        self.assertIsNone(
            self.ledger.begin_operation(
                OPERATION_KEY, OPERATION_KIND, INPUT_SHA256, LATER_TOKEN, retry_safe=False
            )
        )
        taken_over = self.claim()
        self.assertEqual(taken_over.owner_token, LATER_TOKEN)
        self.assertEqual(taken_over.owner_attempt, 2)
        self.assertEqual(self.ledger.operation_status(OPERATION_KEY), "pending")

    def test_the_later_attempt_adopts_the_work_the_dead_worker_finished(self):
        """"The run is wedged, not burned" is the claim; this is what it costs if false.

        A released key that lost its neighbours' recorded output would make recovery
        a re-run of every completed stage. So the second operation here is completed
        before the release, and the later attempt has to get its `ArtifactRef` back
        from `begin_operation` rather than a fresh `None`.
        """
        finished = "stage-decode-completed"
        output = artifact_ref(finished)
        self.wedge(finished)
        self.ledger.record_effect(finished, OWNER_TOKEN, output)
        self.ledger.complete_operation(finished, output)

        release_claim(self.path, OPERATION_KEY, OWNER_TOKEN)

        self.assertEqual(
            self.ledger.begin_operation(
                finished, OPERATION_KIND, INPUT_SHA256, LATER_TOKEN, retry_safe=False
            ),
            output,
        )
        self.assertIsNone(
            self.ledger.begin_operation(
                OPERATION_KEY, OPERATION_KIND, INPUT_SHA256, LATER_TOKEN, retry_safe=False
            )
        )

    def test_releasing_an_already_released_claim_is_refused_by_name(self):
        """A second release is a sign the operator is not looking at what they think.

        It changes nothing either way, so the value is entirely in the message: the
        row *is* `pending`, so the status refusal cannot fire, and without this rule
        the empty owner would be compared against the empty `--release` value and
        the tool would report a release it did not perform.
        """
        release_claim(self.path, OPERATION_KEY, OWNER_TOKEN)
        for token in (OWNER_TOKEN, ""):
            with self.subTest(token=token):
                with self.assertRaises(ClaimError) as caught:
                    release_claim(self.path, OPERATION_KEY, token)
                self.assertEqual(
                    str(caught.exception), f"{OPERATION_KEY} is already released"
                )

    def test_releasing_writes_no_operation_event(self):
        """The append-only history records what the *run* did, not what an operator did.

        Pinned because the alternative is tempting and wrong: a 'released' event has
        no status in the table's CHECK constraint, and inventing one would make
        `operation_status` — which every adoption path reads — answer with something
        no caller knows.
        """
        before = self.ledger.operation_event_count(OPERATION_KEY)
        release_claim(self.path, OPERATION_KEY, OWNER_TOKEN)
        self.assertEqual(self.ledger.operation_event_count(OPERATION_KEY), before)
        self.assertEqual(self.ledger.operation_status(OPERATION_KEY), "pending")


# ------------------------------------------------------- the token, typed exactly


class OwnerTokenTests(ClaimTestCase):
    """One string comparison carries the whole safety of the operation."""

    #: Every near-miss a looser comparison would wave through, each named for the
    #: mutation that produces it. `prefix` passes a `startswith` check, `empty`
    #: passes `startswith` too and is the value an operator gets from an unset
    #: shell variable, `upper` passes a case-folded compare, and the two padded
    #: forms pass a `strip()` — which is the single most plausible "helpful"
    #: change to make to this line, since the token arrives from a terminal.
    NEAR_MISSES = {
        "empty": "",
        "prefix": OWNER_TOKEN[:9],
        "upper": OWNER_TOKEN.upper(),
        "padded both sides": f"  {OWNER_TOKEN}  ",
        "trailing newline": f"{OWNER_TOKEN}\n",
        "one character longer": f"{OWNER_TOKEN}x",
        "one character shorter": OWNER_TOKEN[:-1],
        "another live worker": LATER_TOKEN,
        "the operation key": OPERATION_KEY,
    }

    def test_every_near_miss_is_refused_and_leaves_the_claim_exactly_as_it_was(self):
        """The second half is the one that matters.

        A comparison that refuses but has already blanked the owner is worse than no
        comparison at all: the run is now open to a second attempt *and* the operator
        has been told it is not. So each case re-reads the whole row and compares it
        against the snapshot, rather than checking the owner field alone.

        The positive control is last, and is the point: after nine refusals the
        correct token still works. A comparison that refused everything would satisfy
        every assertion above it and leave the recovery path just as dead as before.
        """
        before = self.claim()
        for label, token in self.NEAR_MISSES.items():
            with self.subTest(near_miss=label):
                with self.assertRaises(ClaimError) as caught:
                    release_claim(self.path, OPERATION_KEY, token)
                self.assertEqual(
                    str(caught.exception),
                    f"{OPERATION_KEY} is owned by {OWNER_TOKEN!r}, not {token!r}. "
                    "The owner must be stated exactly, so a release cannot be aimed "
                    "at a row nobody looked at.",
                )
                self.assertEqual(self.claim(), before)

        self.assertEqual(release_claim(self.path, OPERATION_KEY, OWNER_TOKEN).owner_token, "")

    def test_the_message_shows_both_tokens_quoted_so_the_difference_is_visible(self):
        """A padded or case-shifted token is invisible unquoted.

        The message is what an operator compares by eye, and `worker-9f3a-attempt-1`
        beside `worker-9f3a-attempt-1 ` reads as the same string until the repr puts
        quotes around it.
        """
        with self.assertRaises(ClaimError) as caught:
            release_claim(self.path, OPERATION_KEY, f"{OWNER_TOKEN} ")
        message = str(caught.exception)
        self.assertIn(repr(OWNER_TOKEN), message)
        self.assertIn(repr(f"{OWNER_TOKEN} "), message)
        self.assertNotEqual(repr(OWNER_TOKEN), repr(f"{OWNER_TOKEN} "))

    def test_a_release_aimed_at_the_wrong_operation_cannot_hit_this_one(self):
        """Two wedged operations, and the token is not what disambiguates them.

        Both rows can legitimately carry the same owner token — one worker claims
        several stages — so the key is the only thing separating them, and a release
        must move exactly one row.
        """
        neighbour = "stage-decode-wedged-1"
        self.wedge(neighbour)
        self.assertEqual(self.claim(neighbour).owner_token, OWNER_TOKEN)

        release_claim(self.path, neighbour, OWNER_TOKEN)
        self.assertEqual(self.claim(neighbour).owner_token, "")
        self.assertEqual(self.claim().owner_token, OWNER_TOKEN)


# ------------------------------------------------------- statuses that never move


class QuarantineTests(ClaimTestCase):
    """Terminal by design, and the refusal has to say which rule stopped it."""

    def setUp(self) -> None:
        super().setUp()
        self.ledger.quarantine_operation(OPERATION_KEY, OWNER_TOKEN)
        self.assertEqual(self.claim().status, "quarantined")

    def test_a_quarantined_row_is_refused_by_the_quarantine_rule_and_stays_quarantined(self):
        """Asserted on the exact message, because the fallback refusal is a near-miss.

        Delete the quarantine check and the row falls through to
        `status != RELEASABLE`, whose message also contains the word "quarantined"
        and which also raises `ClaimError` — so `assertRaisesRegex(ClaimError,
        "quarantined")` passes with the rule that matters gone. What is actually
        lost is the reason: "the adoption path is working, there is nothing to
        release" is a comforting untruth about a row a fail-closed check refused.
        """
        with self.assertRaises(ClaimError) as caught:
            release_claim(self.path, OPERATION_KEY, OWNER_TOKEN)
        self.assertEqual(
            str(caught.exception),
            f"{OPERATION_KEY} is quarantined. That is terminal by design; releasing it "
            "would run what a fail-closed check refused.",
        )
        self.assertEqual(self.claim().status, "quarantined")
        self.assertEqual(self.claim().owner_token, OWNER_TOKEN)

    def test_the_quarantine_rule_fires_before_the_owner_is_even_considered(self):
        """An operator must not learn "wrong token" about a row no token can release.

        Retyping the token is the obvious response to that message, and it would
        send someone round the loop again on an operation whose recovery is a new
        run id, a new run spec and a new gate decision.
        """
        with self.assertRaises(ClaimError) as caught:
            release_claim(self.path, OPERATION_KEY, "a-token-that-owns-nothing")
        self.assertIn("terminal by design", str(caught.exception))
        self.assertNotIn("owned by", str(caught.exception))
        self.assertEqual(self.claim().status, "quarantined")

    def test_the_description_says_what_recovery_actually_is(self):
        described = self.claim().describe()
        self.assertIn("Quarantined. Terminal by design", described)
        self.assertIn("a new run id, a new run spec and a new gate decision", described)
        self.assertNotIn("--release", described)


class AdoptionPathTests(ClaimTestCase):
    """`effect` and `completed` are not wedged rows — they are the recovery working.

    Blanking their owner would gain nothing: `begin_operation` returns their
    recorded `ArtifactRef` to whoever asks next, whatever the owner column says. So
    the refusal here is not a safety rule, it is a correction — an operator who
    reaches for `--release` on one of these has misread which stage is stuck.
    """

    def reach(self, status: str) -> ArtifactRef:
        output = artifact_ref(OPERATION_KEY)
        self.ledger.record_effect(OPERATION_KEY, OWNER_TOKEN, output)
        if status == "completed":
            self.ledger.complete_operation(OPERATION_KEY, output)
        self.assertEqual(self.claim().status, status)
        return output

    def test_neither_status_can_be_released_and_neither_row_moves(self):
        for status in ("effect", "completed"):
            with self.subTest(status=status):
                self.setUp()
                output = self.reach(status)
                before = self.claim()

                with self.assertRaises(ClaimError) as caught:
                    release_claim(self.path, OPERATION_KEY, OWNER_TOKEN)
                self.assertEqual(
                    str(caught.exception),
                    f"{OPERATION_KEY} is {status}, not {RELEASABLE}. A later attempt "
                    "already adopts its recorded output; there is nothing to release.",
                )
                self.assertEqual(self.claim(), before)
                self.assertIs(before.releasable, False)

                # The correction is true, and this is what makes it true: a later
                # attempt gets the recorded output without any release at all.
                self.assertEqual(
                    self.ledger.begin_operation(
                        OPERATION_KEY, OPERATION_KIND, INPUT_SHA256, LATER_TOKEN,
                        retry_safe=False,
                    ),
                    output,
                )

    def test_the_description_explains_the_adoption_path_rather_than_offering_a_flag(self):
        for status in ("effect", "completed"):
            with self.subTest(status=status):
                self.setUp()
                self.reach(status)
                described = self.claim().describe()
                self.assertIn(f"Status {status}: nothing to release", described)
                self.assertIn("adoption path working", described)
                self.assertNotIn("--release", described)


class LedgerIsTheFinalFenceTests(ClaimTestCase):
    """The tool's read and the tool's write are two statements, not one.

    `release_claim` reads the row, checks it, and then asks the ledger to release —
    and between those the row can move, because the whole reason this tool exists is
    that something else was recently touching this key. The ledger re-checks owner
    and status inside `BEGIN IMMEDIATE`, and that, not the check in `claims.py`, is
    what makes the release safe. Proven by handing `release_claim` a stale read.
    """

    def test_a_stale_read_cannot_release_a_row_that_has_since_been_quarantined(self):
        stale = self.claim()
        self.ledger.quarantine_operation(OPERATION_KEY, OWNER_TOKEN)

        with mock.patch.object(claims, "read_claim", return_value=stale) as reader:
            with self.assertRaisesRegex(ValueError, "Only a pending operation can be released"):
                release_claim(self.path, OPERATION_KEY, OWNER_TOKEN)
        self.assertTrue(reader.called)
        self.assertEqual(self.claim().status, "quarantined")
        self.assertEqual(self.claim().owner_token, OWNER_TOKEN)

    def test_a_stale_read_cannot_release_a_row_another_attempt_has_taken_over(self):
        stale = self.claim()
        self.ledger.release_pending_operation(OPERATION_KEY, OWNER_TOKEN)
        self.ledger.begin_operation(
            OPERATION_KEY, OPERATION_KIND, INPUT_SHA256, LATER_TOKEN, retry_safe=False
        )

        with mock.patch.object(claims, "read_claim", return_value=stale):
            with self.assertRaisesRegex(ValueError, "release owner does not match claim"):
                release_claim(self.path, OPERATION_KEY, OWNER_TOKEN)
        self.assertEqual(self.claim().owner_token, LATER_TOKEN)


# ------------------------------------------------------------ a ledger it may read


@unittest.skipIf(os.geteuid() == 0, "running as root ignores the mode bits this test sets")
class ReadOnlyLedgerTests(ClaimTestCase):
    """`read_claim` promises it "opens the ledger read-only to prove it".

    Pointed at a ledger the process genuinely cannot write, the read must still
    answer and the release must still refuse — legibly, and without having changed
    anything. Both halves matter: a reader that quietly opened the writable `Ledger`
    would run the schema statements on a database an operator was told this tool
    only inspects, and would fail on the read as well, so `show` — the one thing
    that still works on a locked-down ledger — would stop working too.
    """

    def make_unwritable(self, target: Path) -> None:
        mode = target.stat().st_mode
        self.addCleanup(os.chmod, target, stat.S_IMODE(mode))
        os.chmod(target, stat.S_IMODE(mode) & ~0o222)

    def test_show_answers_and_release_refuses_when_the_ledger_file_is_read_only(self):
        self.make_unwritable(self.path)
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()

        self.assertEqual(self.claim().owner_token, OWNER_TOKEN)
        status, out, err = self.run_cli("--state-root", str(self.root), OPERATION_KEY)
        self.assertEqual((status, err), (0, ""))
        self.assertIn(f"--release {OWNER_TOKEN}", out)

        with self.assertRaises(sqlite3.OperationalError) as caught:
            release_claim(self.path, OPERATION_KEY, OWNER_TOKEN)
        self.assertIn("readonly", str(caught.exception))
        self.assertEqual(self.claim().owner_token, OWNER_TOKEN)
        # The database itself is byte-identical. (The `-wal`/`-shm` sidecars a WAL
        # read has to create are a separate matter and are the ledger's, not this
        # module's; what is asserted here is that nothing in the ledger moved.)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)

    def test_the_read_only_refusal_reaches_the_operator_as_a_message(self):
        """A `sqlite3.Error` is not a `ClaimError`, and must not surface as a crash.

        This is the one refusal that does not come from `claims.py` at all, so it is
        also the one most likely to be left out of `main`'s caught set.
        """
        self.make_unwritable(self.path)
        status, out, err = self.run_cli(
            "--state-root", str(self.root), OPERATION_KEY, "--release", OWNER_TOKEN
        )
        self.assertIn("readonly", self.assert_legible_refusal(status, out, err))
        self.assertEqual(self.claim().owner_token, OWNER_TOKEN)

    def test_a_read_only_state_directory_stops_the_read_too_and_says_so(self):
        """The likelier shape in practice, and it is *not* the same as a locked file.

        The ledger is a WAL database, so even a `mode=ro` open has to create the
        `-shm` sidecar — which a read-only directory forbids. So `show` stops working
        as well, and the only thing that saves the operator is that the refusal is
        legible: without it, the tool that exists for the worst moment of a run would
        answer a locked-down state root with a traceback about SQLite.
        """
        self.make_unwritable(self.root)

        for argv in (
            ["--state-root", str(self.root), OPERATION_KEY],
            ["--state-root", str(self.root), OPERATION_KEY, "--release", OWNER_TOKEN],
        ):
            with self.subTest(argv=argv[-1]):
                self.assertIn("readonly", self.assert_legible_refusal(*self.run_cli(*argv)))

        os.chmod(self.root, 0o755)
        self.assertEqual(self.claim().owner_token, OWNER_TOKEN)


class UnreadableLedgerTests(ClaimTestCase):
    """A file that is not a ledger is not an empty ledger."""

    def test_a_file_that_is_not_a_database_is_a_message_not_a_traceback(self):
        root = self.root.parent / "garbage-state"
        root.mkdir()
        (root / "ledger.sqlite3").write_bytes(b"this is not a ledger, it is a note\n")
        status, out, err = self.run_cli("--state-root", str(root), OPERATION_KEY)
        self.assertEqual(self.assert_legible_refusal(status, out, err), "file is not a database")

    def test_an_empty_file_is_refused_and_no_schema_is_created_over_it(self):
        """An empty file is a database with no tables, and creating them is the trap.

        `read_claim` going through the writable `Ledger` would answer "no claim
        recorded for operation ..." here — a confident statement about a run, made
        from a ledger this tool had just invented.
        """
        root = self.root.parent / "empty-state"
        root.mkdir()
        empty = root / "ledger.sqlite3"
        empty.write_bytes(b"")
        status, out, err = self.run_cli("--state-root", str(root), OPERATION_KEY)
        self.assertEqual(self.assert_legible_refusal(status, out, err), "no such table: decisions")
        self.assertEqual(empty.read_bytes(), b"")
        self.assertEqual(sorted(entry.name for entry in root.iterdir()), ["ledger.sqlite3"])

    def test_a_ledger_with_no_claims_table_is_not_repaired_into_answering(self):
        """The sharpest form of "opens the ledger read-only to prove it".

        A database that has `decisions` but no `operation_claims` gets past
        `Ledger(read_only=True)`'s own probe, so this is the one input where opening
        the *writable* ledger instead would look like it worked: the schema
        statements would create the missing table and the tool would answer "No
        claim recorded for operation ..." — indistinguishable from a healthy ledger
        that has never seen this key, and read off rows it had just created itself.
        """
        root = self.root.parent / "half-schema-state"
        root.mkdir()
        half = root / "ledger.sqlite3"
        connection = sqlite3.connect(half)
        try:
            connection.execute("CREATE TABLE decisions (decision_id TEXT PRIMARY KEY)")
            connection.commit()
        finally:
            connection.close()
        before = hashlib.sha256(half.read_bytes()).hexdigest()

        with self.assertRaises(sqlite3.OperationalError) as caught:
            read_claim(half, OPERATION_KEY)
        self.assertEqual(str(caught.exception), "no such table: operation_claims")
        self.assertEqual(hashlib.sha256(half.read_bytes()).hexdigest(), before)

        status, out, err = self.run_cli("--state-root", str(root), OPERATION_KEY)
        self.assertEqual(
            self.assert_legible_refusal(status, out, err), "no such table: operation_claims"
        )
        self.assertEqual(hashlib.sha256(half.read_bytes()).hexdigest(), before)


# ------------------------------------------------------------------- the CLI shape


class CommandLineTests(ClaimTestCase):
    """Exit codes and streams, because a wrapper script reads them and a human does not."""

    def test_show_and_list_succeed_and_write_only_to_stdout(self):
        status, out, err = self.run_cli("--state-root", str(self.root))
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(
            out, f"{OPERATION_KEY}  {OPERATION_KIND}  owner={OWNER_TOKEN}\n"
        )

        status, out, err = self.run_cli("--state-root", str(self.root), OPERATION_KEY)
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(out, self.claim().describe() + "\n")

    def test_an_empty_list_says_so_rather_than_printing_nothing(self):
        """Silence is what a broken tool also produces."""
        release_claim(self.path, OPERATION_KEY, OWNER_TOKEN)
        status, out, err = self.run_cli("--state-root", str(self.root))
        self.assertEqual((status, out, err), (0, "no pending owned claims\n", ""))

    def test_a_release_prints_the_row_it_left_behind_and_exits_zero(self):
        status, out, err = self.run_cli(
            "--state-root", str(self.root), OPERATION_KEY, "--release", OWNER_TOKEN
        )
        self.assertEqual((status, err), (0, ""))
        self.assertTrue(out.startswith("released.\n"), out)
        self.assertIn("owner       (released)", out)
        self.assertIn("Already released", out)
        self.assertEqual(self.claim().owner_token, "")

    def test_every_refusal_exits_one_with_a_message_and_no_traceback(self):
        """One table, so a refusal added later without a message is visible.

        Each of these is a `ClaimError` or a `sqlite3.Error` raised somewhere the
        operator cannot see, and the only thing standing between it and a traceback
        is one `except` clause in `main`.
        """
        quarantined = "stage-build-quarantined"
        self.wedge(quarantined)
        self.ledger.quarantine_operation(quarantined, OWNER_TOKEN)
        completed = "stage-build-completed"
        self.wedge(completed)
        self.ledger.record_effect(completed, OWNER_TOKEN, artifact_ref(completed))
        self.ledger.complete_operation(completed, artifact_ref(completed))
        missing_root = self.root.parent / "no-such-state"

        cases = {
            "wrong token": (
                ["--state-root", str(self.root), OPERATION_KEY, "--release", "not-the-token"],
                "is owned by",
            ),
            "quarantined": (
                ["--state-root", str(self.root), quarantined, "--release", OWNER_TOKEN],
                "terminal by design",
            ),
            "completed": (
                ["--state-root", str(self.root), completed, "--release", OWNER_TOKEN],
                "there is nothing to release",
            ),
            "unknown key": (
                ["--state-root", str(self.root), "stage-build-never-started"],
                "No claim recorded",
            ),
            "wrong state root": (
                ["--state-root", str(missing_root), OPERATION_KEY],
                f"No ledger at {missing_root / 'ledger.sqlite3'}",
            ),
        }
        for label, (argv, expected) in cases.items():
            with self.subTest(refusal=label):
                status, out, err = self.run_cli(*argv)
                self.assertIn(expected, self.assert_legible_refusal(status, out, err))

        # The control: nothing above was refused because the CLI refuses everything.
        self.assertEqual(
            self.run_cli("--state-root", str(self.root), OPERATION_KEY, "--release", OWNER_TOKEN)[0],
            0,
        )

    def test_release_without_an_operation_key_is_refused_by_argparse(self):
        """Exit 2 and a usage line, not a release aimed at every pending claim."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                main(["--state-root", str(self.root), "--release", OWNER_TOKEN])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("--release needs an operation key", err.getvalue())
        self.assertEqual(self.claim().owner_token, OWNER_TOKEN)

    def test_a_missing_state_root_argument_is_refused_by_argparse(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                main([OPERATION_KEY])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("--state-root", err.getvalue())

    def test_an_error_the_tool_does_not_name_is_not_dressed_up_as_a_refusal(self):
        """`main` catches three families, and widening that is the tempting change.

        A bug in this tool and an operator's mistake must not arrive looking the
        same. Turning every `Exception` into `error: ...` and exit 1 would report a
        `TypeError` in `claims.py` as though the ledger had said no — and the whole
        value of the tool is that its refusals are trustworthy statements about the
        ledger.

        The three named families are the control below: they must still be caught,
        so this is a statement about the *boundary* and not about `except` in
        general.
        """
        argv = ["--state-root", str(self.root), OPERATION_KEY]
        with mock.patch.object(claims, "read_claim", side_effect=TypeError("bug in the tool")):
            with self.assertRaisesRegex(TypeError, "bug in the tool"):
                self.run_cli(*argv)
        with mock.patch.object(claims, "read_claim", side_effect=KeyError("owner_token")):
            with self.assertRaises(KeyError):
                self.run_cli(*argv)

        for error in (
            ClaimError("a claim refusal"),
            ValueError("a ledger refusal"),
            sqlite3.OperationalError("a database refusal"),
        ):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(claims, "read_claim", side_effect=error):
                    status, out, err = self.run_cli(*argv)
                self.assertEqual(self.assert_legible_refusal(status, out, err), str(error))


class EmptyReleaseTokenTests(ClaimTestCase):
    """`--release ""` is a wrong owner token, and must be refused like any other.

    It used not to be. `main` gated the release on `if args.release:`, so the
    empty string was falsy, the whole branch was skipped, the claim was printed
    and the exit code was 0 — which is exactly what a *successful read* looks
    like. It was the one wrong token that produced neither a release nor a
    refusal. And it is not exotic: `claims.py --release "$OWNER"` with `OWNER`
    unset expands to precisely this.

    Nothing was ever released, so it was a legibility defect rather than a safety
    one — but the module's whole discipline is that a release is refused unless
    the owner is stated exactly, and "neither performed nor refused" is not a
    third acceptable outcome. The fix is `default=None` and `is not None`, never
    truthiness; the same shape as an empty `--expected-certificate-sha256` that
    silently disabled a signing pin.
    """

    def test_the_api_refuses_an_empty_token_like_any_other_wrong_one(self):
        with self.assertRaises(ClaimError) as caught:
            release_claim(self.path, OPERATION_KEY, "")
        self.assertEqual(
            str(caught.exception),
            f"{OPERATION_KEY} is owned by {OWNER_TOKEN!r}, not ''. "
            "The owner must be stated exactly, so a release cannot be aimed at a row "
            "nobody looked at.",
        )
        self.assertEqual(self.claim().owner_token, OWNER_TOKEN)

    def test_the_cli_refuses_an_empty_release_flag(self):
        status, out, err = self.run_cli(
            "--state-root", str(self.root), OPERATION_KEY, "--release", ""
        )
        self.assertEqual(status, 1)
        self.assertIn("is owned by", err)
        self.assertIn("not ''", err)
        # And the refusal refused: the claim is exactly as it was.
        self.assertEqual(self.claim().owner_token, OWNER_TOKEN)
        self.assertEqual(self.claim().status, "pending")

    def test_the_cli_refuses_an_empty_release_with_no_key(self):
        """The same truthiness test also skipped `--release needs an operation key`.

        `parser.error` exits rather than returning, so this is a `SystemExit`
        where the other refusals are a return code — argparse's usage error, not
        the module's, which is right for a flag combination that cannot be acted
        on at all.
        """
        with self.assertRaises(SystemExit) as raised:
            self.run_cli("--state-root", str(self.root), "--release", "")
        self.assertEqual(raised.exception.code, 2)

    def test_pending_claims_is_exported(self):
        """A star import used to get everything except the default path's function."""
        from dfinsta_pipeline import claims as module

        self.assertIn("pending_claims", module.__all__)
        for name in module.__all__:
            self.assertTrue(hasattr(module, name), name)


if __name__ == "__main__":
    unittest.main()

"""`reaper.py` deletes, so every test here is about what it must *not* delete.

Nothing in this tree has ever removed an attempt workspace, so the first thing
that does is the first thing that can destroy a run's evidence. The module's own
contract names four things it will never do, and each of them is a single
expression that a later edit can delete without breaking anything else in the
program — the sweep still runs, still prints a report, still exits 0. That is the
shape of failure this file exists to catch:

* **It deletes a live workspace.** A `pending` claim's owner is writing
  `sha256(owner_token)` right now, and the join that protects it is one hash
  comparison in `_survey`. :class:`OwnerHashJoinTests` drives both sides of that
  join in one scenario — a superseded sibling under the same key, which *must*
  go, beside the owner's own leaf, which must not — because a join that answered
  "keep" for everything would satisfy the safety half on its own and quietly turn
  the tool into a no-op.

* **It deletes something it cannot restore.** `effect` and `completed` are banked
  in the content store; `quarantined` is the only surviving record of why a
  fail-closed check refused, and a workspace with no ledger row at all is not
  understood. :class:`RefusalTests` pins each refusal by its exact message and
  then, for the two that have a flag, reaps the same workspace with the flag on.
  The refusal without the flag proves the default is safe; the removal with it
  proves the refusal was the rule and not an accident of the fixture.

* **It runs against a directory that is not an attempts root.**
  :class:`MistypedAttemptsRootTests` is the most important class in the file. A
  mistyped `--attempts-root` is the failure that costs a home directory, and the
  contract is abort-before-anything rather than skip-and-report — so every test
  there asserts on the *tree afterwards*, not on the exception. A guard that
  raises after removing four workspaces has raised, and has also done the damage.
  Its workspaces are all perfectly reapable, and one test removes the stranger
  and reaps them, so "nothing was removed" is a statement rather than a
  tautology.

* **It answers a bad root with a traceback instead of a refusal.**
  :class:`RefusalChannelTests`. This project has shipped that gap before — the
  feature-gate client — and the lesson recorded from it is that a refusal channel
  is only a channel if everything uses it. So a group-readable root must arrive
  as `ReaperError` and as `refused: …` on stderr with exit 2, never as the bare
  `ValueError` that `_validate_private_directory` raises.

:class:`ClosedDefectTests` were `expectedFailure` for about an hour, the same way
`tests/test_probes.py` records one. Each asserted what this module's own
docstrings say must happen and what it did not yet do — a `sqlite3` error
escaping the refusal channel, and `reap` opening an `operation_key` it never
checked, which removed a directory *outside* the attempts root. Writing them that
way is what made the fix announce itself as an unexpected success rather than as
a test somebody remembered to add afterwards.

Two mechanical notes. Ages are moved by `survey(now=...)` and never by sleeping
or by `os.utime`: `utime` resets `st_ctime` to the present and `_newest_mtime`
takes the max of mtime and ctime, so a workspace cannot be pushed into the past
from outside. Moving `now` is the only way, which is why the parameter exists.
And the ledger is a real :class:`Ledger` throughout, reached only through
`begin_operation` / `record_effect` / `complete_operation` /
`quarantine_operation` — a fixture that hand-wrote `operation_claims` rows would
be testing this module against the ledger someone imagined.

`tests/test_claims.py` is the model for the refusal-by-exact-message and
positive-control shape, and for the CLI-streams fixture.
"""

import ast
import contextlib
import dataclasses
import hashlib
import io
import os
import stat
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from dfinsta_pipeline import reaper, worker
from dfinsta_pipeline.activities import LONGEST_STAGE_BUDGET_MULTIPLIER
from dfinsta_pipeline.contracts import ArtifactRef
from dfinsta_pipeline.ledger import Ledger
from dfinsta_pipeline.reaper import (
    BANKED,
    DEFAULT_MIN_AGE_SECONDS,
    MIN_AGE_FLOOR_SECONDS,
    ReaperError,
    Workspace,
    describe_survey,
    reap,
)

OPERATION_KIND = "phase_b_build_v1"
INPUT_SHA256 = "a" * 64

#: The worker that claimed first and died. The leaf it left is `sha256` of this.
OWNER_TOKEN = "worker-9f3a-attempt-1"

#: The worker that took the key over afterwards and is still running.
LATER_TOKEN = "worker-c4d1-attempt-2"


def operation_key(label: str) -> str:
    """A real 64-lowercase-hex key, since that is what the root guard matches on."""
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


COMPLETED_KEY = operation_key("build-completed")
EFFECT_KEY = operation_key("verify-effect")
PENDING_KEY = operation_key("decode-pending")
QUARANTINED_KEY = operation_key("apply-quarantined")
ORPHAN_KEY = operation_key("no-ledger-row")

#: Far past `DEFAULT_MIN_AGE_SECONDS`, so every workspace this file builds is
#: unambiguously old under `aged()` and unambiguously young under `time.time()`.
#: There is no third case anywhere here, deliberately: a fixture whose ages sit
#: near the boundary turns an age assertion into a race.
AGE_OFFSET_SECONDS = 30 * 86_400


def aged() -> float:
    """A `now` from which every workspace built in `setUp` is a month old.

    Recomputed per call rather than frozen at import, so a slow suite cannot
    drift the fixture towards the boundary.
    """
    return time.time() + AGE_OFFSET_SECONDS


def owner_leaf(token: str) -> str:
    """The leaf name a stage writes for `token` — the join the whole tool rests on."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def artifact_ref(producer_operation_id: str, payload: bytes = b"banked output") -> ArtifactRef:
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


class FrozenClock:
    """Just enough of `time` for `survey`'s single call, so the CLI can be aged.

    `main` takes no `now`, so moving the module's clock is the only way to drive
    its `--confirm` path over a workspace old enough to reap. Patched onto
    `reaper`'s own namespace rather than onto `time.time` globally, so nothing
    else in the process sees a stopped clock.
    """

    def __init__(self, moment: float) -> None:
        self._moment = moment

    def time(self) -> float:
        return self._moment


class ReaperTestCase(unittest.TestCase):
    """A real ledger and a real attempts tree, both private, both disposable."""

    def setUp(self) -> None:
        holder = TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.state = Path(holder.name).resolve() / "state"
        self.ledger_path = self.state / "ledger.sqlite3"
        self.ledger = Ledger(self.ledger_path)
        self.attempts = self.state / "attempts"
        self.attempts.mkdir(mode=0o700)
        # `mkdir(mode=...)` is masked by the umask and does not touch parents, so
        # every directory this fixture creates is chmodded explicitly. Without
        # this `open_private_root` refuses the fixture itself and every test in
        # the file fails for a reason that has nothing to do with the module.
        os.chmod(self.attempts, 0o700)

    # ------------------------------------------------------------- the ledger

    def begin(self, key: str, token: str = OWNER_TOKEN) -> None:
        self.ledger.begin_operation(key, OPERATION_KIND, INPUT_SHA256, token, retry_safe=False)

    def reach_effect(self, key: str, token: str = OWNER_TOKEN) -> ArtifactRef:
        self.begin(key, token)
        output = artifact_ref(key)
        self.ledger.record_effect(key, token, output)
        return output

    def reach_completed(self, key: str, token: str = OWNER_TOKEN) -> ArtifactRef:
        output = self.reach_effect(key, token)
        self.ledger.complete_operation(key, output)
        return output

    def reach_quarantined(self, key: str, token: str = OWNER_TOKEN) -> None:
        self.begin(key, token)
        self.ledger.quarantine_operation(key, token)

    # --------------------------------------------------------- the filesystem

    def workspace(self, key: str, leaf: str, payload: bytes = b"several gigabytes") -> Path:
        """One attempt workspace holding a file and a nested directory.

        Nested on purpose. A remover that only unlinked the leaf's direct
        children would pass every assertion in a file whose workspaces were flat,
        and a real one holds a materialized decoded tree.
        """
        key_directory = self.attempts / key
        key_directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(key_directory, 0o700)
        path = key_directory / leaf
        path.mkdir(mode=0o700)
        os.chmod(path, 0o700)
        (path / "stock.apk").write_bytes(payload)
        (path / "output").mkdir(mode=0o700)
        os.chmod(path / "output", 0o700)
        (path / "output" / "receipt.json").write_bytes(b'{"receipt": true}')
        return path

    def tree(self) -> dict[str, bytes | None]:
        """Every path under the attempts root, with file contents; `None` for a directory.

        Contents and not just names, so "the workspace is gone" and "the
        workspace's bytes are gone" are the same assertion — an `rmdir` that left
        a hollowed-out directory, or a remover that emptied one without removing
        it, both show up here.
        """
        found: dict[str, bytes | None] = {}
        for path in sorted(self.attempts.rglob("*")):
            name = str(path.relative_to(self.attempts))
            found[name] = None if path.is_dir() else path.read_bytes()
        return found

    # ------------------------------------------------------------- the module

    def survey(self, now: float | None = None, **options: object) -> list[Workspace]:
        """`reaper.survey` over this fixture, aged past the default minimum by default.

        Passing `now=time.time()` instead is how a test asks for the *young* view
        of the very same tree.
        """
        return reaper.survey(
            self.attempts,
            self.ledger_path,
            now=aged() if now is None else now,
            **options,  # type: ignore[arg-type]
        )

    def verdicts(self, now: float | None = None, **options: object) -> dict[str, str | None]:
        """`operation_key/leaf -> refusal`, with `None` meaning reapable."""
        return {found.path: found.refusal for found in self.survey(now, **options)}

    def run_cli(self, *argv: str, moment: float | None = None) -> tuple[int, str, str]:
        """`main` with its two streams kept apart, because which one is used matters."""
        out, err = io.StringIO(), io.StringIO()
        clock = FrozenClock(aged() if moment is None else moment)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with mock.patch.object(reaper, "time", clock):
                status = reaper.main(
                    [
                        "--attempts-root",
                        str(self.attempts),
                        "--ledger",
                        str(self.ledger_path),
                        *argv,
                    ]
                )
        return status, out.getvalue(), err.getvalue()


# --------------------------------------------------------------- the surface


class SurfaceTests(ReaperTestCase):
    """The names and constants other things are allowed to depend on."""

    def test_the_documented_surface_is_all_importable(self) -> None:
        for name in reaper.__all__:
            with self.subTest(name=name):
                self.assertTrue(hasattr(reaper, name), name)
        self.assertTrue(issubclass(ReaperError, RuntimeError))

    def test_banked_is_exactly_the_two_statuses_the_store_already_holds(self) -> None:
        """`BANKED` is not in `__all__` and is checked anyway.

        It is the list that decides what may be deleted, and adding `pending` to
        it would be a one-word change that removes live workspaces while every
        other assertion in this file about `effect` and `completed` keeps passing.
        """
        self.assertEqual(BANKED, frozenset({"effect", "completed"}))

    def test_an_empty_attempts_root_is_an_empty_survey_and_not_an_error(self) -> None:
        """A root with nothing in it is the normal state after a successful sweep."""
        self.assertEqual(self.survey(), [])
        self.assertEqual(describe_survey([]), "No attempt workspaces found.")


# ------------------------------------------------------------ what may go


class BankedWorkspaceTests(ReaperTestCase):
    """`effect` and `completed` are redundant with the content store — the whole thesis.

    If these do not go, the tool reclaims nothing and a port keeps leaving
    gigabytes behind. So each test here ends on the disk rather than on the
    returned list: `reap` reporting a removal it did not perform is the failure
    that would leave an operator believing the problem was solved.
    """

    def test_a_completed_workspace_is_removed_and_its_bytes_go_with_it(self) -> None:
        leaf = owner_leaf(OWNER_TOKEN)
        self.reach_completed(COMPLETED_KEY)
        path = self.workspace(COMPLETED_KEY, leaf, payload=b"the patched apk")
        self.assertEqual((path / "stock.apk").read_bytes(), b"the patched apk")

        found = self.survey()
        self.assertEqual([item.status for item in found], ["completed"])
        self.assertEqual([item.refusal for item in found], [None])
        self.assertEqual([item.reapable for item in found], [True])

        removed = reap(self.attempts, found)

        self.assertEqual([item.path for item in removed], [f"{COMPLETED_KEY}/{leaf}"])
        self.assertFalse(path.exists())
        self.assertFalse((path / "stock.apk").exists())
        self.assertFalse((path / "output" / "receipt.json").exists())
        self.assertEqual(self.tree(), {})

    def test_an_effect_workspace_is_removed_before_the_claim_ever_completes(self) -> None:
        """`record_effect` is the moment the outputs are banked, not `complete_operation`.

        Every stage publishes to the store *before* the claim leaves `pending`,
        so waiting for `completed` would keep a workspace whose contents are
        already redundant — and `effect` is exactly where a run that was killed
        between the two ends up, which is the population that accumulates.
        """
        leaf = owner_leaf(OWNER_TOKEN)
        self.reach_effect(EFFECT_KEY)
        path = self.workspace(EFFECT_KEY, leaf)
        self.assertEqual(self.ledger.operation_status(EFFECT_KEY), "effect")

        found = self.survey()
        self.assertEqual([item.status for item in found], ["effect"])
        self.assertEqual(reap(self.attempts, found), found)
        self.assertFalse(path.exists())
        self.assertEqual(self.tree(), {})

    def test_a_survey_on_its_own_removes_nothing(self) -> None:
        """The report is only worth printing before `--confirm` if it is inert.

        Surveyed over a tree where every workspace is reapable, so a survey that
        removed what it verdicted would have everything to remove.
        """
        self.reach_completed(COMPLETED_KEY)
        self.reach_effect(EFFECT_KEY)
        self.workspace(COMPLETED_KEY, owner_leaf(OWNER_TOKEN))
        self.workspace(EFFECT_KEY, owner_leaf(OWNER_TOKEN))
        before = self.tree()

        found = self.survey()

        self.assertEqual([item.refusal for item in found], [None, None])
        self.assertEqual(self.tree(), before)

    def test_the_workspace_record_carries_the_ledgers_view_and_not_a_guess(self) -> None:
        """Every field is read off the claim, so a wrong join shows here first.

        `kind` and `status` come from the row, `owned` from the owner hash, and
        `operation_key`/`name` from the path. A record assembled in the wrong
        order — the mistake a positional dataclass makes silently — would put the
        key where the name goes and reap by a path that does not exist.
        """
        leaf = owner_leaf(OWNER_TOKEN)
        self.reach_completed(COMPLETED_KEY)
        self.workspace(COMPLETED_KEY, leaf)

        (found,) = self.survey()

        self.assertEqual(found.operation_key, COMPLETED_KEY)
        self.assertEqual(found.name, leaf)
        self.assertEqual(found.status, "completed")
        self.assertEqual(found.kind, OPERATION_KIND)
        self.assertIs(found.owned, True)
        self.assertIs(found.validation, False)
        self.assertEqual(found.path, f"{COMPLETED_KEY}/{leaf}")
        self.assertGreater(found.age_seconds, MIN_AGE_FLOOR_SECONDS)


# ------------------------------------------------------------ what may not


class RefusalTests(ReaperTestCase):
    """Four refusals, each pinned by its exact message and each paired with a positive.

    Pinned by message rather than by "not reapable" because every refusal returns
    a truthy string from the same function: delete the quarantine rule and a
    quarantined row falls through to `BANKED`, which returns `None`, and a test
    that only asked "was something refused" would go on passing for the other
    three. The message is the only thing that says *which* rule fired.
    """

    def setUp(self) -> None:
        super().setUp()
        self.leaf = owner_leaf(OWNER_TOKEN)

    def test_the_live_leaf_of_a_pending_claim_is_kept(self) -> None:
        """The one workspace whose loss is a running stage's loss.

        `pending` means a worker holds the key, and `sha256(owner_token)` is the
        directory it is writing into at this moment. There is no flag for this
        one and there should not be.
        """
        self.begin(PENDING_KEY)
        path = self.workspace(PENDING_KEY, self.leaf, payload=b"executor output, uncaptured")

        self.assertEqual(self.verdicts(), {f"{PENDING_KEY}/{self.leaf}": "claimed and pending"})

        self.assertEqual(reap(self.attempts, self.survey()), [])
        self.assertEqual((path / "stock.apk").read_bytes(), b"executor output, uncaptured")

    def test_a_quarantined_workspace_is_kept_by_default_and_reaped_with_the_flag(self) -> None:
        """It is the only surviving record of why a fail-closed check refused.

        Reproducible — the key is a hash of the whole input — but not
        recoverable, so the default is to keep it and the flag has to be typed.
        The second half is the control: without it, a `_refusal` that returned a
        string for every status would satisfy the first half exactly.
        """
        self.reach_quarantined(QUARANTINED_KEY)
        path = self.workspace(QUARANTINED_KEY, self.leaf, payload=b"why the check refused")
        surveyed = f"{QUARANTINED_KEY}/{self.leaf}"

        self.assertEqual(self.verdicts(), {surveyed: "quarantined (--include-quarantined)"})
        self.assertEqual(reap(self.attempts, self.survey()), [])
        self.assertEqual((path / "stock.apk").read_bytes(), b"why the check refused")

        self.assertEqual(self.verdicts(include_quarantined=True), {surveyed: None})
        removed = reap(self.attempts, self.survey(include_quarantined=True))
        self.assertEqual([item.path for item in removed], [surveyed])
        self.assertEqual(self.tree(), {})

    def test_a_workspace_with_no_ledger_row_is_kept_by_default_and_reaped_with_the_flag(
        self,
    ) -> None:
        """A directory the ledger has never heard of is not understood, so it stays.

        It is also the shape a *partly wrong* `--ledger` produces: point the tool
        at a different state root and every workspace becomes an orphan at once,
        which is why the default may not be to delete them.
        """
        path = self.workspace(ORPHAN_KEY, self.leaf, payload=b"whose is this?")
        surveyed = f"{ORPHAN_KEY}/{self.leaf}"

        (found,) = self.survey()
        self.assertIsNone(found.status)
        self.assertIsNone(found.kind)
        self.assertIs(found.owned, False)
        self.assertEqual(found.refusal, "no ledger row (--include-orphans)")
        self.assertEqual(reap(self.attempts, self.survey()), [])
        self.assertEqual((path / "stock.apk").read_bytes(), b"whose is this?")

        self.assertEqual(self.verdicts(include_orphans=True), {surveyed: None})
        self.assertEqual(
            [item.path for item in reap(self.attempts, self.survey(include_orphans=True))],
            [surveyed],
        )
        self.assertEqual(self.tree(), {})

    def test_a_workspace_younger_than_the_minimum_age_is_kept_and_later_is_not(self) -> None:
        """The same workspace, the same ledger row, two moments — only the age differs.

        The measured age is a *lower bound*: `_newest_mtime` looks one level down,
        so a stage spending an hour filling `output/` never touches the parent
        again and reads as older than it is. That is what the minimum is covering,
        and it fires before the status is even consulted — so a freshly completed
        build is refused too, which is the point.
        """
        self.reach_completed(COMPLETED_KEY)
        path = self.workspace(COMPLETED_KEY, self.leaf)
        surveyed = f"{COMPLETED_KEY}/{self.leaf}"

        young = self.survey(now=time.time())
        self.assertEqual([item.refusal for item in young], ["younger than 24.0h"])
        self.assertLess(young[0].age_seconds, DEFAULT_MIN_AGE_SECONDS)
        self.assertEqual(reap(self.attempts, young), [])
        self.assertTrue(path.is_dir())

        self.assertEqual(self.verdicts(), {surveyed: None})

    def test_the_age_refusal_is_per_workspace_and_not_a_property_of_the_sweep(self) -> None:
        """One young leaf must not save its old siblings, nor they condemn it.

        The young one is made young by pushing its mtime *forward* — the only
        direction that works, since `os.utime` resets `st_ctime` to the present
        and `_newest_mtime` takes the max of the two.
        """
        old = owner_leaf(OWNER_TOKEN)
        recent = owner_leaf("worker-still-writing")
        self.reach_completed(COMPLETED_KEY)
        self.workspace(COMPLETED_KEY, old)
        path = self.workspace(COMPLETED_KEY, recent)
        moment = aged()
        os.utime(path, (moment, moment))

        self.assertEqual(
            self.verdicts(now=moment),
            {
                f"{COMPLETED_KEY}/{old}": None,
                f"{COMPLETED_KEY}/{recent}": "younger than 24.0h",
            },
        )

    def test_neither_flag_reaches_the_other_refusal(self) -> None:
        """`--include-orphans` must not release quarantined rows, or the reverse.

        Both refusals live in the same function and both are one line, so the
        cheap wrong fix — a single `include_everything` — is invisible unless the
        two are surveyed together with one flag at a time.
        """
        self.reach_quarantined(QUARANTINED_KEY)
        self.workspace(QUARANTINED_KEY, self.leaf)
        self.workspace(ORPHAN_KEY, self.leaf)
        quarantined = f"{QUARANTINED_KEY}/{self.leaf}"
        orphan = f"{ORPHAN_KEY}/{self.leaf}"

        self.assertEqual(
            self.verdicts(include_quarantined=True),
            {quarantined: None, orphan: "no ledger row (--include-orphans)"},
        )
        self.assertEqual(
            self.verdicts(include_orphans=True),
            {quarantined: "quarantined (--include-quarantined)", orphan: None},
        )
        self.assertEqual(
            self.verdicts(include_quarantined=True, include_orphans=True),
            {quarantined: None, orphan: None},
        )


# --------------------------------------------------------- the owner-hash join


class OwnerHashJoinTests(ReaperTestCase):
    """The reason the tool joins on `sha256(owner_token)` at all.

    Since cancellation started *releasing* claims rather than quarantining them,
    a wedged key is retried, and each retry mints a fresh leaf beside the one
    before it. Those siblings are the population worth reclaiming — and they sit
    under a key whose claim is `pending`, which is the status that must never be
    swept wholesale. Only the hash separates them.
    """

    def setUp(self) -> None:
        super().setUp()
        self.dead = owner_leaf(OWNER_TOKEN)
        self.live = owner_leaf(LATER_TOKEN)

    def take_over(self) -> None:
        """The sequence an operator's recovery actually produces on disk."""
        self.begin(PENDING_KEY, OWNER_TOKEN)
        self.ledger.release_pending_operation(PENDING_KEY, OWNER_TOKEN)
        self.begin(PENDING_KEY, LATER_TOKEN)

    def test_the_superseded_sibling_goes_and_the_owners_own_leaf_stays(self) -> None:
        """Both sides of the join in one sweep, because either alone proves nothing.

        A join that always answered "owned" would keep both and reclaim nothing —
        the tool would run, report, and never free a byte. A join that never
        answered "owned" would take the live worker's workspace out from under it.
        So the two verdicts are asserted together, and then the disk is checked
        for exactly one survivor with its contents intact.
        """
        self.take_over()
        dead_path = self.workspace(PENDING_KEY, self.dead, payload=b"crashed attempt")
        live_path = self.workspace(PENDING_KEY, self.live, payload=b"being written now")

        found = {item.path: item for item in self.survey()}
        self.assertEqual(
            {path: item.refusal for path, item in found.items()},
            {
                f"{PENDING_KEY}/{self.dead}": None,
                f"{PENDING_KEY}/{self.live}": "claimed and pending",
            },
        )
        self.assertIs(found[f"{PENDING_KEY}/{self.dead}"].owned, False)
        self.assertIs(found[f"{PENDING_KEY}/{self.live}"].owned, True)

        removed = reap(self.attempts, list(found.values()))

        self.assertEqual([item.path for item in removed], [f"{PENDING_KEY}/{self.dead}"])
        self.assertFalse(dead_path.exists())
        self.assertEqual((live_path / "stock.apk").read_bytes(), b"being written now")
        self.assertEqual((live_path / "output" / "receipt.json").read_bytes(), b'{"receipt": true}')

    def test_a_leaf_that_is_not_the_owners_is_not_kept_by_being_under_a_pending_key(self) -> None:
        """The refusal is about one leaf, never about the key it sits under.

        Stated separately from the test above because the cheap way to satisfy
        that one is to keep the *first* leaf and reap the rest; here the owner's
        leaf is the only one under the key that is not reapable, among three.
        """
        self.take_over()
        for label in ("attempt-3", "attempt-4"):
            self.workspace(PENDING_KEY, owner_leaf(label))
        self.workspace(PENDING_KEY, self.live)

        refused = {path for path, why in self.verdicts().items() if why is not None}

        self.assertEqual(refused, {f"{PENDING_KEY}/{self.live}"})

    def test_a_released_claim_owns_no_leaf_and_the_age_floor_is_what_is_left(self) -> None:
        """Documented behaviour, and the sharpest edge in the module.

        A released `pending` row has an empty owner token, so no leaf matches and
        every workspace under the key becomes reapable — including the one a
        released-but-still-running worker may still be writing. `_refusal` says so
        in as many words: the age floor is what stands between them. The young
        half below is that floor doing the standing, and it is the reason this is
        acceptable rather than a hole.
        """
        self.begin(PENDING_KEY, OWNER_TOKEN)
        self.ledger.release_pending_operation(PENDING_KEY, OWNER_TOKEN)
        self.workspace(PENDING_KEY, self.dead)
        surveyed = f"{PENDING_KEY}/{self.dead}"

        self.assertEqual(self.verdicts(now=time.time()), {surveyed: "younger than 24.0h"})
        self.assertEqual(self.verdicts(), {surveyed: None})


# ------------------------------------------------------- the mistyped-path guard


class MistypedAttemptsRootTests(ReaperTestCase):
    """A root that is not an attempts root aborts the sweep before anything is removed.

    This is the failure that matters. `--attempts-root ~` or a stale path from
    another checkout has children that are not operation keys and contents that
    no content store can give back, and the difference between "abort" and "skip
    the stranger and carry on" is the difference between a refusal and a
    catastrophe that also prints a tidy summary of what it skipped.

    Every test asserts the tree is byte-for-byte what it was, not merely that an
    exception was raised. The two workspaces in `setUp` are perfectly reapable —
    proven by the last test in the class, which removes the stranger and sweeps
    them — so "nothing was removed" here is a statement and not a tautology.
    """

    def setUp(self) -> None:
        super().setUp()
        self.reach_completed(COMPLETED_KEY)
        self.reach_effect(EFFECT_KEY)
        self.workspace(COMPLETED_KEY, owner_leaf(OWNER_TOKEN))
        self.workspace(EFFECT_KEY, owner_leaf(OWNER_TOKEN))
        # The control, before any stranger exists: this tree really does sweep.
        self.assertEqual(list(self.verdicts().values()), [None, None])

    def stranger_directory(self, name: str) -> None:
        path = self.attempts / name
        path.mkdir(mode=0o700)
        os.chmod(path, 0o700)
        (path / "irreplaceable.txt").write_bytes(b"not a workspace")

    def assert_aborts_and_touches_nothing(self, stranger: str) -> None:
        before = self.tree()
        with self.assertRaises(ReaperError) as caught:
            self.survey()
        message = str(caught.exception)
        self.assertIn(f"{stranger!r} is not an operation key", message)
        self.assertIn("does not look like an attempts root", message)
        self.assertIn("Refusing to remove anything.", message)
        self.assertEqual(self.tree(), before)

    def test_a_directory_that_is_not_an_operation_key_aborts_the_whole_sweep(self) -> None:
        self.stranger_directory("Documents")
        self.assert_aborts_and_touches_nothing("Documents")

    def test_a_stranger_that_sorts_after_every_operation_key_still_aborts(self) -> None:
        """The scan is over a sorted list, so position must not decide the outcome.

        A guard that stopped at the first key it recognised, or that only checked
        `keys[0]`, passes the test above and fails this one.
        """
        self.stranger_directory("zzz-somebody-elses-directory")
        self.assert_aborts_and_touches_nothing("zzz-somebody-elses-directory")

    def test_a_stranger_that_sorts_before_every_operation_key_still_aborts(self) -> None:
        self.stranger_directory(".git")
        self.assert_aborts_and_touches_nothing(".git")

    def test_an_uppercase_key_is_not_an_operation_key(self) -> None:
        """Operation keys are `canonical_sha256` output, which is lowercase.

        A case-insensitive match would let a directory of the operator's own
        naming through, and the tool would then treat it as a key and start
        reading leaves out of it.
        """
        self.stranger_directory(COMPLETED_KEY.upper())
        self.assert_aborts_and_touches_nothing(COMPLETED_KEY.upper())

    def test_a_key_with_a_trailing_newline_is_a_stranger(self) -> None:
        """`\\Z` and not `$`, which is the one-character version of this guard failing.

        `re.match(r"[0-9a-f]{64}$", key + "\\n")` matches. Newlines are legal in
        POSIX filenames, so this is a directory an attacker — or a shell loop with
        an unquoted variable — can actually create, and under `$` it would be
        accepted as a key.
        """
        stranger = f"{COMPLETED_KEY}\n"
        self.stranger_directory(stranger)
        self.assert_aborts_and_touches_nothing(stranger)

    def test_a_hex_name_of_the_wrong_length_is_a_stranger(self) -> None:
        for label, stranger in (
            ("one short", COMPLETED_KEY[:-1]),
            ("one long", COMPLETED_KEY + "0"),
            ("empty-ish", "0"),
        ):
            with self.subTest(length=label):
                self.stranger_directory(stranger)
                self.assert_aborts_and_touches_nothing(stranger)
                os.unlink(self.attempts / stranger / "irreplaceable.txt")
                os.rmdir(self.attempts / stranger)

    def test_a_plain_file_at_the_root_aborts_the_sweep_too(self) -> None:
        """The scan is over every entry, not only the directories.

        A `README` or a stray `.tar.gz` beside the keys means this is somebody's
        working directory, and that is exactly the signal the guard exists to
        read.
        """
        (self.attempts / "notes.txt").write_bytes(b"remember to clean this up")
        self.assert_aborts_and_touches_nothing("notes.txt")

    def test_confirm_removes_nothing_when_the_root_holds_a_stranger(self) -> None:
        """End to end, through the path an operator actually types.

        The guard lives in `survey`, and `main` calls `survey` before `reap` — so
        this also pins that ordering. A `main` that reaped first and reported
        afterwards would pass every other test in this class.
        """
        self.stranger_directory("Documents")
        before = self.tree()

        status, out, err = self.run_cli("--confirm")

        self.assertEqual(status, 2)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("refused: "), err)
        self.assertIn("'Documents' is not an operation key", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.tree(), before)

    def test_without_the_stranger_the_same_sweep_reaps_both_workspaces(self) -> None:
        """The positive control the whole class rests on.

        Every assertion above is "nothing was removed". They mean nothing unless
        this tree, one stranger lighter, is swept to nothing by the same call.
        """
        self.stranger_directory("Documents")
        with self.assertRaises(ReaperError):
            self.survey()

        os.unlink(self.attempts / "Documents" / "irreplaceable.txt")
        os.rmdir(self.attempts / "Documents")

        found = self.survey()
        self.assertEqual(len(reap(self.attempts, found)), 2)
        self.assertEqual(self.tree(), {})


# ------------------------------------------------------------------ the floor


class AgeFloorTests(ReaperTestCase):
    """`--min-age` has a floor, and the floor is derived rather than chosen."""

    def test_a_minimum_age_below_the_floor_is_refused_and_nothing_is_surveyed(self) -> None:
        """An operator who wants a directory gone sooner wants `rm`, not this.

        Refused before the root is even opened, so the answer does not depend on
        the tree: there is no argument from "but this particular workspace is
        obviously finished" that this option is allowed to accept.
        """
        self.reach_completed(COMPLETED_KEY)
        self.workspace(COMPLETED_KEY, owner_leaf(OWNER_TOKEN))
        before = self.tree()

        with self.assertRaises(ReaperError) as caught:
            self.survey(min_age_seconds=MIN_AGE_FLOOR_SECONDS - 1)

        self.assertEqual(
            str(caught.exception),
            f"Minimum age {MIN_AGE_FLOOR_SECONDS - 1:.0f}s is below the "
            f"{MIN_AGE_FLOOR_SECONDS}s floor: a workspace that young may still "
            "be being written by a stage that has not finished",
        )
        self.assertEqual(self.tree(), before)

    def test_exactly_the_floor_is_allowed(self) -> None:
        """The boundary, and the positive control for the refusal above.

        A guard written `<=` would refuse the documented minimum itself, and
        every other test in this class would still pass.
        """
        self.reach_completed(COMPLETED_KEY)
        self.workspace(COMPLETED_KEY, owner_leaf(OWNER_TOKEN))

        found = self.survey(min_age_seconds=MIN_AGE_FLOOR_SECONDS)

        self.assertEqual([item.refusal for item in found], [None])

    def test_the_cli_refuses_a_minimum_age_below_the_floor(self) -> None:
        status, out, err = self.run_cli("--min-age-seconds", "0", "--confirm")
        self.assertEqual(status, 2)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("refused: "), err)
        self.assertIn("below the", err)

    def test_the_floor_is_the_longest_a_stage_may_run_and_matches_the_workers_grace(
        self,
    ) -> None:
        """`MIN_AGE_FLOOR_SECONDS` and `DEFAULT_GRACEFUL_SHUTDOWN_SECONDS` are one number.

        Both are "the longest a replay stage may legitimately run", reached from
        opposite ends: the worker waits that long for a stage to finish before
        shutting down, and the reaper refuses to touch anything younger for the
        same reason. They live in different modules with no import between them,
        so nothing but this assertion stops one being raised — because a stage got
        slower — while the other silently keeps letting a live workspace through.
        """
        self.assertEqual(MIN_AGE_FLOOR_SECONDS, worker.DEFAULT_GRACEFUL_SHUTDOWN_SECONDS)
        self.assertEqual(
            MIN_AGE_FLOOR_SECONDS,
            reaper._REFERENCE_PLAN_TIMEOUT_SECONDS * LONGEST_STAGE_BUDGET_MULTIPLIER,
        )
        # And the default is above the floor, not equal to it: the default is a
        # generous choice, the floor is a hard limit, and collapsing the two would
        # make lowering the default look free.
        self.assertGreater(DEFAULT_MIN_AGE_SECONDS, MIN_AGE_FLOOR_SECONDS)


# -------------------------------------------------------------- the refusal channel


class RefusalChannelTests(ReaperTestCase):
    """Every way this tool says no must be `ReaperError`, and `refused: …` at the CLI.

    The gap this closes has shipped in this tree before. `open_private_root` and
    `open_private_directory` raise a bare `ValueError("Unsafe … directory")` on
    any root the caller does not own or that is group-readable — which is the
    ordinary way a real attempts root goes wrong, not an exotic one — and a
    `ValueError` reaching `main` uncaught is a traceback where an operator
    expected a sentence. A traceback-as-refusal teaches whoever is on call to
    skim tracebacks, and the next one will be a real crash.
    """

    def setUp(self) -> None:
        super().setUp()
        self.reach_completed(COMPLETED_KEY)
        self.workspace(COMPLETED_KEY, owner_leaf(OWNER_TOKEN))

    def test_a_group_readable_attempts_root_is_a_refusal_not_a_bare_value_error(self) -> None:
        os.chmod(self.attempts, 0o755)
        self.addCleanup(os.chmod, self.attempts, 0o700)

        with self.assertRaises(ReaperError) as caught:
            self.survey()

        self.assertEqual(
            str(caught.exception),
            f"cannot survey {self.attempts}: ValueError: Unsafe attempts root directory",
        )

    def test_a_group_readable_operation_directory_is_a_refusal_too(self) -> None:
        """`reap` and `survey` each open two levels, and both levels are validated.

        Stated separately because the root is opened by `open_private_root` and
        the key directory by `open_private_directory` — two functions, two call
        sites, and only one of them is exercised by the test above.
        """
        key_directory = self.attempts / COMPLETED_KEY
        os.chmod(key_directory, 0o755)
        self.addCleanup(os.chmod, key_directory, 0o700)

        with self.assertRaises(ReaperError) as caught:
            self.survey()

        self.assertIn("ValueError: Unsafe operation directory", str(caught.exception))

    def test_reap_refuses_a_group_readable_root_rather_than_raising_out_of_it(self) -> None:
        """`reap` has its own refusal wrapper, and it is the one that deletes.

        The permissions can change between the survey and the confirm — that is
        the whole reason `--confirm` is a second invocation — so the removal path
        must refuse in its own right and not rely on the survey having passed.
        """
        found = self.survey()
        self.assertEqual([item.refusal for item in found], [None])
        os.chmod(self.attempts, 0o755)
        self.addCleanup(os.chmod, self.attempts, 0o700)
        before = self.tree()

        with self.assertRaises(ReaperError) as caught:
            reap(self.attempts, found)

        self.assertEqual(
            str(caught.exception),
            f"cannot reap {self.attempts}: ValueError: Unsafe attempts root directory",
        )
        self.assertEqual(self.tree(), before)

    def test_a_missing_attempts_root_names_the_path_and_creates_nothing(self) -> None:
        """The likeliest mistake, and the one a `mkdir -p` sweeper hides.

        `open_private_root` deliberately does not create what it was handed; a
        reaper that created the directory would report cheerfully that it found
        nothing to remove, which is indistinguishable from a clean tree.
        """
        absent = self.state / "not-the-attempts-root"

        with self.assertRaises(ReaperError) as caught:
            reaper.survey(absent, self.ledger_path)

        self.assertEqual(str(caught.exception), f"No attempts root at {absent}")
        self.assertFalse(absent.exists())

    def test_a_missing_ledger_names_the_path_and_creates_nothing(self) -> None:
        """The other half of a wrong `--state-root`, and it must not invent a ledger.

        An empty ledger has no claims, so every workspace becomes an orphan — and
        with `--include-orphans` an invented ledger would authorise deleting the
        entire tree.
        """
        absent = self.state / "other" / "ledger.sqlite3"

        with self.assertRaises(ReaperError) as caught:
            reaper.survey(self.attempts, absent)

        self.assertEqual(str(caught.exception), f"No ledger at {absent}")
        self.assertFalse(absent.parent.exists())

    def test_the_cli_prints_refused_on_stderr_and_exits_two(self) -> None:
        """Exit code and stream, because a wrapper script reads them and a human does not.

        Exit 2 rather than 1, and stdout empty: a half-printed report above an
        error reads like something happened.
        """
        os.chmod(self.attempts, 0o755)
        self.addCleanup(os.chmod, self.attempts, 0o700)

        status, out, err = self.run_cli()

        self.assertEqual(status, 2)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("refused: "), err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(err.count("\n"), 1, err)

    def test_a_plain_dry_run_exits_zero_and_removes_nothing(self) -> None:
        """The default is a report, over a tree that would otherwise be swept.

        Run at the aged clock on purpose: the report says the workspace is
        reapable, and the workspace is still there afterwards. A dry run over a
        tree with nothing reapable in it would pass with `--confirm` wired to
        always-on.
        """
        before = self.tree()

        status, out, err = self.run_cli()

        self.assertEqual((status, err), (0, ""))
        self.assertIn(COMPLETED_KEY, out)
        self.assertIn("REAP", out)
        self.assertIn("1 of 1 workspaces reapable.", out)
        self.assertIn("Re-run with --confirm to remove them.", out)
        self.assertEqual(self.tree(), before)

    def test_confirm_reports_what_it_removed_and_removes_it(self) -> None:
        """The positive control for the dry run: the same argv plus one flag."""
        status, out, err = self.run_cli("--confirm")

        self.assertEqual((status, err), (0, ""))
        self.assertIn("Removed 1 workspace(s).", out)
        self.assertEqual(self.tree(), {})

    def test_a_dry_run_at_the_real_clock_reports_nothing_reapable(self) -> None:
        """The age floor reaching the CLI, where no `now` parameter exists.

        `main` reads the wall clock, so a freshly built workspace must be refused
        through the tool an operator actually runs — not only through the API a
        test can hand a moment to.
        """
        status, out, err = self.run_cli("--confirm", moment=time.time())

        self.assertEqual((status, err), (0, ""))
        self.assertIn("0 of 1 workspaces reapable.", out)
        self.assertNotIn("Re-run with --confirm", out)
        self.assertIn("Removed 0 workspace(s).", out)
        self.assertTrue((self.attempts / COMPLETED_KEY / owner_leaf(OWNER_TOKEN)).is_dir())


# ------------------------------------------------- reap follows the survey exactly


class ReapFollowsTheSurveyTests(ReaperTestCase):
    """What is printed and what is removed must be the same set, or the report lies.

    `reap` takes the surveyed list rather than re-deciding from the ledger, so an
    operator who reads the dry run and then types `--confirm` gets exactly what
    they read. These tests hand it lists it would never have produced itself,
    which is the only way to tell "follows the list" from "happens to agree".
    """

    def setUp(self) -> None:
        super().setUp()
        self.leaf = owner_leaf(OWNER_TOKEN)
        self.reach_completed(COMPLETED_KEY)
        self.reach_completed(EFFECT_KEY)
        self.kept = self.workspace(COMPLETED_KEY, self.leaf, payload=b"do not touch")
        self.swept = self.workspace(EFFECT_KEY, self.leaf, payload=b"take this one")

    def test_a_workspace_marked_not_reapable_survives_a_reap_of_its_neighbours(self) -> None:
        """Both are reapable by the rules; one is marked kept by hand.

        The neighbour is the control. A `reap` that removed nothing at all would
        satisfy the survival assertion on its own.
        """
        found = [
            dataclasses.replace(item, refusal="an operator changed their mind")
            if item.operation_key == COMPLETED_KEY
            else item
            for item in self.survey()
        ]

        removed = reap(self.attempts, found)

        self.assertEqual([item.path for item in removed], [f"{EFFECT_KEY}/{self.leaf}"])
        self.assertEqual((self.kept / "stock.apk").read_bytes(), b"do not touch")
        self.assertEqual(
            (self.kept / "output" / "receipt.json").read_bytes(), b'{"receipt": true}'
        )
        self.assertFalse(self.swept.exists())

    def test_a_list_with_nothing_reapable_in_it_removes_nothing_and_returns_nothing(
        self,
    ) -> None:
        found = [dataclasses.replace(item, refusal="kept") for item in self.survey()]
        before = self.tree()

        self.assertEqual(reap(self.attempts, found), [])
        self.assertEqual(self.tree(), before)

    def test_reap_does_not_re_derive_the_verdict_from_the_ledger(self) -> None:
        """The sharp form: a live `pending` leaf marked reapable by hand *is* removed.

        Not an invitation to do that — it is the proof that `reap` has exactly one
        source of truth. If it re-read the ledger it would disagree with the
        report an operator approved, and the two-step `--confirm` workflow would
        be meaningless: what you read is not what would go.
        """
        self.begin(PENDING_KEY)
        live = self.workspace(PENDING_KEY, self.leaf)
        found = [item for item in self.survey() if item.operation_key == PENDING_KEY]
        self.assertEqual([item.refusal for item in found], ["claimed and pending"])

        removed = reap(self.attempts, [dataclasses.replace(found[0], refusal=None)])

        self.assertEqual([item.path for item in removed], [f"{PENDING_KEY}/{self.leaf}"])
        self.assertFalse(live.exists())
        # And the ledger is untouched by any of it: this tool reads that database
        # through `mode=ro` and must never be the thing that changed a claim.
        self.assertEqual(self.ledger.operation_status(PENDING_KEY), "pending")


# ------------------------------------------------- the operation-key directories


class EmptyOperationDirectoryTests(ReaperTestCase):
    """A key directory that has lost its last leaf is itself garbage.

    Leaving thousands of empty directories behind would make the next sweep's
    report unreadable and the reclaim look like it had not worked. But the rule
    is "empty", not "swept": a key whose owner is still writing must keep its
    directory, or the live worker's next path lookup lands somewhere new.
    """

    def setUp(self) -> None:
        super().setUp()
        self.leaf = owner_leaf(OWNER_TOKEN)

    def test_a_key_directory_goes_when_its_last_leaf_goes(self) -> None:
        self.reach_completed(COMPLETED_KEY)
        self.workspace(COMPLETED_KEY, self.leaf)
        self.workspace(COMPLETED_KEY, owner_leaf("worker-earlier-attempt"))

        reap(self.attempts, self.survey())

        self.assertFalse((self.attempts / COMPLETED_KEY).exists())
        self.assertEqual(self.tree(), {})

    def test_a_key_directory_with_a_kept_leaf_is_not_removed(self) -> None:
        """The owner's leaf survives, so the directory holding it must survive too.

        Under one key, so this is not "the reaper left some directory alone" but
        "the reaper removed a sibling and stopped". An `rmdir` reached
        unconditionally after the loop raises `ENOTEMPTY` here, which becomes a
        `ReaperError` — a sweep that refuses halfway through, having already
        deleted.
        """
        self.begin(PENDING_KEY, LATER_TOKEN)
        superseded = self.workspace(PENDING_KEY, self.leaf)
        live = self.workspace(PENDING_KEY, owner_leaf(LATER_TOKEN))

        removed = reap(self.attempts, self.survey())

        self.assertEqual([item.name for item in removed], [self.leaf])
        self.assertFalse(superseded.exists())
        self.assertTrue((self.attempts / PENDING_KEY).is_dir())
        self.assertEqual((live / "stock.apk").read_bytes(), b"several gigabytes")

    def test_a_key_directory_holding_something_the_survey_never_listed_is_not_removed(
        self,
    ) -> None:
        """The survey lists directories; the emptiness check counts everything.

        A stray file beside the leaves — a log, a lock, a half-written marker — is
        not a workspace and is not surveyed, and it must still stop the `rmdir`.
        Counting only what was surveyed would make the removal succeed on a
        directory that was not empty, or fail the whole sweep on `ENOTEMPTY`.
        """
        self.reach_completed(COMPLETED_KEY)
        self.workspace(COMPLETED_KEY, self.leaf)
        (self.attempts / COMPLETED_KEY / "attempt.log").write_bytes(b"stage output\n")

        removed = reap(self.attempts, self.survey())

        self.assertEqual(len(removed), 1)
        self.assertEqual(
            self.tree(),
            {COMPLETED_KEY: None, f"{COMPLETED_KEY}/attempt.log": b"stage output\n"},
        )

    def test_a_key_whose_leaves_are_all_kept_is_never_opened_for_removal(self) -> None:
        """Nothing reapable under a key means the key is not touched at all."""
        self.reach_quarantined(QUARANTINED_KEY)
        self.workspace(QUARANTINED_KEY, self.leaf)
        before = self.tree()

        self.assertEqual(reap(self.attempts, self.survey()), [])
        self.assertEqual(self.tree(), before)


# ------------------------------------------------------------------- the report


class SurveyReportTests(ReaperTestCase):
    """The dry run is what an operator approves, so it has to name every workspace.

    A report that summarised — "3 reapable" with no list — would be approved
    without being read, and the two-step `--confirm` workflow would be a
    formality.
    """

    def test_every_workspace_appears_under_its_key_with_a_verdict(self) -> None:
        leaf = owner_leaf(OWNER_TOKEN)
        self.reach_completed(COMPLETED_KEY)
        self.reach_quarantined(QUARANTINED_KEY)
        self.workspace(COMPLETED_KEY, leaf)
        self.workspace(QUARANTINED_KEY, leaf)

        described = describe_survey(self.survey())

        self.assertIn(COMPLETED_KEY, described)
        self.assertIn(QUARANTINED_KEY, described)
        self.assertIn(leaf[:16], described)
        self.assertIn("REAP", described)
        self.assertIn("keep  quarantined (--include-quarantined)", described)
        self.assertIn("1 of 2 workspaces reapable.", described)
        self.assertIn("Re-run with --confirm to remove them.", described)

    def test_a_report_with_nothing_reapable_does_not_offer_the_confirm_flag(self) -> None:
        """Offering `--confirm` where it would do nothing trains an operator to type it."""
        self.begin(PENDING_KEY)
        self.workspace(PENDING_KEY, owner_leaf(OWNER_TOKEN))

        described = describe_survey(self.survey())

        self.assertIn("0 of 1 workspaces reapable.", described)
        self.assertNotIn("--confirm", described)
        self.assertIn("keep  claimed and pending", described)


# ------------------------------------------------------------------ known defects


class ReadOnlySourceTreeTests(ReaperTestCase):
    """The one thing 49 tests and 11 mutations all missed.

    Every fixture in this file builds its workspaces at ``0o700``, because that is
    what `_exclusive_directory` does — and a real attempt workspace contains one
    directory tree that a *different* module wrote. `source_admission` publishes
    `admitted-source/` with **read-only directories**, deliberately, and unlinking
    a file needs write permission on its parent rather than on the file. So the
    first real sweep removed two of five workspaces and died on
    `SettingsWrapper.smali`; a plain `rm -rf` fails identically.

    A fixture that mirrors the module under test can only ever produce the
    permissions that module produces. This one is built the way production builds
    it, which is the whole point of the class.
    """

    def admitted_source(self, workspace: Path) -> Path:
        """A read-only source tree, the shape `source_admission` publishes.

        Directories at ``0o500`` and files at ``0o444`` — write-protected against
        accident, not against their owner, which is exactly why the remover is
        allowed to restore write and why it must.
        """
        tree = workspace / "admitted-source" / "dfinsta_source_430" / "newCode"
        tree.mkdir(parents=True)
        (tree / "SettingsWrapper.smali").write_bytes(b".class public LSettingsWrapper;\n")
        (tree / "hooks.smali").write_bytes(b".class public Lhooks;\n")
        for path in (tree / "SettingsWrapper.smali", tree / "hooks.smali"):
            os.chmod(path, 0o444)
        # Innermost first: a parent chmodded to 0o500 first would block the rest.
        for directory in sorted(
            (workspace / "admitted-source").rglob("*"), reverse=True
        ):
            if directory.is_dir():
                os.chmod(directory, 0o500)
        os.chmod(workspace / "admitted-source", 0o500)
        return tree

    def test_a_workspace_holding_a_read_only_source_tree_is_removed(self):
        """The exact failure, as a test. Was `PermissionError`, is now gone."""
        self.reach_completed(COMPLETED_KEY)
        workspace = self.workspace(COMPLETED_KEY, owner_leaf(OWNER_TOKEN))
        self.admitted_source(workspace)

        found = reaper.survey(self.attempts, self.ledger_path, now=aged())
        self.assertEqual([w.reapable for w in found], [True])

        removed = reap(self.attempts, found)

        self.assertEqual(len(removed), 1)
        self.assertEqual(self.tree(), {})

    def test_the_control_is_that_the_directories_really_were_unwritable(self):
        """POSITIVE CONTROL: without it, the test above proves nothing.

        If the fixture's chmod silently failed — wrong order, umask, a
        `Path.mkdir(mode=...)` that does not apply to parents — the removal would
        succeed for the ordinary reason and the guard it is meant to exercise
        would never run.
        """
        self.reach_completed(COMPLETED_KEY)
        workspace = self.workspace(COMPLETED_KEY, owner_leaf(OWNER_TOKEN))
        tree = self.admitted_source(workspace)

        self.assertFalse(os.stat(tree).st_mode & stat.S_IWUSR, "fixture is writable")
        with self.assertRaises(PermissionError):
            (tree / "planted.smali").write_bytes(b"x")

    def test_the_live_cleanup_path_does_not_widen_permissions(self):
        """`restore_write` is opt-in, and the Activity cleanup must not opt in.

        That path removes only `validate-` workspaces this module created at
        ``0o700``, so it never needs the relaxation — and a remover that quietly
        widens permissions everywhere is not what a live stage wants near its own
        descriptors.
        """
        source = (
            Path(__file__).resolve().parents[1] / "src/dfinsta_pipeline/activities.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        remover = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_secure_remove_tree_entry"
        )
        names = [keyword.arg for keyword in remover.args.kwonlyargs]
        self.assertIn("restore_write", names)

        # The DEFAULT, not just the parameter. Checking only that the option
        # exists let a mutation flip it to `True` and pass every test in this
        # file — opt-in is the property, and an opt-in whose default is on is
        # not one.
        default = remover.args.kw_defaults[names.index("restore_write")]
        self.assertIsInstance(default, ast.Constant)
        self.assertIs(default.value, False)

        # The workspace remover opts in; `_remove_private_workspace` does not.
        self.assertIn("restore_write=True", source)
        private = source[source.index("def _remove_private_workspace") :][:400]
        self.assertNotIn("restore_write", private)


class ClosedDefectTests(ReaperTestCase):
    """Two things the module promised and did not do until 2026-08-05.

    Both were written as `expectedFailure` first, so the defect was on the record
    and executable before it was fixed, and the fix announced itself as an
    unexpected success rather than as a test somebody remembered to write.
    """

    def test_a_ledger_that_is_not_a_database_reaches_the_operator_as_a_refusal(self) -> None:
        """`_claims` ran outside `_refusing`, so `sqlite3` escaped the refusal channel.

        `survey` called `_claims` one line above the
        `with _refusing(...)` block — and `_refusing` caught only `OSError` and
        `ValueError` anyway, while `main` catches only `ReaperError`. So a
        `--ledger` pointing at a file that is not a SQLite database, or at a state
        directory too locked down for the WAL sidecars, comes out of
        `Ledger(read_only=True)` as `sqlite3.DatabaseError` and reached the
        operator as a traceback with exit 1 instead of `refused: …` with exit 2.

        Neither of those inputs is exotic: they are the two shapes a wrong
        `--ledger` takes. And this is precisely the gap `_refusing`'s own
        docstring says the module exists to close — a refusal channel is only a
        channel if everything uses it. Closed by moving the `_claims` call inside
        the block and adding `sqlite3.Error` to what it catches.
        """
        self.reach_completed(COMPLETED_KEY)
        self.workspace(COMPLETED_KEY, owner_leaf(OWNER_TOKEN))
        not_a_ledger = self.state / "notes.txt"
        not_a_ledger.write_bytes(b"this is a note, not a ledger\n")

        with self.assertRaises(ReaperError):
            reaper.survey(self.attempts, not_a_ledger)

    def test_reap_refuses_an_operation_key_that_is_not_an_operation_key(self) -> None:
        """`reap` did not re-apply the guard, so a bad key walked out of the root.

        `_survey` checks every name against `_OPERATION_KEY`; `_reap` checked
        nothing. It opens `open_private_directory(root_fd, workspace.operation_key)`
        and removes `workspace.name` inside it — so a `Workspace` whose key is
        `..` removes a directory *outside* the attempts root, which is the exact
        outcome the module's headline promise ("it will not run against a
        directory that is not an attempts root") is about.

        It was not reachable through `main`, because `main` always surveys first
        and the survey only produces guard-checked keys. But `reap` is in
        `__all__`, the two functions are separately callable by design, and the
        guard that made one of them safe was not in the one that deletes.
        `_validate_target` now runs over every reapable workspace *before* the
        root is opened, so a bad name refuses the whole call rather than being
        reached after earlier removals have already happened.

        The state root is chmodded to 0o700 first because that is what production
        looks like — `_open_or_create_directory` mkdirs every component of
        `attempts_root` at 0o700 — and because it is the only thing standing in
        the way here: on a 0o755 parent the escape is stopped by
        `_validate_private_directory` rather than by any rule of this module's,
        which would make the test pass for a reason that does not generalise.

        Sandboxed inside this test's temporary directory: the victim is a sibling
        of the attempts root, so the escape it demonstrates cannot reach anything
        the fixture does not own.
        """
        os.chmod(self.state, 0o700)
        victim = self.state / "victim"
        victim.mkdir(mode=0o700)
        os.chmod(victim, 0o700)
        (victim / "irreplaceable.txt").write_bytes(b"a home directory, notionally")
        escape = Workspace("..", "victim", "completed", OPERATION_KIND, False, False, 1e9, None)

        with self.assertRaises(ReaperError):
            reap(self.attempts, [escape])
        self.assertTrue(victim.is_dir())


if __name__ == "__main__":
    unittest.main()

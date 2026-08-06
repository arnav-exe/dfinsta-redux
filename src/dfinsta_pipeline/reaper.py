"""Remove attempt workspaces whose work is already banked in the content store.

Every replay stage builds a private workspace under

    <attempts_root>/<operation_key>/<sha256(owner_token)>/

and **nothing has ever removed one**. A build workspace holds a stock APK, an
intermediate APK, a patched APK and a materialized decoded tree; a verify
workspace holds two APKs, a framework cache, an admitted source tree and two
more decoded trees. One port leaves several gigabytes behind, and the only
cleanup anywhere in the tree is the `validate-` workspace that
`_validate_replay_final_apk_verification_receipt` removes in its own `finally`.

This became more pressing rather than less on 2026-08-05. Until then a cancelled
stage quarantined its operation, so a wedged run mostly did not retry; now
cancellation *releases* the claim and a later attempt takes it over — which is
the fix working, and which means retries now happen where they previously could
not, each minting a fresh leaf directory beside the one before it.

===============================================================================
  WHY DELETING A FINISHED WORKSPACE LOSES NOTHING
===============================================================================

Every stage publishes its outputs to the content store **before** the ledger
claim leaves `pending`: the tree capture or `put_bytes` runs, then
`record_effect`, then `complete_operation`. So a claim at `effect` or
`completed` is a claim whose workspace is fully redundant — verified by reading
the ordering at each site rather than by assuming it, e.g. the apply stage
captures at `activities.py:2142`, publishes at `:2175`, records the effect at
`:2190`.

No adoption path reads a stale workspace either. When a completed operation is
re-validated, `_validate_replay_final_apk_verification_receipt` materializes a
*fresh* `validate-` workspace out of the store; it never looks at the directory
the original attempt left.

`pending` and `quarantined` are different, and are refused by default. A
`pending` workspace may hold executor output that was never captured, and a
`quarantined` one is the only surviving record of why a fail-closed check
refused. Both are *reproducible* — the operation key is a hash of the full input
— but neither is *recoverable*.

===============================================================================
  WHAT THIS WILL NOT DO
===============================================================================

**It will not run against a directory that is not an attempts root.** Every
child of the root must be a 64-hex operation key. One that is not aborts the
whole sweep before anything is removed, rather than skipping it — a mistyped
`--attempts-root` is the failure that matters here, and a tool that half-deletes
an unfamiliar directory and then reports what it skipped has already done the
damage.

**It will not remove the live workspace of a claimed operation.** A `pending`
row names its owner, and `sha256(owner_token)` is the leaf that owner is
writing.

**It will not remove anything younger than the longest a stage may run.** The
age it measures is the newest mtime of the workspace directory and its direct
children, which is a *lower bound* on how recently something was written: a
stage that spends an hour filling `output/` does not touch the parent again.
`MIN_AGE_FLOOR_SECONDS` covers that gap, derived from the largest stage budget
rather than picked, and `--min-age-seconds` cannot go below it. An operator who wants a
directory gone sooner than a stage could still be using it does not want this
tool; they want `rm`.

**It will not delete anything without `--confirm`.** The default is a report.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sqlite3
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .activities import (
    LONGEST_STAGE_BUDGET_MULTIPLIER,
    open_private_directory,
    open_private_root,
    remove_attempt_workspace,
)
from .ledger import Ledger

__all__ = [
    "ReaperError",
    "Workspace",
    "survey",
    "reap",
    "describe_survey",
    "MIN_AGE_FLOOR_SECONDS",
    "DEFAULT_MIN_AGE_SECONDS",
    "main",
]


class ReaperError(RuntimeError):
    """Raised when a sweep cannot be performed safely."""


@contextmanager
def _refusing(what: str) -> Iterator[None]:
    """Turn a filesystem or validation failure into this module's refusal.

    Without this the tool has a documented contract (`refused: …`, exit 2) and a
    second undocumented one: a traceback out of `_validate_private_directory`,
    which fires on any directory the caller does not own or that is
    group-readable — the ordinary way a real attempts root goes wrong. The
    feature-gate client shipped exactly this gap and the lesson from it is that a
    refusal channel is only a channel if everything uses it.

    `ReaperError` passes through unwrapped so its own message is not buried.
    """

    try:
        yield
    except ReaperError:
        raise
    except (OSError, ValueError, sqlite3.Error) as error:
        raise ReaperError(f"{what}: {type(error).__name__}: {error}") from error


#: An operation key is `canonical_sha256(...)` and nothing else appears at this
#: level of the tree.
_OPERATION_KEY = re.compile(r"\A[0-9a-f]{64}\Z")

#: The leaf a stage writes is `sha256(owner_token)`; the leaf the adoption-path
#: validator writes is the same digest behind a marker. Both are matched, and the
#: marker is kept because the two are removed under different circumstances --
#: the validator's is transient and its presence at all means a crash.
_VALIDATION_PREFIX = "validate-"

#: A reference subprocess timeout, in seconds. The real budget is per-run
#: (`plan.timeout_seconds * _STAGE_BUDGET_MULTIPLIER[stage]`), and this module has
#: no run to read it from -- so the floor uses the largest decode-plan timeout the
#: shipped toolchain profiles declare. `worker.DEFAULT_GRACEFUL_SHUTDOWN_SECONDS`
#: is the same product for the same reason, and a test pins the two together so
#: neither can drift alone.
_REFERENCE_PLAN_TIMEOUT_SECONDS = 600

#: No workspace younger than the longest a stage may legitimately run is ever
#: removed, whatever `--min-age-seconds` says.
MIN_AGE_FLOOR_SECONDS = _REFERENCE_PLAN_TIMEOUT_SECONDS * LONGEST_STAGE_BUDGET_MULTIPLIER

#: A day. Generous because the cost of keeping a workspace one more day is disk
#: and the cost of removing one an operator was about to inspect is the
#: investigation.
DEFAULT_MIN_AGE_SECONDS = 86_400

#: Claim statuses whose workspace is fully redundant with the content store.
BANKED = frozenset({"effect", "completed"})


@dataclass(frozen=True)
class Workspace:
    """One attempt workspace, with the ledger's view of it and a verdict."""

    operation_key: str
    name: str
    #: The claim status, or None when the ledger has no row for this key at all.
    status: str | None
    kind: str | None
    #: True when this leaf is the one the claim's current owner is writing.
    owned: bool
    #: True for the transient workspace the adoption-path validator builds.
    validation: bool
    age_seconds: float
    #: None when the workspace is reapable; otherwise why it was refused.
    refusal: str | None

    @property
    def reapable(self) -> bool:
        return self.refusal is None

    @property
    def path(self) -> str:
        return f"{self.operation_key}/{self.name}"

    def describe(self) -> str:
        age = f"{self.age_seconds / 3600:.1f}h"
        # `owned` and `validation` are shown rather than folded into the verdict
        # because each answers a question an operator asks before agreeing to a
        # sweep: "is a worker writing this right now?" and "why is there a
        # validation workspace at all?" -- the validator removes its own in a
        # `finally`, so one that survived means a crash mid-validation.
        status = self.status or "(no ledger row)"
        marks = ("owner " if self.owned else "") + ("validate " if self.validation else "")
        verdict = "REAP" if self.reapable else f"keep  {self.refusal}"
        return f"  {self.name[:16]}…  {status:<12} {age:>7}  {marks}{verdict}"


def _newest_mtime(parent_fd: int, name: str) -> float:
    """The newest mtime of a workspace directory or any of its direct children.

    One level deep on purpose. A full walk of a materialized decoded tree is
    200,000 stats to refine a number `MIN_AGE_FLOOR_SECONDS` already covers, and
    a sweeper that takes minutes per candidate is one nobody runs.
    """

    descriptor = open_private_directory(parent_fd, name, "attempt workspace")
    try:
        metadata = os.fstat(descriptor)
        newest = max(metadata.st_mtime, metadata.st_ctime)
        with os.scandir(descriptor) as entries:
            for entry in entries:
                child = entry.stat(follow_symlinks=False)
                newest = max(newest, child.st_mtime, child.st_ctime)
        return newest
    finally:
        os.close(descriptor)


def _claims(ledger_path: Path) -> dict[str, tuple[str, str, str]]:
    """`operation_key -> (status, kind, owner_token)` for every recorded claim.

    Read through `Ledger(read_only=True)` for the same reason `claims.py` does:
    `mode=ro` makes "this tool cannot write your ledger" a property of the
    database handle rather than a promise in a docstring. A reaper deletes
    files; it should not be able to touch the record that says what they were.
    """

    if not ledger_path.is_file():
        raise ReaperError(f"No ledger at {ledger_path}")
    reader = Ledger(ledger_path, read_only=True)
    with Ledger._connection(reader) as connection:
        rows = connection.execute(
            "SELECT operation_key, status, kind, owner_token FROM operation_claims"
        ).fetchall()
    return {key: (status, kind, owner) for key, status, kind, owner in rows}


def _refusal(
    *,
    status: str | None,
    owned: bool,
    age_seconds: float,
    min_age_seconds: float,
    include_quarantined: bool,
    include_orphans: bool,
) -> str | None:
    if age_seconds < min_age_seconds:
        return f"younger than {min_age_seconds / 3600:.1f}h"
    if status is None:
        return None if include_orphans else "no ledger row (--include-orphans)"
    if status == "quarantined":
        return None if include_quarantined else "quarantined (--include-quarantined)"
    if status == "pending":
        # A pending claim's own leaf is live. Its siblings are attempts that
        # crashed or were released, and the age floor is what stands between a
        # released-but-still-running owner and its workspace disappearing.
        return "claimed and pending" if owned else None
    if status in BANKED:
        # The validator's workspace is transient and removes itself; one that
        # survived means a crash mid-validation, and it is as redundant as the
        # rest -- it was materialized out of the store in the first place.
        return None
    return f"unrecognised status {status!r}"


def survey(
    attempts_root: Path | str,
    ledger_path: Path | str,
    *,
    min_age_seconds: float = DEFAULT_MIN_AGE_SECONDS,
    include_quarantined: bool = False,
    include_orphans: bool = False,
    now: float | None = None,
) -> list[Workspace]:
    """Every attempt workspace under `attempts_root`, with a verdict on each.

    Read-only. Nothing is removed here, which is what makes the report worth
    printing before `--confirm`.
    """

    if min_age_seconds < MIN_AGE_FLOOR_SECONDS:
        raise ReaperError(
            f"Minimum age {min_age_seconds:.0f}s is below the "
            f"{MIN_AGE_FLOOR_SECONDS}s floor: a workspace that young may still "
            "be being written by a stage that has not finished"
        )
    root = Path(attempts_root)
    if not root.is_dir():
        raise ReaperError(f"No attempts root at {root}")
    moment = time.time() if now is None else now

    with _refusing(f"cannot survey {root}"):
        # Inside the block, not above it. A `--ledger` naming a file that is not
        # a SQLite database is one of the two ordinary ways that flag goes wrong,
        # and reading it one line early sent `sqlite3.DatabaseError` straight past
        # the refusal channel to the operator as a traceback.
        claims = _claims(Path(ledger_path))
        return _survey(
            root,
            claims,
            moment,
            min_age_seconds,
            include_quarantined=include_quarantined,
            include_orphans=include_orphans,
        )


def _survey(
    root: Path,
    claims: dict[str, tuple[str, str, str]],
    moment: float,
    min_age_seconds: float,
    *,
    include_quarantined: bool,
    include_orphans: bool,
) -> list[Workspace]:
    root_fd = open_private_root(root, "attempts root")
    try:
        with os.scandir(root_fd) as entries:
            keys = sorted(entry.name for entry in entries)
        # Abort on the FIRST unrecognised name rather than skipping it. This is
        # the mistyped-path guard, and a guard that reports what it skipped after
        # deleting everything else has not guarded anything.
        stranger = next((name for name in keys if not _OPERATION_KEY.match(name)), None)
        if stranger is not None:
            raise ReaperError(
                f"{root} does not look like an attempts root: {stranger!r} is not "
                "an operation key. Refusing to remove anything."
            )
        found: list[Workspace] = []
        for key in keys:
            key_fd = open_private_directory(root_fd, key, "operation")
            try:
                with os.scandir(key_fd) as entries:
                    names = sorted(
                        entry.name for entry in entries if entry.is_dir(follow_symlinks=False)
                    )
                status, kind, owner_token = claims.get(key, (None, None, ""))
                owner_leaf = (
                    hashlib.sha256(owner_token.encode("utf-8")).hexdigest()
                    if owner_token
                    else None
                )
                for name in names:
                    validation = name.startswith(_VALIDATION_PREFIX)
                    digest = name[len(_VALIDATION_PREFIX) :] if validation else name
                    age = moment - _newest_mtime(key_fd, name)
                    found.append(
                        Workspace(
                            key,
                            name,
                            status,
                            kind,
                            owner_leaf is not None and digest == owner_leaf,
                            validation,
                            age,
                            _refusal(
                                status=status,
                                owned=owner_leaf is not None and digest == owner_leaf,
                                age_seconds=age,
                                min_age_seconds=min_age_seconds,
                                include_quarantined=include_quarantined,
                                include_orphans=include_orphans,
                            ),
                        )
                    )
            finally:
                os.close(key_fd)
        return found
    finally:
        os.close(root_fd)


def reap(attempts_root: Path | str, workspaces: list[Workspace]) -> list[Workspace]:
    """Remove the reapable workspaces in `workspaces`, and any key left empty.

    Takes the surveyed list rather than re-deciding, so what is printed and what
    is removed cannot diverge — the mistake `submission.py` avoids by running its
    own validator over its own submission.
    """

    root = Path(attempts_root)
    removed: list[Workspace] = []
    try:
        with _refusing(f"cannot reap {root}"):
            _reap(root, workspaces, removed)
    except ReaperError as error:
        # Say what already went. A sweep is not atomic -- each workspace is its
        # own recursive unlink -- and the first real run removed two of five and
        # then failed, telling the operator only that it had failed. "It broke"
        # and "it broke after removing these two" are different situations, and
        # only the second makes it obvious that re-running is safe.
        if removed:
            error.add_note(
                f"{len(removed)} workspace(s) were removed before this: "
                + ", ".join(workspace.path for workspace in removed)
            )
        raise
    return removed


def _validate_target(workspace: Workspace) -> None:
    """Re-check the two names before either is used to open or unlink anything.

    `_survey` already refuses a root holding anything that is not a 64-hex
    operation key -- and `_reap` used to trust that, taking the names off the
    `Workspace` and handing them to `open_private_directory` and `os.rmdir`
    unexamined. A `Workspace` with `operation_key=".."` therefore removed a
    directory OUTSIDE the attempts root: reproduced, and the victim was gone.
    Nothing reachable from `main` builds one, because `main` always surveys
    first, but `reap` is exported and the guard that made one function safe was
    simply absent from the one that deletes.

    Checked here rather than "documented as a precondition of the caller",
    because a deletion tool whose safety lives in another function's docstring
    has the same shape as the gate whose authority checked less than its filter.
    """

    if not _OPERATION_KEY.match(workspace.operation_key):
        raise ReaperError(
            f"{workspace.operation_key!r} is not an operation key; refusing to open it"
        )
    name = workspace.name
    digest = name[len(_VALIDATION_PREFIX) :] if name.startswith(_VALIDATION_PREFIX) else name
    if not _OPERATION_KEY.match(digest):
        raise ReaperError(f"{name!r} is not an attempt workspace name; refusing to remove it")


def _reap(root: Path, workspaces: list[Workspace], removed: list[Workspace]) -> None:
    by_key: dict[str, list[Workspace]] = {}
    for workspace in workspaces:
        if workspace.reapable:
            # Before anything is opened, so a bad name in the list refuses the
            # whole call rather than being reached after earlier removals.
            _validate_target(workspace)
            by_key.setdefault(workspace.operation_key, []).append(workspace)
    root_fd = open_private_root(root, "attempts root")
    try:
        for key in sorted(by_key):
            key_fd = open_private_directory(root_fd, key, "operation")
            try:
                for workspace in by_key[key]:
                    remove_attempt_workspace(key_fd, workspace.name)
                    removed.append(workspace)
                with os.scandir(key_fd) as entries:
                    empty = next(entries, None) is None
            finally:
                os.close(key_fd)
            if empty:
                # Nothing else creates these, and a stage that needs one back
                # mkdirs it inside a `try/except FileExistsError`.
                os.rmdir(key, dir_fd=root_fd)
    finally:
        os.close(root_fd)


def describe_survey(workspaces: list[Workspace], *, confirmed: bool = False) -> str:
    if not workspaces:
        return "No attempt workspaces found."
    lines: list[str] = []
    for key in sorted({workspace.operation_key for workspace in workspaces}):
        lines.append(key)
        for workspace in workspaces:
            if workspace.operation_key == key:
                lines.append(workspace.describe())
    reapable = sum(1 for workspace in workspaces if workspace.reapable)
    lines.append("")
    lines.append(f"{reapable} of {len(workspaces)} workspaces reapable.")
    # Only when this IS the dry run. Printing "re-run with --confirm" underneath a
    # sweep that was confirmed reads as though nothing happened, which is exactly
    # what it looked like the first time a real removal failed part-way.
    if reapable and not confirmed:
        lines.append("Re-run with --confirm to remove them.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--attempts-root", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument(
        "--min-age-seconds",
        type=float,
        default=DEFAULT_MIN_AGE_SECONDS,
        help=f"refuse anything newer (default {DEFAULT_MIN_AGE_SECONDS}, "
        f"floor {MIN_AGE_FLOOR_SECONDS})",
    )
    parser.add_argument(
        "--include-quarantined",
        action="store_true",
        help="also remove the workspaces of quarantined operations. They are the "
        "only surviving record of why a fail-closed check refused",
    )
    parser.add_argument(
        "--include-orphans",
        action="store_true",
        help="also remove workspaces with no ledger row at all",
    )
    parser.add_argument("--confirm", action="store_true", help="actually remove them")
    args = parser.parse_args(argv)

    try:
        found = survey(
            args.attempts_root,
            args.ledger,
            min_age_seconds=args.min_age_seconds,
            include_quarantined=args.include_quarantined,
            include_orphans=args.include_orphans,
        )
    except ReaperError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    print(describe_survey(found, confirmed=args.confirm))
    if not args.confirm:
        return 0
    try:
        removed = reap(args.attempts_root, found)
    except ReaperError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    print(f"\nRemoved {len(removed)} workspace(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

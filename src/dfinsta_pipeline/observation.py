"""What the app was actually asked for. Endpoint evidence measured, not guessed.

    python -m dfinsta_pipeline.observation record --version 441 \
        --build-sha256 <64 hex> --recorded-at 2026-08-09T10:00:00Z \
        --session-id 441-feed-1 --surface feed_tab \
        --watched-from watched.txt --capture logcat.txt
    python -m dfinsta_pipeline.observation report --version 441 [--json]

Stage 4 finds endpoint *strings in a class* and asks a human to judge from a
name. On 2026-08-08 that produced two rulings it should not have: one endpoint
that fires zero times, and one — `delivery/background_prefetch` — that is not an
endpoint at all but a no-op logger's marker name. Both looked exactly like the
four good rulings beside them, because a name in a class of names is all the
evidence stage 4 has.

So the app grows an **observe mode**: a generated form of `throwIfBlocked` that
blocks nothing and emits one line per watched path it sees. This module is the
host side — it turns those lines into committed evidence, and that evidence into
an answer to "which of these paths does this phone never actually request?".

===============================================================================
  THE CONTRACT WITH THE APP
===============================================================================

One line per observed request, through `android.util.Log.i`::

    I DFInstaObserve: /feed/timeline_stream/

and in a real capture, with the threadtime prefix logcat adds::

    08-08 17:31:02.412 12875 12875 I DFInstaObserve: /feed/timeline/

The message is **verbatim** one of the watched literals, and nothing else is
emitted under that tag. :func:`parse` therefore anchors on the tag *position*
rather than searching for the string: a crash dump quoting one of these lines
inside its own payload is another component talking about DFInsta, not DFInsta
seeing a request, and `probes.count_signal` already paid for that lesson once —
re-narration counted as events and turned an off-side zero into a phantom leak.

Anything under that tag which is not verbatim a watched literal makes the whole
capture refuse. It means the build and the `watched` list disagree about what
was being watched, and a session whose watch list is wrong cannot support a
statement about what was *not* seen.

===============================================================================
  WHY A SESSION THAT SAW NOTHING IS NOT EVIDENCE
===============================================================================

The claim this module exists to support is a **negative** one: "the app was
watching this path and never once asked for it". Negative claims fail in one
characteristic way here — the measurement silently did not happen, and its
silence reads as the finding. `absence-assertions-need-positive-controls` is the
same lesson from the other end; so is the differential that compared 2 of 7 hooks
and reported it as a comparison.

A session in which **nothing at all** was observed is exactly that failure. It is
equally well explained by:

* the installed build not being the observing one,
* the capture being empty, taken from the wrong device, or cleared too late,
* the app never having run,
* or every watched path genuinely going unrequested.

Only the last is a finding, and nothing in the capture distinguishes it from the
other three. So a session counts as evidence **only if it observed at least one
literal** — the session's own output is its positive control, and the app's own
emission is the one signal that cannot rot into a false pass.

That threshold is *derived*, not chosen: it is `total > 0`, where `total` is the
session's own count. There is deliberately no "at least N sessions" constant.
A number like that would be a judgement about sufficiency dressed as a rule, and
`derive-the-threshold-never-declare-it` is the standing objection — `4` → `3` is
one character and looks like maintenance.

And when *every* session is vacuous, :func:`never_observed` **refuses**. It does
not return `()`. An empty tuple is the same answer it gives when every watched
path was seen, so returning it would report "we measured nothing" in the words of
"nothing is wrong" — the absence-as-a-pass this project refuses everywhere.
`rulings.unenforced_endpoints` refuses in the same place for the same reason.

===============================================================================
  WHAT THIS EVIDENCE CANNOT SAY
===============================================================================

**Never observed is bounded by the surfaces that were walked.** A path the Reels
player requests is not observed by a session that stayed on the feed, and that
silence is about the session, not about the app. `surface` is recorded per
session and every report repeats the list, because the reader's first question
has to be "would this session have seen it if it happened?".

It is also bounded by the account and by server-side configuration — a
MobileConfig flag picking the other implementation is how a statically perfect
430 settings hook came to be dead at runtime. This module records what a phone
did. It does not decide anything, and nothing here withdraws a block.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .contracts import SHA256_PATTERN
from .history import _NUMERIC

__all__ = [
    "ObservationError",
    "SCHEMA_VERSION",
    "TAG",
    "OBSERVATIONS",
    "ObservationSession",
    "parse",
    "store_path",
    "append",
    "read",
    "evidential",
    "never_observed",
    "summary",
    "render",
    "main",
]


class ObservationError(RuntimeError):
    """Raised when an observation cannot honestly be read or recorded."""


SCHEMA_VERSION = 1

#: The app's observe-mode log tag. Fixed by the contract above; the app side
#: emits under this and nothing else does.
TAG = "DFInstaObserve"

#: Per version, tracked, beside `manifest/runtime_evidence`. Committed for the
#: same reason that one is: evidence that must survive *between* ports is the
#: evidence a gitignored directory loses, and `evidence-in-scratch-is-not-evidence`
#: cost 441 four of its seven hooks.
OBSERVATIONS = Path("manifest") / "observations"

#: `[<stamp> <pid> <tid>] <LEVEL> DFInstaObserve: <literal>`.
#:
#: The optional prefix is what logcat's `threadtime` format prepends; without it
#: this still reads the bare form the contract states and a hand-made fixture.
#: The tag is anchored in **tag position** — immediately after the level — so a
#: line that merely contains `DFInstaObserve:` inside another tag's message body
#: does not match. That is the re-narration failure `probes.count_signal`
#: documents, and here it would manufacture requests that never happened.
_OBSERVE_LINE = re.compile(
    r"^\s*(?:\S+\s+\S+\s+\d+\s+\d+\s+)?[VDIWEFS]\s+" + re.escape(TAG) + r":\s?(?P<literal>.*)$"
)


def parse(text: str) -> dict[str, int]:
    """Count **every** appearance of each literal, ordered by its first.

    The count is what matters and the ordering is incidental — an earlier
    docstring said "by first appearance" of the counting rather than of the
    order, which reads as though repeats are collapsed. They are not: two
    requests for one path are two requests.

    Refuses rather than skips. A line under this tag whose payload is empty or
    padded is a build that is not honouring the contract, and quietly dropping it
    would subtract requests that did happen from a count whose whole purpose is
    to be compared against zero.
    """

    counts: dict[str, int] = {}
    for number, line in enumerate(text.splitlines(), 1):
        match = _OBSERVE_LINE.match(line)
        if match is None:
            continue
        literal = match.group("literal")
        if not literal.strip():
            raise ObservationError(
                f"line {number}: a {TAG} line carries no path literal. The contract is "
                "one line per observed request, and its message is the literal — an "
                "empty one cannot be attributed to any watched path"
            )
        if literal != literal.strip():
            raise ObservationError(
                f"line {number}: {TAG} emitted {literal!r}, which is padded. The message "
                "is compared verbatim against the watch list, so a padded literal would "
                "be counted against a path no build is watching"
            )
        counts[literal] = counts.get(literal, 0) + 1
    return counts


def _stamp(value: str) -> str:
    """An ISO 8601 timestamp with a UTC offset, stripped. Refuses anything else.

    Parsed rather than checked for emptiness, following `reversal_record._stamp`
    and for the reason that found: `--recorded-at banana` exited 0 and wrote into
    an append-only file nothing ever deletes from. `Z` is accepted and not
    rewritten — what a human typed is what gets recorded, and
    `datetime.fromisoformat` reads both spellings back.
    """

    from datetime import datetime  # noqa: PLC0415  (only this one function needs it)

    stamp = value.strip()
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError as error:
        raise ObservationError(
            f"{value!r} is not an ISO 8601 timestamp ({error}). It would be written "
            "into an append-only store nothing ever deletes from"
        ) from error
    if parsed.tzinfo is None:
        raise ObservationError(
            f"{value!r} has no UTC offset. A naive stamp cannot be ordered against one "
            "written on another machine, and two sessions being orderable is what makes "
            "them comparable"
        )
    return stamp


@dataclass(frozen=True)
class ObservationSession:
    """One device session: which build watched what, and what it saw.

    Every field that makes the row joinable is required. A measurement that names
    no version, no build and no time is a number nobody can join to anything —
    that was found once already, by asking what a report could honestly say about
    an evidence claim, and the answer was "nothing".
    """

    schema_version: int
    #: The Instagram version the observing build was made from.
    version: str
    #: The APK the operator actually had installed. The device serial identifies
    #: a phone, never a build, so without this the row cannot be joined to what
    #: was measured.
    build_sha256: str
    #: Supplied, never read from the clock here — as everywhere else in this repo.
    #: **Parsed**, with a required UTC offset, and stored stripped. Not merely
    #: checked for emptiness: `reversal` was checked that way and
    #: `--recorded-at banana` exited 0 into an append-only file nothing ever
    #: deletes from. A naive stamp cannot be ordered against one written on
    #: another machine, which is the whole point of two sessions being comparable.
    recorded_at: str
    session_id: str
    #: Free text: which surface the operator walked, e.g. `feed_tab`. Load-bearing
    #: rather than decorative — a zero means "not on this surface", and a reader
    #: who cannot see which surface cannot read the zero.
    surface: str
    #: Every literal this build was watching. The population the negative claim
    #: is made over; without it a zero count is indistinguishable from a path the
    #: build never looked for.
    watched: tuple[str, ...]
    counts: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ObservationError(
                f"unsupported observation schema {self.schema_version!r}"
            )
        # `str(...)` on both, because these run on values that may have come
        # straight out of JSON: a numeric `version` would otherwise make the
        # *guard* raise TypeError, replacing a legible refusal with a traceback
        # about the refusal — the shape `rulings.unenforced_endpoints` notes.
        if not _NUMERIC.fullmatch(str(self.version)):
            raise ObservationError(f"{self.version!r} is not a version number")
        if not SHA256_PATTERN.fullmatch(str(self.build_sha256 or "")):
            raise ObservationError(
                f"build_sha256 must be a lowercase SHA-256, got {self.build_sha256!r}. "
                "A session that cannot name the APK it measured cannot be joined to one"
            )
        for value, label in (
            (self.recorded_at, "recorded_at"),
            (self.session_id, "session_id"),
            (self.surface, "surface"),
        ):
            if not str(value).strip():
                raise ObservationError(
                    f"an observation session is missing {label}. A measurement nobody "
                    "can place in time, tell apart from another, or attribute to a "
                    "surface is not evidence a human can read a zero from"
                )
        # Stored stripped, and the stripped value is what `to_dict` writes.
        # Validating `value.strip()` and then recording `value` put the padding
        # into a permanent record in a form the next read refuses — the same
        # defect, found in `reversal_record._stamp`.
        object.__setattr__(self, "recorded_at", _stamp(self.recorded_at))
        object.__setattr__(self, "watched", tuple(self.watched))
        if not self.watched:
            raise ObservationError(
                f"session {self.session_id} watched nothing. The negative claim is made "
                "over the watch list, so an empty one makes every count unattributable "
                "and the session unable to support any statement at all"
            )
        blank = [item for item in self.watched if not str(item).strip()]
        if blank:
            raise ObservationError(
                f"session {self.session_id} has a blank entry in its watch list"
            )
        repeated = sorted({
            item for item in self.watched if self.watched.count(item) > 1
        })
        if repeated:
            raise ObservationError(
                f"session {self.session_id} watches {', '.join(repeated)} more than "
                "once. Two spellings of one fact is how a count comes to be read twice"
            )
        for literal, count in self.counts.items():
            if not isinstance(count, int) or isinstance(count, bool) or count < 1:
                raise ObservationError(
                    f"session {self.session_id}: {literal} has count {count!r}. `parse` "
                    "never produces a zero, so a recorded zero is a second spelling of "
                    "'absent' — and absence is exactly what this store must not blur"
                )
        unwatched = sorted(set(self.counts) - set(self.watched))
        if unwatched:
            raise ObservationError(
                f"session {self.session_id} counted {', '.join(unwatched)}, which it was "
                "not watching. The build and the watch list disagree about what was "
                "being watched, so nothing this session did not see can be relied on"
            )

    @property
    def total(self) -> int:
        """Every request this session observed, across all watched paths."""

        return sum(self.counts.values())

    @property
    def vacuous(self) -> bool:
        """Did this session observe nothing at all?

        Derived from the session's own output — `total > 0` and no constant. A
        vacuous session is equally well explained by a build that was not
        observing, a capture that was empty, and an app that never ran, so it is
        no evidence about any path. See the module docstring.
        """

        return self.total == 0

    @property
    def observed(self) -> tuple[str, ...]:
        return tuple(sorted(self.counts))

    @property
    def unobserved(self) -> tuple[str, ...]:
        """Watched by this session and not seen by it. Meaningless when vacuous."""

        return tuple(sorted(set(self.watched) - set(self.counts)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "version": self.version,
            "build_sha256": self.build_sha256,
            "recorded_at": self.recorded_at,
            "session_id": self.session_id,
            "surface": self.surface,
            "watched": list(self.watched),
            "counts": dict(sorted(self.counts.items())),
            # Derived and written anyway, following `SignalCount.to_dict`. It is
            # the number a human reads first, and `from_dict` refuses a row whose
            # total disagrees with its counts — so a hand-edit that changes one
            # count and forgets the total is caught instead of believed.
            "total": self.total,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ObservationSession":
        if not isinstance(data, Mapping):
            raise ObservationError(
                f"an observation session must be a JSON object, got {type(data).__name__}"
            )
        allowed = {
            "schema_version",
            "version",
            "build_sha256",
            "recorded_at",
            "session_id",
            "surface",
            "watched",
            "counts",
            "total",
        }
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise ObservationError(
                f"observation session has unknown keys: {', '.join(unknown)}"
            )
        watched = data.get("watched")
        if not isinstance(watched, Sequence) or isinstance(watched, (str, bytes)):
            raise ObservationError(
                f"watched must be a list of literals, got {type(watched).__name__}"
            )
        counts = data.get("counts", {})
        if not isinstance(counts, Mapping):
            raise ObservationError(
                f"counts must be an object of literal -> integer, got "
                f"{type(counts).__name__}"
            )
        session = cls(
            schema_version=data.get("schema_version"),
            version=str(data.get("version", "")),
            build_sha256=str(data.get("build_sha256", "")),
            recorded_at=str(data.get("recorded_at", "")),
            session_id=str(data.get("session_id", "")),
            surface=str(data.get("surface", "")),
            watched=tuple(str(item) for item in watched),
            counts={str(key): value for key, value in counts.items()},
        )
        stated = data.get("total")
        if stated is not None and stated != session.total:
            raise ObservationError(
                f"session {session.session_id} states total {stated!r} and its counts "
                f"sum to {session.total}. One of the two was edited without the other, "
                "and a store that disagrees with itself cannot be read"
            )
        return session


def store_path(version: str, root: Path | str = ".") -> Path:
    """Where one version's sessions live. `root` decides, always."""

    if not _NUMERIC.fullmatch(version):
        raise ObservationError(f"{version!r} is not a version number")
    return Path(root) / OBSERVATIONS / f"{version}.jsonl"


def read(
    version: str, root: Path | str = ".", *, path: Path | str | None = None
) -> tuple[ObservationSession, ...]:
    """Every recorded session for `version`, in file order.

    A missing file means none, which is the ordinary state before any device
    session has been taken. A file that exists and cannot be read is a **refusal**
    — including a path that is a directory, or bytes that are not UTF-8. Those
    are the shapes that make `is_file()` answer False and turn "unreadable" into
    "absent", which is the defect `expectation.sweep` names in as many words.

    Every row must name `version`. A 440 session filed under 441 would make a
    negative claim about a build that was never installed.
    """

    location = Path(path) if path is not None else store_path(version, root)
    # `stat`, not `exists()` / `is_file()`. Both of those answer False for a
    # directory, for a dangling symlink and for a path under a directory this
    # process may not traverse — three unreadable stores wearing the answer
    # "there is nothing here", in the one function whose empty result means
    # "nothing is wrong".
    try:
        status = location.stat()
    except FileNotFoundError as error:
        if location.is_symlink():
            raise ObservationError(
                f"{location} is a symlink that points nowhere. Somebody meant a store "
                "to be there, so this is not the same fact as no store at all"
            ) from error
        return ()
    except OSError as error:
        raise ObservationError(f"{location}: {error}") from error
    if not stat.S_ISREG(status.st_mode):
        raise ObservationError(
            f"{location} exists and is not a regular file, so its sessions cannot be "
            "read. Treating that as 'no sessions' would report an unreadable store as "
            "an empty one"
        )
    try:
        text = location.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ObservationError(f"{location}: {error}") from error

    out: list[ObservationSession] = []
    seen: set[str] = set()
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ObservationError(f"{location}:{number}: {error}") from error
        try:
            session = ObservationSession.from_dict(row)
        except ObservationError as error:
            raise ObservationError(f"{location}:{number}: {error}") from error
        if session.version != version:
            raise ObservationError(
                f"{location}:{number}: session {session.session_id} is about "
                f"{session.version} and is filed under {version}. A session read against "
                "the wrong version is a claim about a build that was never installed"
            )
        if session.session_id in seen:
            raise ObservationError(
                f"{location}:{number}: session {session.session_id} appears twice. Two "
                "rows under one id is the state where nobody can say which capture the "
                "counts came from"
            )
        seen.add(session.session_id)
        out.append(session)
    return tuple(out)


def append(
    session: ObservationSession,
    *,
    root: Path | str = ".",
    path: Path | str | None = None,
) -> Path:
    """Append one session. Append-only, and atomic.

    The existing store is **read first**, so a malformed one refuses instead of
    being overwritten by a writer that never looked at it, and a duplicate
    `session_id` refuses rather than making the counts ambiguous.

    Written to a temporary file in the same directory and renamed, following
    `submission.journal` and `manifest_patch.write_manifest_atomically`. A plain
    `open(…, "a")` that dies mid-line leaves a truncated last row, and this
    store's readers refuse a truncated row **permanently** — the operator would
    have to work out for themselves that the fix is to edit a file nothing told
    them about. `os.replace` within one directory is atomic, so the store is
    either every session before this one or every session including it.
    """

    location = Path(path) if path is not None else store_path(session.version, root)
    existing = read(session.version, root, path=location)
    if any(item.session_id == session.session_id for item in existing):
        raise ObservationError(
            f"session {session.session_id} is already recorded in {location}. A second "
            "capture is a new session with its own id; re-filing one under an existing "
            "id would silently replace a measurement"
        )

    body = "".join(
        json.dumps(item.to_dict(), sort_keys=True) + "\n"
        for item in (*existing, session)
    )
    location.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=location.parent,
        prefix=location.name + ".",
        suffix=".tmp",
        delete=False,
    )
    scratch = Path(handle.name)
    try:
        try:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.replace(scratch, location)
    except BaseException:
        scratch.unlink(missing_ok=True)
        raise
    return location


def evidential(sessions: Iterable[ObservationSession]) -> tuple[ObservationSession, ...]:
    """The sessions that observed something, and are therefore evidence.

    The whole non-vacuity control, in one line and with no constant in it.
    """

    return tuple(item for item in sessions if not item.vacuous)


def never_observed(
    version: str, root: Path | str = ".", *, path: Path | str | None = None
) -> tuple[str, ...]:
    """Literals watched in at least one non-vacuous session and never once seen.

    Both halves matter. *Watched* excludes a path no build was looking for, whose
    silence says nothing. *Non-vacuous* excludes a session that saw nothing at
    all, whose silence says nothing either — see the module docstring for the
    three explanations for a vacuous session that have nothing to do with the
    app's behaviour.

    **Refuses when no session is evidence.** NOT `()`. This function's entire job
    is to name paths whose absence was measured, and an empty tuple is the same
    answer it gives when every watched path was seen — so "we measured nothing"
    would arrive spelled "nothing is wrong". That is absence reported as a pass,
    which is the one failure this project refuses everywhere else;
    `rulings.unenforced_endpoints` refuses in the same place, for the same reason.
    """

    location = Path(path) if path is not None else store_path(version, root)
    sessions = read(version, root, path=location)
    usable = evidential(sessions)
    if not usable:
        if not sessions:
            raise ObservationError(
                f"there is no observation evidence for {version} ({location} holds no "
                "session). Nothing can be said about what the app never requested "
                "until something recorded what it did"
            )
        raise ObservationError(
            f"all {len(sessions)} observation session(s) for {version} are vacuous: not "
            "one of them observed a single watched literal. A session that saw nothing "
            "is equally well explained by a build that was not observing, an empty "
            "capture, or an app that never ran — so it is evidence about no path. "
            "Returning an empty tuple here would be the same answer this gives when "
            "every watched path WAS seen"
        )

    watched: set[str] = set()
    seen: set[str] = set()
    for item in usable:
        watched.update(item.watched)
        seen.update(item.counts)
    return tuple(sorted(watched - seen))


# ------------------------------------------------------------------ reporting


def summary(version: str, root: Path | str = ".") -> dict[str, Any]:
    """Everything a report says, in one shape, so both output forms read it.

    One producer for both views. The human banner and the machine field going out
    of step is a defect this project has shipped — the JSON a script gates on was
    missing the warning the human form printed.
    """

    sessions = read(version, root)
    usable = evidential(sessions)
    vacuous = [item for item in sessions if item.vacuous]
    totals: dict[str, int] = {}
    for item in usable:
        for literal, count in item.counts.items():
            totals[literal] = totals.get(literal, 0) + count

    warnings: list[str] = []
    if not sessions:
        warnings.append(
            f"there is no observation evidence for {version}: nothing has been recorded"
        )
    elif not usable:
        warnings.append(
            f"all {len(sessions)} session(s) are vacuous — not one observed a single "
            "watched literal, so none of them is evidence about any path"
        )
    elif vacuous:
        warnings.append(
            f"{len(vacuous)} of {len(sessions)} session(s) are vacuous — they observed "
            "nothing and are excluded: "
            + ", ".join(sorted(item.session_id for item in vacuous))
        )
    # Computed once and used twice. Two `sorted({... for item in usable})`
    # expressions — one for the warning, one for the field — are two places that
    # can disagree, and a page whose banner says `feed_tab, reels_tab` while its
    # bound says `feed_tab` is worse than either alone.
    surfaces = sorted({item.surface for item in usable})
    if usable:
        warnings.append(
            "never-observed is bounded by the surfaces walked: "
            + ", ".join(surfaces)
            + ". A path only the Reels player requests is not observed by a session "
            "that stayed on the feed"
        )

    try:
        unseen: list[str] = list(never_observed(version, root))
        refusal = ""
    except ObservationError as error:
        unseen = []
        refusal = str(error)

    return {
        "schema_version": SCHEMA_VERSION,
        "version": version,
        "session_count": len(sessions),
        "evidential_session_count": len(usable),
        "vacuous_session_ids": sorted(item.session_id for item in vacuous),
        "surfaces": surfaces,
        "observed": dict(sorted(totals.items())),
        "never_observed": unseen,
        # Present in the machine form whenever the human form would print it, and
        # never only in one of them.
        "never_observed_refused": refusal,
        "warnings": warnings,
    }


def render(report: Mapping[str, Any]) -> str:
    lines = [f"OBSERVATION  {report['version']}", "=" * 68, ""]
    lines.append(
        f"  {report['session_count']} session(s), "
        f"{report['evidential_session_count']} with observations"
    )
    if report["surfaces"]:
        lines.append(f"  surfaces walked: {', '.join(report['surfaces'])}")
    lines.append("")

    if report["never_observed_refused"]:
        lines += ["  WATCHED AND NEVER OBSERVED: refused", "",
                  f"    {report['never_observed_refused']}", ""]
    elif report["never_observed"]:
        lines.append(f"  WATCHED AND NEVER OBSERVED ({len(report['never_observed'])})")
        lines.append("")
        for literal in report["never_observed"]:
            lines.append(f"    {literal}")
        lines.append("")
    else:
        lines += ["  Every watched path was observed at least once.", ""]

    if report["observed"]:
        lines.append("  OBSERVED")
        lines.append("")
        width = max(len(literal) for literal in report["observed"])
        for literal, count in sorted(
            report["observed"].items(), key=lambda pair: (-pair[1], pair[0])
        ):
            lines.append(f"    {literal.ljust(width)}  {count}")
        lines.append("")

    if report["warnings"]:
        lines += ["  WARNINGS", ""]
        for warning in report["warnings"]:
            lines.append(f"    {warning}")
        lines.append("")

    lines.append(
        "  This measures; it does not decide. `reconsider` turns a never-observed "
        "block into a"
    )
    lines.append("  question for a human, and only a human withdraws one.")
    return "\n".join(lines)


# ------------------------------------------------------------------------ CLI


def _watch_list(args: argparse.Namespace) -> tuple[str, ...]:
    watched: list[str] = list(args.watched or ())
    if args.watched_from:
        text = Path(args.watched_from).read_text(encoding="utf-8")
        watched += [line.strip() for line in text.splitlines() if line.strip()]
    ordered: list[str] = []
    for literal in watched:
        if literal not in ordered:
            ordered.append(literal)
    if not ordered:
        raise ObservationError(
            "no watch list given. Pass --watched or --watched-from: without it the "
            "session records counts against a population nobody stated, and a zero "
            "becomes unreadable"
        )
    return tuple(ordered)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path("."))
    sub = parser.add_subparsers(dest="command", required=True)

    record = sub.add_parser("record", help="turn one logcat capture into a session")
    record.add_argument("--version", required=True)
    record.add_argument("--build-sha256", required=True, help="the APK that was installed")
    record.add_argument(
        "--recorded-at", required=True, help="ISO 8601. Supplied, never read from the clock"
    )
    record.add_argument("--session-id", required=True)
    record.add_argument("--surface", required=True, help="which surface was walked, e.g. feed_tab")
    record.add_argument(
        "--watched", action="append", default=[], help="a watched literal; repeatable"
    )
    record.add_argument("--watched-from", type=Path, help="a file of watched literals, one per line")
    record.add_argument("--capture", type=Path, help="a logcat capture; default stdin")

    report = sub.add_parser("report", help="what was seen at a version, and what was not")
    report.add_argument("--version", required=True)
    report.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "record":
            text = (
                Path(args.capture).read_text(encoding="utf-8")
                if args.capture
                else sys.stdin.read()
            )
            session = ObservationSession(
                schema_version=SCHEMA_VERSION,
                version=args.version,
                build_sha256=args.build_sha256,
                recorded_at=args.recorded_at,
                session_id=args.session_id,
                surface=args.surface,
                watched=_watch_list(args),
                counts=parse(text),
            )
            written = append(session, root=args.root)
            print(
                f"{session.session_id}: {session.total} request(s) across "
                f"{len(session.counts)} of {len(session.watched)} watched path(s) "
                f"on {session.surface}"
            )
            if session.vacuous:
                # Printed on the way out, not swallowed. A vacuous session is
                # worth recording — it is the honest record of a capture that saw
                # nothing — and it is worth saying that it will never be counted.
                print(
                    "  VACUOUS: nothing at all was observed, so this session is not "
                    "evidence about any path. Check the build is the observing one and "
                    "that the capture covers the app running."
                )
            print(f"recorded in {written}")
            print(
                "Commit it: the report and `reconsider` read the committed files, and "
                "an uncommitted row works here and vanishes on clone."
            )
            return 0

        report_data = summary(args.version, args.root)
        if args.json:
            print(json.dumps(report_data, indent=2, sort_keys=True))
        else:
            print(render(report_data))
        # Exit 0 whatever it finds. This is a measurement, not a gate.
        return 0
    except (ObservationError, ValueError, OSError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

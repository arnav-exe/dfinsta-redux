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
emits one line per watched path it sees, *before* any rule can throw. It blocks
exactly what a shipped build blocks — `test_an_observing_build_blocks_exactly_what_a_shipped_one_blocks`
compares the rule spans of both renderings — and that is the whole reason the
section below exists, because a build that still blocks suppresses the very
requests it is counting. This module is the host side: it turns those lines into
committed evidence, and that evidence into an answer to "which of these paths
does this phone never actually request?".

===============================================================================
  THE CONTRACT WITH THE APP
===============================================================================

One line per observed request, through `android.util.Log.i`::

    I DFInstaObserve: /feed/timeline_stream/

and in a real capture, with the threadtime prefix logcat adds::

    08-08 17:31:02.412 12875 12875 I DFInstaObserve: /feed/timeline/

plus a **directive** naming which blocks were active, emitted on *every* checked
request, ahead of any path line that request produces::

    I DFInstaObserve: !toggles disable_feed=1 disable_explore=0 disable_reels=1 …
    I DFInstaObserve: /feed/timeline/

`1` is on, meaning blocking. A payload beginning `!` is a directive and never a
path; an unrecognised one **refuses**, so a host reading a capture from a newer
build fails loudly instead of counting `!version 442` as a request. Repeats are
collapsed — a 22-request session states the same thing 22 times.

It repeats because the once-per-process version of it failed in the field, and
failed silently. The protocol is `adb logcat -c` immediately before walking the
app, Instagram's process is usually already alive, so the single line had been
written into the buffer that was then cleared and the flag stayed set: 22 path
lines and no statement of what was active, with nothing marking the omission.
Restating it per request buys the invariant **any capture holding a path line
also holds the toggle state**, and buys a second thing the flag could not — a
toggle changed halfway through a session now contradicts itself in the file
instead of being invisible.

The message is otherwise **verbatim** one of the watched literals, and nothing
else is emitted under that tag. :func:`parse` therefore anchors on the tag
*position* rather than searching for the string: a crash dump quoting one of
these lines inside its own payload is another component talking about DFInsta,
not DFInsta seeing a request, and `probes.count_signal` already paid for that
lesson once — re-narration counted as events and turned an off-side zero into a
phantom leak.

Anything under that tag which is not verbatim a watched literal makes the whole
session refuse — in `ObservationSession`, which is the only place that knows the
watch list; `parse` counts what it is given. It means the build and the `watched`
list disagree about what was being watched, and a session whose watch list is
wrong cannot support a statement about what was *not* seen.

===============================================================================
  A ZERO IS ONLY READABLE UNDER A STATED CONFIGURATION
===============================================================================

The blocks suppress requests **downstream of themselves**, so a session measured
with them on can produce a zero that is a fact about our own configuration.
Measured on 2026-08-08, same build and same walk, only the five toggles changed:

    /feed/injected_reels_media/   0 with the blocks on   3 with them off
    /feed/reels_media_stream/     0                      1
    /clips/discover/stream/       0                      3

Blocking `/feed/timeline/` leaves no timeline response for Reels to be injected
into, so the child request is never made. And `replaceReelsEndpoint` blanks the
endpoint string before the URL is built — which is also *before* the observe
pass — so with `disable_reels` on those paths report zero for a reason that has
nothing to do with traffic. Three zeros, none of them about Instagram.

So every session carries the toggle state it was measured under, and:

**The state is read from the device, never typed by the operator.** There is no
`--toggles` flag, and adding one would be the same shape of mistake as the rule
this project shipped and broke in one line the next day: `effective_from` derived
from a `--version` the same person supplied in the same command, a safety
property that was really a formality. `retirement`'s docstring states the lesson —
*ask what the operator controls*. Here the operator controls the phone's settings
and the capture; the build controls what it says about itself. A selector may
*choose* among recorded states, because choosing wrong refuses rather than
answering.

Precisely what that buys, and no more: **the recorded state is a function of the
capture alone.** Somebody who wants a row to say something else has to put the
line into the capture, or construct the record by hand — forging the evidence
rather than filling in a field. That is a different act from typing a flag: it is
visible in the capture that gets kept beside the session, and visible in a diff.
An adversarial pass is right that it is not impossible; it is *the thing you
would have to do*, which is the most any host-side rule can be worth.

**A capture that cannot state its toggle state is a refusal, not an "all off".**
A path line ahead of any directive is a capture whose start was cut off, and its
counts cannot be attributed to any configuration. Two directives that disagree
are two configurations in one file — a toggle changed mid-session, or two
captures concatenated — with no line saying which counts belong to which, and
they refuse too.

A capture with no tag lines at all is the ordinary vacuous capture: no directive
because the observe pass never ran. It records honestly, with an unknown state,
and is excluded from every answer by the vacuity rule below rather than by this
one. The directive proves the build was observing; it does **not** prove the app
was walked, so a stated session that saw nothing is still vacuous.

===============================================================================
  WHY THE SESSIONS ARE NOT BLENDED
===============================================================================

:func:`never_observed` takes the toggle state as a **required argument** and
answers over the sessions measured under exactly that state. The all-off
exploration session and the one-toggle-on isolation sessions of the protocol land
in one `<version>.jsonl` and answer different questions; unioning them produces a
number that is about no configuration at all.

A required argument rather than "group, and refuse when mixed": a call that
answers today and refuses tomorrow because somebody filed a second session is
indistinguishable, from the caller's side, from a corpus that broke. Naming the
state makes the question well-posed at the call site and keeps it well-posed for
ever. :func:`states` says which states are on record, and the report answers
each state separately so the reader never has to pick.

**A session whose toggle state is unknown answers nothing.** It is not "probably
all off" and not "probably as shipped" — it is a measurement whose experiment was
not written down. `manifest/observations/441.jsonl` holds exactly one such row,
recorded on 2026-08-08 before the build reported its own state. The design note
written the same week says it was walked with the blocks on, which would make it
the circular measurement above; that note is a recollection and not a
measurement, and treating it as one is the back-fill this module refuses — which
is why the row answers nothing rather than answering as "blocks on". It stays
readable, though: deleting or back-filling a row in
an append-only store would be inventing a measurement from memory, which is the
operator-supplied state this design refuses — and it is excluded from every
toggle-scoped answer, by name, loudly, in both report forms.

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
did. It does not decide anything, and nothing here changes a block.

===============================================================================
  AND THE ONE QUESTION IT ANSWERS ABOUT BLOCKS
===============================================================================

:func:`blocked_and_never_observed` intersects the manifest's own blocked
literals with :func:`never_observed`. It is the surviving half of a deleted
module: `reconsider` asked the same question in order to *propose withdrawing*
a block, through a reversal gate that in its whole life recorded none. That
whole layer went on 2026-08-08, because the project stopped deciding early on a
name in a class and correcting afterwards, and started exploring on the phone
first. The question outlived the machinery — "we block this and the app has
never once asked for it" is exactly what a decision made on measurement wants to
know — so it lives here, beside the measurement, and answers rather than
proposes.
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
    "TOGGLE_DIRECTIVE",
    "OBSERVATIONS",
    "ToggleState",
    "Capture",
    "ObservationSession",
    "parse",
    "store_path",
    "append",
    "read",
    "evidential",
    "stated",
    "states",
    "never_observed",
    "blocked_endpoints",
    "blocked_and_never_observed",
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

#: The one directive the app emits, on every checked request, ahead of any path
#: line that request produces. A payload starting `!` is never a path — no
#: watched literal can begin with one, because `throwIfBlocked` tests
#: `URI.getPath()`.
TOGGLE_DIRECTIVE = "!toggles"

#: A preference key the guard reads. Shape only, deliberately: `guards.Rule`
#: already decides which names are legitimate for a *rule*, and this module
#: records what the device said rather than judging it. A build that renames its
#: toggles must still be able to file an honest capture.
_TOGGLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class ToggleState:
    """Which blocks were active while a capture was taken. Read from the device.

    Stored sorted by name, so two states are equal exactly when they say the same
    thing however the build ordered them — the app emits them in the order the
    guard reads them, which is rule order and moves when a rule moves. A state
    that compared unequal to itself across a rule reordering would split one
    experiment into two groups and answer both from half the sessions.

    Complete as the build reported it, never as a set of "the ones that were on".
    Two states naming different *keys* are different states and do not blend:
    a version that grows a sixth toggle has not measured the same experiment.
    """

    #: `(name, on)`, sorted by name. A Mapping is accepted here and normalised.
    pairs: tuple[tuple[str, bool], ...]

    def __post_init__(self) -> None:
        given = self.pairs
        items = list(given.items()) if isinstance(given, Mapping) else list(given)
        cleaned: list[tuple[str, bool]] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, Sequence) or isinstance(item, (str, bytes)) or len(item) != 2:
                raise ObservationError(
                    f"a toggle state is (name, on) pairs, got {item!r}"
                )
            name, value = item
            name = str(name)
            if not _TOGGLE_NAME.fullmatch(name):
                raise ObservationError(f"{name!r} is not a preference key")
            if not isinstance(value, bool):
                # `1 == True` in Python, so an int would compare equal here and
                # round-trip through JSON as `1` — one state with two spellings
                # in a store whose whole job is telling two states apart.
                raise ObservationError(
                    f"toggle {name} is {value!r}; a toggle state is on or off, and the "
                    "store writes true/false"
                )
            if name in seen:
                raise ObservationError(
                    f"a toggle state names {name} twice. One key cannot have been both "
                    "on and off for one capture"
                )
            seen.add(name)
            cleaned.append((name, value))
        if not cleaned:
            raise ObservationError(
                "a toggle state that names no toggle states nothing. The build reports "
                "every key it reads, so an empty one is a build that did not answer"
            )
        object.__setattr__(self, "pairs", tuple(sorted(cleaned)))

    @classmethod
    def of(cls, values: Mapping[str, bool] | Iterable[tuple[str, bool]]) -> "ToggleState":
        return cls(tuple(values.items()) if isinstance(values, Mapping) else tuple(values))

    @classmethod
    def parse(cls, text: str) -> "ToggleState":
        """`disable_feed=1 disable_explore=0` — the app's spelling, and a selector's.

        The same reader for both directions, so a state cannot be recorded in a
        form no caller can name back.
        """

        pairs: list[tuple[str, bool]] = []
        for token in str(text).split():
            name, separator, value = token.partition("=")
            if not separator or value not in ("0", "1"):
                raise ObservationError(
                    f"{token!r} is not `key=0` or `key=1`. A toggle state is read "
                    "verbatim from what the build reported, and a token nobody can read "
                    "is a build and a host that disagree about the contract"
                )
            pairs.append((name, value == "1"))
        return cls(tuple(pairs))

    @property
    def text(self) -> str:
        """The canonical spelling. Equal states have equal text, and conversely."""

        return " ".join(f"{name}={int(value)}" for name, value in self.pairs)

    @property
    def on(self) -> tuple[str, ...]:
        return tuple(name for name, value in self.pairs if value)

    @property
    def off(self) -> tuple[str, ...]:
        return tuple(name for name, value in self.pairs if not value)

    @property
    def blocking(self) -> bool:
        """Was anything blocking? The condition under which a zero can be ours."""

        return bool(self.on)

    def as_dict(self) -> dict[str, bool]:
        return dict(self.pairs)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.text


@dataclass(frozen=True)
class Capture:
    """What one logcat capture says: the configuration, and what was asked for.

    Both from one pass over the text. Two passes could disagree about which lines
    they saw, and the ordering rule — no path before the directive — is only
    checkable while reading in order.
    """

    #: `None` when the capture carries no directive, which only a capture with no
    #: path lines at all may do. Never a default of "all off": see the module
    #: docstring.
    toggles: ToggleState | None
    counts: Mapping[str, int] = field(default_factory=dict)

    @property
    def stated(self) -> bool:
        return self.toggles is not None


def parse(text: str) -> Capture:
    """Read one capture: the toggle state it states, and every path it counted.

    Counts **every** appearance of each literal, ordered by its first. The count
    is what matters and the ordering is incidental — an earlier docstring said
    "by first appearance" of the counting rather than of the order, which reads as
    though repeats are collapsed. They are not: two requests for one path are two
    requests.

    Refuses rather than skips. A line under this tag whose payload is empty or
    padded is a build that is not honouring the contract, and quietly dropping it
    would subtract requests that did happen from a count whose whole purpose is
    to be compared against zero.

    Refuses a **path before any directive**, and two directives that disagree.
    The app restates its configuration on every checked request, ahead of the
    path lines that request produces, so a path line with no directive in front
    of it is a capture whose start was cut off — `logcat -c` landing between the
    two lines of one request, or a file assembled from pieces. Its counts belong
    to a configuration nobody can name, and the alternative to refusing is to
    attribute them to whichever state does appear.

    Repeats are collapsed rather than counted: the same statement made 22 times
    is one statement. Two *different* statements are two experiments, and this is
    the shape a toggle changed halfway through a session takes — visible only
    because the line repeats, which is why it repeats.
    """

    counts: dict[str, int] = {}
    toggles: ToggleState | None = None
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
        if literal.startswith("!"):
            keyword, _, payload = literal.partition(" ")
            if keyword != TOGGLE_DIRECTIVE:
                # Forward compatibility that fails closed. A host that ignored an
                # unknown directive would read a newer build's capture as though
                # it had said nothing new; one that counted it would manufacture
                # a request for `!version`.
                raise ObservationError(
                    f"line {number}: {TAG} emitted the directive {keyword!r}, which this "
                    "host does not know. The build is newer than the reader, and a "
                    "capture whose statements are not all understood cannot be recorded"
                )
            try:
                stated = ToggleState.parse(payload)
            except ObservationError as error:
                raise ObservationError(f"line {number}: {error}") from error
            if toggles is not None and toggles != stated:
                raise ObservationError(
                    f"line {number}: this capture states two toggle states, "
                    f"{toggles.text!r} then {stated.text!r}. A toggle was changed while "
                    "the session was being walked, or two captures were concatenated; "
                    "either way no line says which counts belong to which "
                    "configuration, so this is two experiments at once"
                )
            toggles = stated
            continue
        if toggles is None:
            raise ObservationError(
                f"line {number}: {literal} was reported before any {TOGGLE_DIRECTIVE} "
                "line. The build restates which blocks were active on every checked "
                "request, ahead of the paths that request reports, so a path in front of "
                "one comes from a request this capture did not see begin — the start of "
                "the file was cut off, or it was assembled from pieces. Its counts "
                "cannot be attributed to any configuration, and a zero measured under an "
                "unknown one is not evidence about the app"
            )
        counts[literal] = counts.get(literal, 0) + 1
    return Capture(toggles=toggles, counts=counts)


def _stamp(value: str) -> str:
    """An ISO 8601 timestamp with a UTC offset, stripped. Refuses anything else.

    Parsed rather than checked for emptiness, for the reason a sibling record
    store found the hard way: `--recorded-at banana` exited 0 and wrote into an
    append-only file nothing ever deletes from. `Z` is accepted and not
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
    #: checked for emptiness: a sibling record store was checked that way and
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
    #: Which blocks were active, as the **build** reported them — never as an
    #: operator typed them. `None` means the capture did not say, which is a
    #: value that has to be written down rather than defaulted: it is the state
    #: of the one row recorded before builds reported themselves, and it answers
    #: no question that depends on the configuration. Required, and deliberately
    #: ahead of `counts`, so that every construction site states it.
    toggles: ToggleState | None
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
        # defect a sibling record store shipped and this one inherited the fix for.
        object.__setattr__(self, "recorded_at", _stamp(self.recorded_at))
        object.__setattr__(self, "watched", tuple(self.watched))
        # Copied, like `watched` is. Keeping the caller's live mapping meant every
        # check below could be undone after construction: a count added afterwards
        # produced a row `append` wrote and this module's own `read` then refused —
        # a store its writer made and its reader rejects.
        object.__setattr__(self, "counts", dict(self.counts))
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
        if self.toggles is not None and not isinstance(self.toggles, ToggleState):
            # Not coerced from a mapping. A state is normalised — sorted, with
            # every value a real boolean — and a raw dict slipping through would
            # compare unequal to the same state read back out of the store, which
            # is the one comparison every answer here is grouped by.
            raise ObservationError(
                f"session {self.session_id} has toggles {self.toggles!r}; pass a "
                "ToggleState (ToggleState.of({'disable_feed': True, ...})) or None"
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
        stated = {"toggles": self.toggles.as_dict()} if self.toggles is not None else {}
        return {
            "schema_version": self.schema_version,
            "version": self.version,
            "build_sha256": self.build_sha256,
            "recorded_at": self.recorded_at,
            "session_id": self.session_id,
            "surface": self.surface,
            "watched": list(self.watched),
            # Written only when the capture stated one. An unknown state is
            # spelled by the key's absence, which is how the row recorded before
            # builds reported themselves is already spelled — so `append`, which
            # rewrites the whole file, gives that row back byte for byte instead
            # of editing a store nothing is allowed to edit.
            **stated,
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
            "toggles",
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
        toggles: ToggleState | None = None
        if "toggles" in data:
            raw = data["toggles"]
            if raw is None:
                # The same rule as the recorded zero below: absence has one
                # spelling. A row that says `null` looks like a build that
                # answered "nothing", and this store must not blur an answer
                # nobody gave with one somebody gave.
                raise ObservationError(
                    "an observation session states toggles: null. An unknown toggle "
                    "state is spelled by the key being absent — the way a row recorded "
                    "before the build reported its own state is spelled — and a null is "
                    "a second spelling of absent"
                )
            if not isinstance(raw, Mapping):
                raise ObservationError(
                    f"toggles must be an object of key -> true/false, got "
                    f"{type(raw).__name__}"
                )
            toggles = ToggleState.of({str(key): value for key, value in raw.items()})
        session = cls(
            schema_version=data.get("schema_version"),
            version=str(data.get("version", "")),
            build_sha256=str(data.get("build_sha256", "")),
            recorded_at=str(data.get("recorded_at", "")),
            session_id=str(data.get("session_id", "")),
            surface=str(data.get("surface", "")),
            watched=tuple(str(item) for item in watched),
            toggles=toggles,
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

    **The writer refuses a session that saw something and cannot say under what
    configuration.** The constructor cannot hold that rule: it is the one shape
    `manifest/observations/441.jsonl` is already in, and the reader has to be able
    to give that row back. So the record type represents history it is no longer
    allowed to make — new evidence states its configuration or is not written.
    """

    location = Path(path) if path is not None else store_path(session.version, root)
    # `not session.vacuous`, which is `total > 0` and no constant — the same
    # derived threshold the module docstring insists on. `total > 1` would read as
    # maintenance and would silently admit a one-request session that says nothing
    # about its configuration.
    if session.toggles is None and not session.vacuous:
        raise ObservationError(
            f"session {session.session_id} observed {session.total} request(s) and states "
            "no toggle state. Every count it holds is unreadable: a zero under a block we "
            "set is caused by us, and nothing here says whether one was set. Record it "
            "from a capture that carries the build's own "
            f"`{TOGGLE_DIRECTIVE}` line — there is deliberately no way to supply one by "
            "hand"
        )
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


def stated(sessions: Iterable[ObservationSession]) -> tuple[ObservationSession, ...]:
    """The sessions that say which blocks were active while they were measured.

    The second control, and it is the same shape as :func:`evidential`: a filter
    derived from the row's own content, with no constant in it. A session that
    does not state its configuration is not evidence *about a configuration*,
    which is the only kind of evidence this module produces.
    """

    return tuple(item for item in sessions if item.toggles is not None)


def states(
    version: str, root: Path | str = ".", *, path: Path | str | None = None
) -> tuple[ToggleState, ...]:
    """The distinct toggle states that `version` has evidence under, sorted.

    The discovery half of the required argument on :func:`never_observed`: a
    caller cannot name a state it has no way to learn. Deliberately **not** a
    refusal when empty — this enumerates, it does not answer, and `()` here says
    "there is nothing you can ask about", which is not a claim about any path.
    The refusal belongs where the question is asked, with a message that can name
    which of the four reasons applies.
    """

    usable = stated(evidential(read(version, root, path=path)))
    return tuple(sorted({item.toggles for item in usable}, key=lambda item: item.text))


def never_observed(
    version: str,
    root: Path | str = ".",
    *,
    toggles: ToggleState,
    path: Path | str | None = None,
) -> tuple[str, ...]:
    """Literals watched under `toggles`, in a non-vacuous session, and never seen.

    Three halves now, and each excludes a silence that is about the measurement
    rather than about the app. *Watched* excludes a path no build was looking for.
    *Non-vacuous* excludes a session that saw nothing at all — see the module
    docstring for the three explanations that have nothing to do with the app's
    behaviour. *Measured under this exact state* excludes a zero that our own
    blocks caused: `/feed/injected_reels_media/` was observed 0 times with the
    blocks on and 3 times with them off, on one build and one walk.

    `toggles` is **required and names the experiment**, and the answer is over
    the sessions measured under exactly that state. An all-off exploration
    session and a one-toggle-on isolation session filed under one version answer
    different questions, and a union of them is about no configuration at all.
    The argument can only *select* — a state nobody measured refuses instead of
    answering — so it is not the operator-supplies-the-safety-property mistake
    that `retirement`'s docstring records; nothing here lets an operator say what
    a capture was.

    **Refuses when nothing can answer.** NOT `()`. This function's entire job is
    to name paths whose absence was measured, and an empty tuple is the same
    answer it gives when every watched path was seen — so "we measured nothing"
    would arrive spelled "nothing is wrong". That is absence reported as a pass,
    which is the one failure this project refuses everywhere else;
    `rulings.unenforced_endpoints` refuses in the same place, for the same reason.

    Four refusals, because four different things are wrong and each has its own
    fix: nothing was recorded, everything recorded saw nothing, everything that
    saw something predates builds stating their own configuration, and nothing
    was measured under the state you asked about.
    """

    if not isinstance(toggles, ToggleState):
        raise ObservationError(
            f"toggles must be a ToggleState, got {type(toggles).__name__}. Build one "
            "with ToggleState.parse('disable_feed=1 disable_explore=0 ...') or "
            "ToggleState.of({...}); `states(version, root)` lists the ones on record"
        )
    location = Path(path) if path is not None else store_path(version, root)
    sessions = read(version, root, path=location)
    unanswerable = _unanswerable(version, location, sessions)
    if unanswerable:
        raise ObservationError(unanswerable)
    configured = stated(evidential(sessions))
    matching = [item for item in configured if item.toggles == toggles]
    if not matching:
        raise ObservationError(
            f"no session for {version} was measured with {toggles.text!r}. The states on "
            "record are: "
            + "; ".join(item.text for item in
                        sorted({item.toggles for item in configured},
                               key=lambda item: item.text))
            + ". Answering from a session measured under another configuration would be "
            "answering a different question"
        )

    watched: set[str] = set()
    seen: set[str] = set()
    for item in matching:
        watched.update(item.watched)
        seen.update(item.counts)
    return tuple(sorted(watched - seen))


def blocked_endpoints(root: Path | str = ".") -> tuple[str, ...]:
    """Every path literal the generated guard tests, from the manifest.

    The *manifest's* rules and not the app source's `throwIfBlocked`, though
    `rulings.guarded_endpoints` reads the latter and would answer a very similar
    question. Two reasons. The manifest is committed and always present, while a
    decoded source tree is not — and a question about what this repository
    currently blocks should not become unanswerable on a machine that has not
    decoded an APK. And the manifest literal is the *same string* the observe
    build watches: `guards` renders both from `url_block_rules`, so the join
    below needs no spelling rule and cannot acquire one that is subtly wrong.
    A leading slash going unnormalised is how an entire grouping went invisible
    on 440, and the fix here is to have nothing to normalise.

    Refuses through `ObservationError` rather than leaking `GuardError`: this
    module has one refusal channel and its callers catch one exception.
    """

    from .guards import GuardError, rules_from_manifest  # noqa: PLC0415

    manifest = Path(root) / "manifest" / "hooks.json"
    try:
        rules = rules_from_manifest(manifest)
    except (GuardError, OSError, json.JSONDecodeError) as error:
        raise ObservationError(f"{manifest}: {error}") from error
    return tuple(sorted({literal.text for rule in rules for literal in rule.literals}))


def blocked_and_never_observed(
    version: str,
    root: Path | str = ".",
    *,
    toggles: ToggleState,
    path: Path | str | None = None,
) -> tuple[str, ...]:
    """Of the endpoints this repository blocks, which `version` never requested.

    The one question worth carrying over from the deleted `reconsider` module,
    whose `block_never_observed` rule asked it in order to propose *withdrawing*
    a block. Nothing withdraws anything now — the project decides late, on
    measurement, rather than deciding early and correcting afterwards — so this
    is a measurement and not a proposal. A path that is blocked and never once
    requested is a fact about this phone and these surfaces; what to do about it
    is a human's business.

    **Refuses whenever `never_observed` refuses, and deliberately does not
    soften it.** An empty tuple here is the honest answer to "every blocked path
    was seen at least once", so returning one because nothing was measured would
    report "we know nothing" in the words of "nothing is wrong". That is the
    absence-as-a-pass this module exists to refuse; see the docstring above.

    **Bounded by the watch list as well as by the surfaces.** A blocked endpoint
    no session was watching cannot appear here, and its silence means nothing —
    `summary` warns by name when the manifest blocks something the evidence never
    watched, because otherwise this answer is quietly incomplete.

    **And bounded by `toggles`, which is where this question is at its most
    circular.** Asked under a state in which the blocked endpoint's own toggle is
    on, "we block it and never saw it asked for" is very nearly a tautology: the
    block is upstream of the request for `/feed/injected_reels_media/`, and
    `replaceReelsEndpoint` removes the Reels paths from the URL before the
    observe pass can see them at all. The state is required here for that reason
    and not merely by inheritance; `summary` says so, per state, in both forms.
    """

    unseen = set(never_observed(version, root, toggles=toggles, path=path))
    return tuple(literal for literal in blocked_endpoints(root) if literal in unseen)


# ------------------------------------------------------------------ reporting


def _unanswerable(version: str, location: Path, sessions: Sequence[ObservationSession]) -> str:
    """Why nothing can be answered for `version`, or `""` when something can.

    One producer for the refusal and for the report's banner. `never_observed`
    raises this string and `summary` prints it, because a refusal a human reads in
    one wording and a script reads in another is the defect this module already
    carries a warning about: the machine view went quiet while the human one spoke.

    Three reasons, in the order they stop being fixable by taking another capture.
    """

    usable = evidential(sessions)
    if not sessions:
        return (
            f"there is no observation evidence for {version} ({location} holds no "
            "session). Nothing can be said about what the app never requested until "
            "something recorded what it did"
        )
    if not usable:
        return (
            f"all {len(sessions)} observation session(s) for {version} are vacuous: not "
            "one of them observed a single watched literal. A session that saw nothing "
            "is equally well explained by a build that was not observing, an empty "
            "capture, or an app that never ran — so it is evidence about no path. "
            "Returning an empty tuple here would be the same answer this gives when "
            "every watched path WAS seen"
        )
    if not stated(usable):
        return (
            f"none of the {len(usable)} evidential session(s) for {version} states which "
            "blocks were active: "
            + ", ".join(sorted(item.session_id for item in usable))
            + f". They predate the build reporting its own {TOGGLE_DIRECTIVE} line. A "
            "zero measured under an unknown configuration cannot be told apart from one "
            "our own blocks caused, and no configuration can be assumed for them now — "
            "that would be the operator-supplied state this module refuses"
        )
    return ""


def summary(version: str, root: Path | str = ".") -> dict[str, Any]:
    """Everything a report says, in one shape, so both output forms read it.

    One producer for both views. The human banner and the machine field going out
    of step is a defect this project has shipped — the JSON a script gates on was
    missing the warning the human form printed.

    **Answered per toggle state, and there is no whole-version answer.** There
    used to be a `never_observed` field here, over every evidential session at
    once; it is gone rather than kept alongside, because a blended number that
    looks like an answer is worse than a missing key. A caller reading the old
    field now fails loudly instead of reading a union of two experiments.
    """

    location = store_path(version, root)
    sessions = read(version, root)
    usable = evidential(sessions)
    vacuous = [item for item in sessions if item.vacuous]
    configured = stated(usable)
    unstated = [item for item in usable if item.toggles is None]

    warnings: list[str] = []
    unanswerable = _unanswerable(version, location, sessions)
    if unanswerable:
        warnings.append(unanswerable)
    if usable and vacuous:
        warnings.append(
            f"{len(vacuous)} of {len(sessions)} session(s) are vacuous — they observed "
            "nothing and are excluded: "
            + ", ".join(sorted(item.session_id for item in vacuous))
        )
    if configured and unstated:
        # Only when something *can* be answered. When nothing states a state the
        # refusal above already names every one of them, and saying it twice in
        # two wordings is how two spellings of one fact come to disagree.
        warnings.append(
            f"{len(unstated)} evidential session(s) state no toggle state and are "
            "excluded from every answer below: "
            + ", ".join(sorted(item.session_id for item in unstated))
            + f". A row without a {TOGGLE_DIRECTIVE} line was measured under a "
            "configuration nobody wrote down"
        )

    # Read once, for every state. It fails for reasons `never_observed` cannot —
    # an unreadable manifest, or one declaring no block at all — and a reader told
    # "all sessions are vacuous" when the real fault is a missing `url_block_rules`
    # would repair the wrong thing. Reported as a warning as well as a field, so it
    # is still audible when there is no state to hang it on.
    try:
        blocked: list[str] = list(blocked_endpoints(root))
        blocked_refusal = ""
    except ObservationError as error:
        blocked = []
        blocked_refusal = str(error)
    if blocked_refusal:
        warnings.append(
            "the blocked-and-never-observed question cannot be answered for any state: "
            + blocked_refusal
        )

    entries: list[dict[str, Any]] = []
    for state in states(version, root):
        group = [item for item in configured if item.toggles == state]
        unseen = list(never_observed(version, root, toggles=state))
        totals: dict[str, int] = {}
        for item in group:
            for literal, count in item.counts.items():
                totals[literal] = totals.get(literal, 0) + count
        # Computed once and used twice. Two `sorted({...})` expressions — one for
        # the warning, one for the field — are two places that can disagree, and a
        # page whose banner says `feed_tab, reels_tab` while its bound says
        # `feed_tab` is worse than either alone.
        surfaces = sorted({item.surface for item in group})
        warnings.append(
            f"{state.text}: never-observed is bounded by the surfaces walked: "
            + ", ".join(surfaces)
            + ". A path only the Reels player requests is not observed by a session "
            "that stayed on the feed"
        )
        # Produced once and placed twice: in the state's own entry, where `render`
        # prints it immediately above the list it is about, and in `warnings`,
        # which is what a script reads. The most dangerous line in this report is
        # a never-observed literal under a blocking state, and a caution twenty
        # lines below it in a WARNINGS block is a caution the reader has already
        # passed.
        caution = ""
        if state.blocking:
            caution = (
                f"{state.text}: measured with {', '.join(state.on)} ON, so a zero here "
                "can be caused by our own blocks rather than by the app. Blocking "
                "/feed/timeline/ leaves no timeline response for /feed/injected_reels_media/ "
                "to be injected into, and disable_reels blanks the Reels endpoint before "
                "the URL is built, which is upstream of the observe pass. Only a session "
                "with every toggle off answers 'would the app ask for this?'"
            )
            warnings.append(caution)
        builds = sorted({item.build_sha256 for item in group})
        if len(builds) > 1:
            # A toggle key is not the experiment: what `disable_feed` blocks is
            # decided by the manifest the build was rendered from, so two builds
            # of one version can report the same state and block different
            # literals. Not a refusal — the usual case is a rebuild that changed
            # nothing here — but the reader has to be told the group spans two.
            warnings.append(
                f"{state.text}: this answer unions sessions from {len(builds)} builds "
                + ", ".join(item[:12] for item in builds)
                + ". A toggle name is not a rule: two builds can report the same state "
                "and block different literals"
            )
        watched: set[str] = set()
        for item in group:
            watched.update(item.watched)
        # A blocked endpoint no session was watching is not evidence of anything,
        # and it is absent from the answer in exactly the way a finding is. Named,
        # because the reader's question about a short list is "is that all of them?".
        unwatched = [item for item in blocked if item not in watched]
        if unwatched:
            warnings.append(
                f"{state.text}: {len(unwatched)} blocked endpoint(s) were not in any "
                "watch list under this state, so nothing here says anything about them: "
                + ", ".join(unwatched)
            )
        entries.append({
            "toggles": state.as_dict(),
            "toggles_text": state.text,
            "toggles_on": list(state.on),
            "circular": caution,
            "session_ids": sorted(item.session_id for item in group),
            "build_sha256s": builds,
            "surfaces": surfaces,
            "observed": dict(sorted(totals.items())),
            "never_observed": unseen,
            "blocked_never_observed": [item for item in blocked if item in set(unseen)],
            "blocked_never_observed_refused": blocked_refusal,
        })

    return {
        "schema_version": SCHEMA_VERSION,
        "version": version,
        "session_count": len(sessions),
        "evidential_session_count": len(usable),
        "stated_session_count": len(configured),
        "vacuous_session_ids": sorted(item.session_id for item in vacuous),
        "unstated_session_ids": sorted(item.session_id for item in unstated),
        # Empty exactly when `states` is non-empty: every question this report can
        # answer is answered under one of them, and when it can answer none this
        # says which of the three reasons applies.
        "unanswerable_reason": unanswerable,
        "states": entries,
        "warnings": warnings,
    }


def render(report: Mapping[str, Any]) -> str:
    lines = [f"OBSERVATION  {report['version']}", "=" * 68, ""]
    lines.append(
        f"  {report['session_count']} session(s), "
        f"{report['evidential_session_count']} with observations, "
        f"{report['stated_session_count']} stating which blocks were active"
    )
    lines.append("")

    if report["unanswerable_reason"]:
        lines += ["  NOTHING CAN BE ANSWERED", "",
                  f"    {report['unanswerable_reason']}", ""]

    for state in report["states"]:
        lines.append(f"  TOGGLES  {state['toggles_text']}")
        lines.append(f"    sessions: {', '.join(state['session_ids'])}")
        lines.append(f"    surfaces: {', '.join(state['surfaces'])}")
        lines.append("")
        if state["circular"]:
            # Above the list, not in the WARNINGS block below it.
            lines += [f"    {state['circular']}", ""]

        if state["never_observed"]:
            # No count in the heading. It is a second spelling of the length of a
            # list printed directly below it, and this project has twice shipped a
            # count that drifted from the thing it counted.
            lines.append("    WATCHED AND NEVER OBSERVED")
            lines.append("")
            for literal in state["never_observed"]:
                lines.append(f"      {literal}")
            lines.append("")
        else:
            lines += ["    Every watched path was observed at least once.", ""]

        if state["blocked_never_observed_refused"]:
            lines += ["    BLOCKED AND NEVER OBSERVED: refused", "",
                      f"      {state['blocked_never_observed_refused']}", ""]
        elif state["blocked_never_observed"]:
            lines.append("    BLOCKED AND NEVER OBSERVED")
            lines.append("")
            for literal in state["blocked_never_observed"]:
                lines.append(f"      {literal}")
            lines.append("")
        else:
            lines += ["    Every blocked path this manifest declares was observed.", ""]

        if state["observed"]:
            lines.append("    OBSERVED")
            lines.append("")
            width = max(len(literal) for literal in state["observed"])
            for literal, count in sorted(
                state["observed"].items(), key=lambda pair: (-pair[1], pair[0])
            ):
                lines.append(f"      {literal.ljust(width)}  {count}")
            lines.append("")

    if report["warnings"]:
        lines += ["  WARNINGS", ""]
        for warning in report["warnings"]:
            lines.append(f"    {warning}")
        lines.append("")

    lines.append(
        "  This measures; it does not decide. A blocked path that was never once "
        "requested is a"
    )
    lines.append(
        "  fact about this phone and these surfaces — what to do about it is a "
        "human's to decide."
    )
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


def _record_parser(sub: Any) -> argparse.ArgumentParser:
    """Every option `record` accepts — one definition, so a test can ask.

    Not inlined into :func:`main`, because the property worth defending is about
    what this command *offers*: nothing here may carry a toggle state. A test
    that rebuilt the option list to check that would be checking its own copy,
    and a test that named `--toggles` would be a denylist of one — which is the
    shape `retirement` replaced with an allowlist after `agent` was denied and
    `claude`, `bot` and `ci` sailed through.
    """

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
    # And deliberately nothing naming a toggle, a block or a state. The toggle
    # state comes out of the capture, where the build put it; an option here would
    # let the person who ran the session state what the session measured, which is
    # the shape of safety property this project shipped and broke the next day.
    return record


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path("."))
    sub = parser.add_subparsers(dest="command", required=True)

    _record_parser(sub)

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
            capture = parse(text)
            session = ObservationSession(
                schema_version=SCHEMA_VERSION,
                version=args.version,
                build_sha256=args.build_sha256,
                recorded_at=args.recorded_at,
                session_id=args.session_id,
                surface=args.surface,
                watched=_watch_list(args),
                toggles=capture.toggles,
                counts=capture.counts,
            )
            written = append(session, root=args.root)
            print(
                f"{session.session_id}: {session.total} request(s) across "
                f"{len(session.counts)} of {len(session.watched)} watched path(s) "
                f"on {session.surface}"
            )
            if session.toggles is None:
                print(
                    f"  toggles: not stated — this capture carries no {TOGGLE_DIRECTIVE} "
                    "line, which only a capture that observed nothing can do."
                )
            else:
                print(f"  toggles: {session.toggles.text}  (as the build reported them)")
            if session.toggles is not None and session.toggles.blocking:
                # The whole reason the field exists, said at the moment the number
                # is produced rather than only where it is read.
                print(
                    "  CIRCULAR: "
                    + ", ".join(session.toggles.on)
                    + " were ON, so a zero in this session can be caused by our own "
                    "blocks. Only a session with every toggle off answers 'would the "
                    "app ask for this?'."
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
                "Commit it: the report reads the committed files, and an uncommitted "
                "row works here and vanishes on clone."
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

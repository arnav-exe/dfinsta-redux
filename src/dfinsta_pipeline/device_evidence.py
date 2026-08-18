"""What the phone said about one endpoint, across every version and every walk.

    python -m dfinsta_pipeline.device_evidence /feed/timeline_stream/

Stage 4 asks a human whether a new endpoint is a distraction worth a switch. Until
now the only evidence it could offer was static: the app groups this literal with
things we already block, and no hook blocks it. Both true, both about the decode,
and neither says whether the app ever *asks* for it.

On 2026-08-08 six candidates were ruled `block` on exactly that evidence. Five of
them are requested **zero times** across the 72 device sessions recorded since, and
one — `delivery/background_prefetch` — is not an endpoint at all but an event name
passed to a stub whose every method is `return-void`. The gate worked; what it
showed did not.

===============================================================================
  THREE STATES, AND CONFLATING TWO OF THEM IS THE FAILURE
===============================================================================

* **Never watched.** No device run has looked for this literal. That is a fact
  about *us*, not about the app, and it is the state with teeth: `feature_gate`
  refuses `block` and `offer_toggle` for a candidate in it.
* **Watched and never requested.** Looked for across N sessions and never seen.
* **Watched and requested.** Seen, with a per-version verdict from `grouping`.

The first two look identical in any rendering that reports a count, which is why
`docs/ROADMAP.md` has carried "say *never watched* and *watched, never seen* in
different words" as an open item since before this module existed.

===============================================================================
  A ZERO IS WEAK EVIDENCE, AND STAYS WEAK
===============================================================================

`feed/timeline_stream/` is requested zero times on this account and blocking it is
still right: it sits in Instagram's own list of continuous-feed paths, and the
routing that decides whether an account sees it is server-side and can change. So
a zero here is `Strength.WEAK` **whatever the corpus size** — the argument is
about what a zero can mean, not about how many sessions produced it. Owner
decision, 2026-08-08. A large red `0` is persuasive out of all proportion to what
it proves, so the summary states the corpus behind it and the caveat with it.

Only a positive observation is `STRONG`, and it is strong for the ordinary reason:
the app asked, repeatedly, across versions, and `grouping` says which toggle
governs it.

===============================================================================
  WHY THIS IS A SEPARATE MODULE
===============================================================================

`assessment` reads no filesystem, deliberately, because it runs inside a Temporal
Activity whose determinism under replay depends on it. `grouping` is scoped to one
version and one walk, deliberately, because pooling two walks was measured
swallowing a real erasure. Neither is the right home for a query that globs the
store and answers across the whole grid, so this is a third thing that imports
both and is imported by the recorder.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .assessment import Evidence, Strength, normalise, spellings
from .feature_gate import (
    DEVICE_NEVER_REQUESTED,
    DEVICE_REQUESTED,
    DEVICE_UNWATCHED,
)
from .observation import OBSERVATIONS, ObservationError

__all__ = [
    "DeviceReading",
    "corpora",
    "grid",
    "reading_for",
    "evidence_for_all",
    "main",
]


@dataclass(frozen=True)
class DeviceReading:
    """What every corpus said about one literal. Counts, never a conclusion."""

    literal: str
    #: `(version, walk)` for every corpus whose sessions watched this literal.
    watched_in: tuple[tuple[str, str], ...] = ()
    #: `(version, walk)` for every corpus at all, watched or not. The denominator.
    corpora: tuple[tuple[str, str], ...] = ()
    #: How many sessions across all corpora watched it.
    sessions: int = 0
    #: Every request observed for it, summed across states and corpora.
    seen: int = 0
    #: `(version, walk, verdict, toggle)` from `grouping`, where one was reached.
    verdicts: tuple[tuple[str, str, str, str | None], ...] = ()
    #: How many of the matching sessions annotated which surface was on screen.
    #: Zero means no walk marked its stream, and then the two counts below are
    #: `None` rather than `0` — a corpus that could not attribute a request is
    #: not a corpus that attributed it to nothing.
    attributed_sessions: int = 0
    #: Requests that arrived in the launch window, before the harness tapped
    #: anything. `None` when nothing was annotated.
    unsolicited: int | None = None
    #: Requests that arrived on a surface the harness had navigated to. `None`
    #: when nothing was annotated.
    solicited: int | None = None

    @property
    def watched(self) -> bool:
        return bool(self.watched_in)

    @property
    def versions(self) -> tuple[str, ...]:
        return tuple(sorted({version for version, _ in self.watched_in}))

    @property
    def walks(self) -> tuple[str, ...]:
        return tuple(sorted({walk for _, walk in self.watched_in}))

    @property
    def kind(self) -> str:
        """Which of the three states this is. The authority keys on this."""

        if not self.watched:
            return DEVICE_UNWATCHED
        return DEVICE_REQUESTED if self.seen else DEVICE_NEVER_REQUESTED

    def as_dict(self) -> dict[str, object]:
        return {
            "literal": self.literal,
            # The state, not only the counts. This module exists because a count
            # cannot tell "nobody looked" from "looked and saw nothing", and a
            # machine-readable view that emitted only counts would reproduce the
            # very conflation it was written to end.
            "kind": self.kind,
            "watched": self.watched,
            "corpora": [list(item) for item in self.corpora],
            "watched_in": [list(item) for item in self.watched_in],
            "sessions": self.sessions,
            "seen": self.seen,
            "verdicts": [list(item) for item in self.verdicts],
            "attributed_sessions": self.attributed_sessions,
            # Null, not zero, when nothing was annotated. A consumer that reads
            # `0` here would conclude the app never asks at startup, which is a
            # measurement no corpus without walk markers can support.
            "unsolicited": self.unsolicited,
            "solicited": self.solicited,
        }


def _watched_forms(watched: Sequence[str]) -> set[str]:
    """Every spelling of every literal a session watched.

    The watch list carries the guard's spelling and a candidate carries the
    index's, and `/api/v1/clips/homecoming/` is watched in a form no slash
    variant of `clips/homecoming/` reaches. Expanding both sides is what lets the
    two meet — `assessment.normalise` is the function that knows `api/v1/` is a
    prefix, and comparing without it reported "no device has looked for this"
    about a path watched in seventy-two sessions.
    """

    forms: set[str] = set()
    for item in watched:
        forms.update(spellings(item))
        forms.update(spellings(normalise(item)))
    return forms


def corpora(root: Path | str = ".") -> tuple[tuple[str, str], ...]:
    """Every `(version, walk)` on record, discovered rather than configured.

    Globbing the store rather than taking a list is what stops this module going
    quietly stale: a version measured and never added to a constant would simply
    be missing from every candidate's evidence, and the gate would show less than
    the project holds without saying so.
    """

    from .history import _NUMERIC  # noqa: PLC0415
    from .observation import read, walks  # noqa: PLC0415

    directory = Path(root) / OBSERVATIONS
    if not directory.exists():
        return ()
    if not directory.is_dir():
        # Present and not a directory is a broken store, not an empty one. The
        # digest beside this makes the same distinction, and `observation.read`
        # goes to some length to keep "unreadable" from reading as "absent".
        raise ObservationError(
            f"{directory} exists and is not a directory, so the observation store "
            "cannot be read. That is not the same as there being none"
        )
    found: list[tuple[str, str]] = []
    for store in sorted(directory.glob("*.jsonl")):
        # `439.bak.jsonl`, `notes.jsonl`, an editor's leftover: not a version, and
        # not a reason to take out stage 4a. `read` refuses a non-numeric stem,
        # and that refusal would escape `record` untranslated, from a module the
        # caller never imported. `history` filters the same way for the same
        # reason.
        version = store.stem
        if not _NUMERIC.fullmatch(version):
            continue
        if not read(version, root):
            continue
        for walk in walks(version, root):
            found.append((version, walk))
    return tuple(found)


def grid(root: Path | str = ".") -> dict[tuple[str, str], Mapping[str, object]]:
    """Every corpus's `grouping.summary`, computed once.

    Once, because a hundred candidates must not mean six hundred `classify`
    calls. `summary` is the entry point that **never raises** — a corpus it
    cannot derive from comes back with `unanswerable_reason` set — so one
    unreadable corpus costs its own verdicts and not the whole answer.
    """

    from .grouping import summary  # noqa: PLC0415

    return {
        (version, walk): summary(version, root, walk=walk)
        for version, walk in corpora(root)
    }


def reading_for(
    literal: str,
    root: Path | str = ".",
    *,
    computed: Mapping[tuple[str, str], Mapping[str, object]] | None = None,
) -> DeviceReading:
    """What every corpus said about `literal`.

    Joined through :func:`assessment.spellings`, never by equality. A candidate
    carries the **index's** spelling (`feed/timeline_stream/`) and the observation
    store is keyed by the **guard's** (`/feed/timeline_stream/`), because
    `throwIfBlocked` tests a URI path. Of the six real candidates on record,
    exactly one joins by equality and all six join through the spellings.
    """

    from .observation import evidential, read  # noqa: PLC0415

    computed = grid(root) if computed is None else computed
    # `spellings` returns nothing for a blank literal, and an empty form set
    # intersects to nothing, so a blank literal matches no session and falls out
    # as `device_unwatched` — without an early return. There was one here; it
    # could not fire, because deleting it changed no answer, and a guard that
    # cannot fire reads as protection. The behaviour is pinned by
    # `test_a_blank_literal_matches_nothing_where_there_is_something_to_match`.
    forms = set(spellings(literal))
    normalised = {normalise(item) for item in forms}

    watched_in: list[tuple[str, str]] = []
    verdicts: list[tuple[str, str, str, str | None]] = []
    sessions = seen = attributed = 0
    unsolicited = solicited = 0
    for (version, walk), report in sorted(computed.items()):
        # `evidential` first, for the reason `grouping`, `walks` and `states` all
        # apply it: a session that observed nothing at all is equally well
        # explained by a build that was not observing, a capture that was empty
        # and an app that never ran. Counting one as "a device looked" would
        # unlock `block` on the strength of a session that measured nothing.
        rows = [item for item in evidential(read(version, root)) if item.walk == walk]
        matching = [item for item in rows if forms & _watched_forms(item.watched)]
        if not matching:
            continue
        watched_in.append((version, walk))
        sessions += len(matching)
        for item in matching:
            seen += sum(
                count
                for key, count in item.counts.items()
                if normalise(key) in normalised
            )
            if item.per_surface is None:
                # This session's walk did not annotate. Counted nowhere rather
                # than as a session that attributed nothing to the launch window.
                continue
            attributed += 1
            for key in item.counts:
                if normalise(key) not in normalised:
                    continue
                unbidden, asked = item.per_surface.consent_split(key)
                unsolicited += unbidden
                solicited += asked
        for verdict in report.get("verdicts", ()):  # type: ignore[union-attr]
            if verdict.get("endpoint") in forms:
                verdicts.append(
                    (version, walk, verdict.get("verdict", ""), verdict.get("toggle"))
                )
    return DeviceReading(
        literal=literal,
        watched_in=tuple(watched_in),
        corpora=tuple(sorted(computed)),
        sessions=sessions,
        seen=seen,
        verdicts=tuple(verdicts),
        attributed_sessions=attributed,
        unsolicited=unsolicited if attributed else None,
        solicited=solicited if attributed else None,
    )


def _consent_note(reading: DeviceReading) -> str:
    """What the corpus can say about the consent test, and what it cannot.

    The gate asks a human "did a user action cause this content to appear", and
    this is the only measurement bearing on it. It goes inside the existing
    `device_requested` item rather than beside it as a second one, because
    `_evidence` mints exactly one per candidate and because this is not a
    separate finding — it is the same requests, split by when they arrived.

    **Three things it will not do.** It does not score, rank or recommend; a
    composite of a-priori signals once put the random control between the
    labelled groups. It does not read a low startup count as evidence of
    solicited delivery — requests on a surface followed *a* tap, not necessarily
    a request for that content, which is why search and generic recommendations
    sit at 33% and 100% in Lukoff's bands with the same tap in front of both.
    And it says nothing at all when no walk annotated, rather than reporting a
    zero nobody measured.
    """

    if not reading.attributed_sessions:
        return (
            ". No walk annotated which surface was on screen, so this corpus "
            "cannot say whether these requests were solicited. Re-walk with "
            "`tools/device_session.py`, which marks the stream as it taps"
        )
    unbidden = reading.unsolicited or 0
    asked = reading.solicited or 0
    note = (
        f". Of these, {unbidden} arrived in the launch window before the harness "
        f"tapped anything and {asked} followed a tap, across "
        f"{reading.attributed_sessions} annotated session(s)"
    )
    if unbidden:
        return note + (
            ". The launch window is the one stretch with no user action in it, so "
            "those are unsolicited by measurement rather than by argument"
        )
    return note + (
        ". A zero here is NOT evidence that the app only asks when asked: a "
        "request that follows a tap need not be a request for what the tap asked "
        "for, and no walk can tell those apart"
    )


def _evidence(reading: DeviceReading) -> Evidence:
    """One `Evidence` for one reading. Exactly one, whatever the state.

    Never a judgement, and never a recommendation: `Assessment` refuses anything
    in `measured` that is not an `Evidence`, and the reason is a past defect where
    a `Judgement` serialised into the measured array while `judgement` stayed
    null.
    """

    if not reading.watched:
        return Evidence(
            DEVICE_UNWATCHED,
            Strength.WEAK,
            f"no device run has looked for {reading.literal!r}: it is on no watch "
            f"list in any of the {len(reading.corpora)} corpus/corpora on record. "
            "This says nothing about whether the app requests it — add it to "
            "`observe_watch` in manifest/hooks.json and walk the phone",
            {
                "literal": reading.literal,
                "corpora": [list(item) for item in reading.corpora],
            },
        )
    if not reading.seen:
        return Evidence(
            DEVICE_NEVER_REQUESTED,
            Strength.WEAK,
            f"watched across {reading.sessions} session(s) on "
            f"{', '.join(reading.versions)} under {', '.join(reading.walks)}, and "
            "never once requested. WEAK on purpose: this is one account and these "
            "surfaces, and the routing that decides what an account sees is "
            "server-side. `feed/timeline_stream/` is requested zero times and "
            "blocking it is still right — a zero is not a reason to leave a path "
            "open",
            {
                "literal": reading.literal,
                "sessions": reading.sessions,
                "versions": list(reading.versions),
                "walks": list(reading.walks),
            },
        )
    return Evidence(
        DEVICE_REQUESTED,
        Strength.STRONG,
        f"requested {reading.seen} time(s) across {reading.sessions} session(s) on "
        f"{', '.join(reading.versions)}"
        + (
            "; " + ", ".join(
                f"{version}/{walk}: {verdict}"
                + (f" by {toggle}" if toggle else "")
                for version, walk, verdict, toggle in reading.verdicts
            )
            if reading.verdicts
            else "; no corpus reached a verdict about which toggle governs it"
        )
        + _consent_note(reading),
        {
            "literal": reading.literal,
            "seen": reading.seen,
            "sessions": reading.sessions,
            "versions": list(reading.versions),
            "verdicts": [list(item) for item in reading.verdicts],
            "attributed_sessions": reading.attributed_sessions,
            "unsolicited": reading.unsolicited,
            "solicited": reading.solicited,
        },
    )


def evidence_for_all(
    literals: Iterable[str], root: Path | str = "."
) -> dict[str, tuple[Evidence, ...]]:
    """One tuple of `Evidence` per literal, over one pass of the grid."""

    computed = grid(root)
    return {
        literal: (_evidence(reading_for(literal, root, computed=computed)),)
        for literal in literals
    }


def render(reading: DeviceReading) -> str:
    lines = [f"DEVICE EVIDENCE  {reading.literal}", "=" * 68, ""]
    lines.append(f"  state    : {reading.kind}")
    lines.append(
        f"  corpora  : {len(reading.corpora)} on record"
        + (
            f", {len(reading.watched_in)} watched it"
            if reading.corpora
            else " — none; nothing has been measured"
        )
    )
    lines.append(f"  sessions : {reading.sessions}")
    lines.append(f"  requests : {reading.seen}")
    # Printed as its own block rather than folded into `requests`, because it is
    # the one line bearing on the question the gate asks a human — and because
    # "not measured" has to be visibly different from "measured as zero".
    lines.append("")
    lines.append("  CONSENT")
    if not reading.attributed_sessions:
        lines.append("    not measured — no walk annotated which surface was on screen")
    else:
        lines.append(
            f"    unsolicited : {reading.unsolicited}   (launch window, no user action)"
        )
        lines.append(
            f"    after a tap : {reading.solicited}   (NOT the same as solicited)"
        )
        lines.append(f"    from        : {reading.attributed_sessions} annotated session(s)")
    if reading.verdicts:
        lines.append("")
        lines.append("  VERDICTS")
        for version, walk, verdict, toggle in reading.verdicts:
            lines.append(
                f"    {version} {walk:16} {verdict}" + (f" by {toggle}" if toggle else "")
            )
    lines += ["", "  " + _evidence(reading).summary, ""]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("literal", help="an endpoint literal, in any slash spelling")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    reading = reading_for(args.literal, args.root)
    if args.json:
        import json  # noqa: PLC0415

        print(json.dumps(reading.as_dict(), indent=1, sort_keys=True))
    else:
        print(render(reading))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())

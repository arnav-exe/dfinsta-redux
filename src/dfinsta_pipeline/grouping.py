"""Which endpoints belong to which toggle, derived from what the phone did.

    python -m dfinsta_pipeline.grouping report --version 439 --walk three-round-v2 [--json]

Step 6 of `docs/DESIGN_EXPLORATION_FIRST.md`: *endpoints that kill the same thing
get grouped, and the group gets a toggle*. Until now the grouping came from
reading a name. `feed/injected_reels_media/` sounds like Reels, so `disable_reels`
— and on 2026-08-08 that reasoning produced six rulings in one sitting, of which
one was not an endpoint at all and five fire zero times on the owner's phone.

This module never reads a name. It reads
`manifest/observations/<version>.jsonl`, groups the sessions by the toggle state
the *build* reported, and asks what changed between the state with everything off
and the states with exactly one thing on.

===============================================================================
  TWO MECHANISMS, AND ONLY ONE OF THEM IS IN THE REQUEST COUNTS
===============================================================================

**A path block does not remove the request.** `throwIfBlocked` runs *after* the
observe pass — it has to, or a blocked path would report zero for exactly the
reason it was working — so the request is made, counted, and then thrown. The
count afterwards is whatever the app's retry logic makes it. On 439 that is
`/feed/timeline/` 6 → 20 under `disable_feed`, a rise, and `/feed/reels_tray/`
2 → 3 under `disable_stories`, which is nothing: two runs of one state differ by
that much on their own. **A block cannot be found by looking at counts.**

**An upstream erasure does remove it.** `replaceReelsEndpoint` returns `""` at the
`const-string` site, before the URL is built and therefore before the observe pass
and before the guard. `/clips/discover` goes 4 → 0 and 3 → 0, and no block is ever
reported, because nothing ever reached the code that throws. **This one is only in
the counts.**

So there are two signals and each is blind to the other's mechanism.
`observation.parse` now counts both: the watched paths, and the
`java.io.IOException: Blocked by DFInsta setting` headers Instagram emits when the
guard throws.

===============================================================================
  HOW A BLOCK IS ATTRIBUTED TO A PATH
===============================================================================

The block count is per *capture*. Nothing in the event says which path it was —
the line above the header names a **feature** (`FEED_NOT_LOADING`,
`STORY_NOT_LOADING`, `EXPLORE_NOT_LOADING`), and a feature is not a path.

What attributes it is arithmetic. Under a state where exactly one toggle is on,
the guard throws once per request to a path that toggle blocks, so

    blocks in the capture  ==  sum of the counts of the paths that toggle blocks

and when a single watched path accounts for the whole total *and no combination of
paths accounts for it too*, that path is the one being blocked. Call it the
**block-accounting identity**; :func:`_accounts` is both halves of it.

Three things make it worth trusting, and one of them is the reason it is used at
all rather than the feature line:

* **It is arithmetic over two measurements**, not a reading of a name. Nothing in
  it knows that `/feed/timeline/` and `disable_feed` share a word.
* **It fails when events are dropped.** Instagram emits these at its discretion.
  If some are missing the equality simply does not hold, and this module says
  "unattributable" instead of guessing — the identity is its own control.
* **It is checked in every session of the state and the answers intersected.**
  In `439-reverse-stories` three watched paths happen to have count 3; in
  `439-isolate-stories` only `/feed/reels_tray/` has 3. One session would have
  been ambiguous, two are not, and neither is allowed to answer alone.

The feature category is carried and printed and is **never** a basis. It was
right three times out of three, which is a sample of three.

**Where the numbers in this docstring come from.** The 439 sessions committed in
`manifest/observations/439.jsonl` carry **no block counts** — they were recorded
before this host counted them, and `grouping report --version 439` accordingly
returns the erasures and refuses the blocked half by name. Every request count
quoted here is in that committed file and can be checked. Every *block* count
quoted here (20/23 under `disable_feed`, 3/3 under `disable_stories`, 1 then 0
under `disable_explore`) comes from re-reading the captures in `work/observations/`,
which is gitignored. They are reproducible on the machine that walked the app and
nowhere else, and they are quoted as the reason the code is shaped this way, not
as evidence a reader can verify from a clone.

The residual risk, stated rather than worked around: **a path that equals the
total in both sessions while the real one does not can still be named wrongly.**
That now takes a dropped event *and* a coincidence surviving two sessions *and* no
combination explaining the same total. It is why the count movement and the
feature category are both printed under every finding: a reader who sees
`/feed/timeline/` named with `FEED_NOT_LOADING` above every header and a rise of
13 has three things agreeing, and a reader who sees one of them alone knows it.

===============================================================================
  THE NOISE FLOOR IS MEASURED, NOT DECLARED
===============================================================================

A constant here would be the exact failure `expectation`'s docstring is about: a
threshold whose repair when it is wrong is one character, in a diff that looks
like maintenance. `NOISE = 2` → `NOISE = 3` is invisible and unarguable.

The corpus measures it instead. Every state was walked **twice**, so for every
(state, path) pair there is a difference that no toggle caused — the same
experiment, run again. The floor for a path is the largest of those differences
anywhere in the corpus:

    floor(path) = max over states with two or more sessions of (max - min)

and a path "moved" under a state only when the state's whole range clears the
baseline's whole range by more than that. Ranges, not averages: an arm whose
counts overlap the baseline's has not replicated whatever it is supposed to show,
and `derive-the-threshold-never-declare-it` is only half the lesson if the
comparison it feeds is a mean of two numbers.

On 439 the floors come out 1, 1, 2, 2, 1, 3 for the six paths that were ever
requested. Nobody chose any of them, and re-walking the app changes them.

===============================================================================
  AND THAT IS EXACTLY WHY A COMPARISON MUST NAME ITS WALK
===============================================================================

*"The same experiment, run again"* is doing all the work in the paragraph above.
It is true only if the two sessions did the same thing, and on 2026-08-11 the
driving script went from one pass over three surfaces to **three rounds** over
them: the 440 baseline went from 11–16 observed requests to 25. Two sessions of
one state, one short-walk and one long-walk, spread by 14 for a reason no toggle
caused — and this module would have taken that spread as its floor, called it
noise, and then swallowed every real effect underneath it. A measured threshold
is only better than a declared one while the thing it measures is what it claims
to measure.

So :func:`classify` takes the **walk as a required argument** and answers over the
sessions recorded under exactly that one. A required argument rather than "group,
and refuse when mixed", for the reason `observation.never_observed` states about
toggle states: a call that answers today and refuses tomorrow because somebody
filed a session on a new walk is indistinguishable, from the caller's side, from a
corpus that broke. `observation.walks()` says which are on record.

Two things this deliberately does *not* do. It does not pool the sessions that
name no walk, and it does not offer a way to ask for them: 439's twelve rows
predate the field, and a comparison over sessions whose protocol nobody wrote
down is the thing the argument exists to prevent — a bucket named "unstated" that
answered in full would hand back exactly the property naming the walk buys.
`observation.summary` is the other way round and says why: a negative claim only
gets safer as walks are pooled into it, a differential does not.

And the walk is **typed**, which the toggle state is not, because nothing on the
phone knows which script drove it. The check that makes a typed value worth
something is `observation.walk_dispute`: the spans of the sessions claiming one
walk must not split into two groups further apart than either group is wide. The
scale in it is derived, exactly as the floor above is — the only number written
down is how many sessions a group needs to *have* a range, and its own comment
gives the measurement behind that. It refuses here rather than warning, because a
floor derived across two protocols is wrong in the direction that looks like an
answer.

===============================================================================
  WHAT A PATH CAN BE CALLED, AND WHAT IT TAKES
===============================================================================

`never_requested`
    Zero in every session of every state. The owner's "recorded, not built"
    group: watched, walked for, and never once asked for. Ten of 439's sixteen.

`erased` by T
    Observed in **every** baseline session, zero in **every** session of T's, and
    the fall larger than the floor. Categorical in both directions, which is what
    an upstream erasure looks like and what a block never does.

`blocked` by T
    T's state reports blocks in every one of its sessions and the baseline reports
    **none** in any of its own, the path is still observed under T, and the
    block-accounting identity names it uniquely across all of T's sessions.

`unaffected`
    No toggle moved it beyond its floor, no toggle erased it, no toggle blocked
    it — **and every arm was readable**, because that is a claim about all five
    toggles and it cannot be made while one of them is unreadable. This is the
    verdict that is easiest to reach by accident, so it is the one with the
    positive control on it.

`unclassifiable`
    Everything else, always with the reason. A path not reliably observed in the
    baseline is here first: if the baseline itself sometimes sees zero, a zero
    under an arm says nothing, and neither does a rise. `/feed/reels_media_stream/`
    on 439 is 1 in one baseline session and 0 in the other.

===============================================================================
  IT REFUSES RATHER THAN RETURNING A TIDY EMPTY ANSWER
===============================================================================

Seven refusals, each naming a different missing thing, because each has a
different fix — and none of them returns "nothing was affected", which is what
this would otherwise say when it had measured nothing at all. That is
`absence-assertions-need-positive-controls`, and `observation.never_observed`
refuses in the same place for the same reason.

Nothing recorded; every session vacuous; an evidential session that states no
toggle state; no session on the walk that was asked for; a walk its own captures
contradict; no baseline; no single-toggle state; sessions from more than one
build; and no literal watched by all of them. The last two are checked late
because they are the ones that have to know which sessions the answer would have
rested on. The build one is a refusal here and only a
warning in `observation.summary`, and the difference is real: that report answers
*within* one state, where two builds are usually one rebuild, while this compares
*across* states, where a toggle name is not a rule and two builds can put
different literals under the same key. Then the difference between two states
would be a difference between two builds.

A state with two or more toggles on is not a refusal — it is excluded, by name,
because it cannot attribute an effect to either of them. A state walked only once
is excluded the same way, and that is `require replication` doing its job: two of
the five findings a human took from this corpus by hand appeared in one running
order only.

===============================================================================
  THIS IS A VIEW, AND IT DECIDES NOTHING
===============================================================================

**Nothing is written.** The grouping is recomputed from the sessions every time it
is asked for, the way `expectation` recomputes its bar from the previous port's
evidence rather than storing it. A file recording the answer would be a second
copy that rots against the store the moment another session is walked, and it
would be the thing people read.

Acting on it means a human editing `url_block_rules` in `manifest/hooks.json`.
That stays a human act and there is deliberately no machinery for it. What each
path is declared under **today** is printed beside what was measured, as context
and not as a proposal; a difference between the two is a question for a person,
and this module has no opinion about which side is wrong.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .observation import (
    ObservationError,
    ObservationSession,
    ToggleState,
    evidential,
    read,
    store_path,
    walk_dispute,
    walk_evidence,
)

__all__ = [
    "GroupingError",
    "SCHEMA_VERSION",
    "ERASED",
    "BLOCKED",
    "UNAFFECTED",
    "NEVER_REQUESTED",
    "UNCLASSIFIABLE",
    "State",
    "Finding",
    "Classification",
    "Grouping",
    "partition",
    "noise_floors",
    "classify",
    "summary",
    "render",
    "main",
]


class GroupingError(RuntimeError):
    """Raised when a grouping cannot honestly be derived."""


SCHEMA_VERSION = 1

#: The five verdicts. Strings rather than an enum, because they are printed, put
#: into JSON, and compared in tests, and a `.value` in all three places buys
#: nothing here.
ERASED = "erased"
BLOCKED = "blocked"
UNAFFECTED = "unaffected"
NEVER_REQUESTED = "never_requested"
UNCLASSIFIABLE = "unclassifiable"


@dataclass(frozen=True)
class State:
    """One toggle state and every session measured under it.

    The unit of the experiment. Sessions are held in recorded order so a reader
    can see which way round they were walked, and every question asked of a state
    is asked of *all* of its sessions — that is where the replication rule lives.
    """

    toggles: ToggleState
    sessions: tuple[ObservationSession, ...]

    @property
    def text(self) -> str:
        return self.toggles.text

    @property
    def session_ids(self) -> tuple[str, ...]:
        return tuple(item.session_id for item in self.sessions)

    @property
    def on(self) -> tuple[str, ...]:
        return self.toggles.on

    @property
    def is_baseline(self) -> bool:
        """Every toggle off. The state everything else is measured against."""

        return not self.toggles.on

    @property
    def label(self) -> str:
        """A short name for a column heading. The full state is always `text`.

        Derived from the toggles the build reported, so it cannot drift from the
        state it names — never from a session id, which is a string an operator
        typed and which says `reverse` on a session this module has no notion of.
        """

        if self.is_baseline:
            return "baseline"
        return self.arm or "+".join(self.toggles.on)

    @property
    def arm(self) -> str | None:
        """The one toggle this state turns on, or `None` if it is not an arm.

        `None` for the baseline and for any state with two or more on: an effect
        seen with two toggles on belongs to either of them or to the pair, and
        nothing in the record separates those.
        """

        on = self.toggles.on
        return on[0] if len(on) == 1 else None

    @property
    def replicated(self) -> bool:
        """Was this state walked more than once? Nothing classifies from one walk."""

        return len(self.sessions) > 1

    @property
    def surfaces(self) -> tuple[str, ...]:
        return tuple(sorted({item.surface for item in self.sessions}))

    @property
    def spans(self) -> tuple[int | None, ...]:
        """How long each session's capture ran, in recorded order. `None` is absent.

        Printed rather than judged. `observation.walk_dispute` is the only thing
        that draws a conclusion from spans, and it does so over the whole walk;
        two numbers from one state are a reader's own check that these really were
        two runs of one thing.
        """

        return tuple(item.span_seconds for item in self.sessions)

    def counts(self, endpoint: str) -> tuple[int, ...]:
        """What each session of this state saw, in recorded order. Absent is 0."""

        return tuple(item.counts.get(endpoint, 0) for item in self.sessions)

    def spread(self, endpoint: str) -> int | None:
        """The largest difference two runs of *this* state produced for `endpoint`.

        `None` from a single session: one number has no spread, and treating it as
        zero would say "this state is perfectly repeatable" about a state nobody
        repeated — which is the noise floor's whole input.
        """

        if not self.replicated:
            return None
        seen = self.counts(endpoint)
        return max(seen) - min(seen)

    @property
    def counted(self) -> bool:
        """Did every session of this state count its blocks?

        A row written before the host read the block header has `blocks is None`,
        which is not a zero. See `ObservationSession.blocks`.
        """

        return all(item.blocks is not None for item in self.sessions)

    @property
    def block_totals(self) -> tuple[int | None, ...]:
        return tuple(
            None if item.blocks is None else item.blocks.total for item in self.sessions
        )

    @property
    def blocks_text(self) -> str:
        """`20, 23` — or `not counted`, which is not the same as `0, 0`."""

        return ", ".join(
            "not counted" if total is None else str(total) for total in self.block_totals
        )

    @property
    def features(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for item in self.sessions:
            if item.blocks is None:
                continue
            for feature, count in item.blocks.by_feature:
                totals[feature] = totals.get(feature, 0) + count
        return dict(sorted(totals.items()))

    @property
    def blocks_replicate(self) -> bool | None:
        """`True` every session blocked, `False` none did, `None` they disagree.

        The third answer is the one that matters and it is not hypothetical:
        `disable_explore` on 439 reported one block in one session and none at all
        in the other, same state, same walk, opposite order. Instagram emits these
        at its discretion, so a zero is not proof that nothing was refused — and a
        state that cannot agree with itself classifies nothing.
        """

        if not self.counted or not self.replicated:
            return None
        totals = [item.blocks.total for item in self.sessions]  # type: ignore[union-attr]
        if all(total > 0 for total in totals):
            return True
        if all(total == 0 for total in totals):
            return False
        return None


@dataclass(frozen=True)
class Finding:
    """One toggle's measured effect on one path, with what says so."""

    kind: str
    toggle: str
    reason: str
    corroboration: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "toggle": self.toggle,
            "reason": self.reason,
            "corroboration": list(self.corroboration),
        }


@dataclass(frozen=True)
class Classification:
    """What one watched path was measured to be, and by what."""

    endpoint: str
    verdict: str
    #: The toggle that governs it, when exactly one was found to.
    toggle: str | None
    reason: str
    findings: tuple[Finding, ...] = ()
    #: Things true of this path that the verdict does not carry — an arm that also
    #: moved it, a state nobody could read. Never silently dropped.
    caveats: tuple[str, ...] = ()
    #: `None` when no state was walked twice, which is not the same as `0`.
    noise_floor: int | None = None
    #: `state text -> counts, in recorded order`.
    observed: Mapping[str, tuple[int, ...]] = field(default_factory=dict)
    #: The toggles `manifest/hooks.json` puts this path under **today**, by
    #: applying each literal's own match kind. `None` means the manifest could not
    #: be read; `()` means it declares nothing that matches. Context, not a
    #: proposal, and the two absences are different facts.
    declared: tuple[str, ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "verdict": self.verdict,
            "toggle": self.toggle,
            "reason": self.reason,
            "findings": [item.to_dict() for item in self.findings],
            "caveats": list(self.caveats),
            "noise_floor": self.noise_floor,
            "observed": {key: list(value) for key, value in sorted(self.observed.items())},
            "declared": None if self.declared is None else list(self.declared),
        }


@dataclass(frozen=True)
class Grouping:
    """Every path's verdict, and everything that bounds them."""

    version: str
    #: The walk every session here was recorded under. Not derived and not implied:
    #: it is what the caller asked for, and what the answer is only about.
    walk: str
    baseline: State
    arms: tuple[State, ...]
    classifications: tuple[Classification, ...]
    warnings: tuple[str, ...] = ()
    #: `toggle -> why its evidence could not be read`. Never counted, walked once,
    #: or the two sessions disagreeing — three different facts with three different
    #: fixes. Carried with the reason rather than as bare names, because the report
    #: printed "does not replicate" over an arm that was never counted, which is one
    #: fact in the words of another on the only real corpus there is.
    unreadable: Mapping[str, str] = field(default_factory=dict)

    @property
    def by_toggle(self) -> dict[str, tuple[str, ...]]:
        """`toggle -> the paths measured to be governed by it`, arms with none included.

        Every arm gets a key even when it governs nothing. A toggle that turned out
        to do nothing observable is a finding, and a dict that simply lacks the key
        reports it as a gap in the report.
        """

        out: dict[str, list[str]] = {state.arm: [] for state in self.arms if state.arm}
        for item in self.classifications:
            for finding in item.findings:
                out.setdefault(finding.toggle, []).append(item.endpoint)
        return {key: tuple(sorted(set(value))) for key, value in sorted(out.items())}


# ------------------------------------------------------------------- grouping


def partition(sessions: Iterable[ObservationSession]) -> tuple[State, ...]:
    """Group sessions by the toggle state their build reported, sorted by state.

    Sessions carrying no state are **dropped**, not gathered into a state of their
    own: they belong to no experiment, and inventing one for them is the
    operator-supplied configuration `observation` refuses. `classify` refuses
    before it gets here if any evidential session is in that shape.

    **Sessions naming more than one walk are a refusal**, and this is the second
    place that rule lives rather than the first being enough. `classify` selects by
    walk before it calls this, so the check can never fire from there — but
    `partition` and `noise_floors` are both exported, and

        noise_floors(partition(read(version, root)), endpoints)

    is one line, needs no argument anybody could get wrong, and reproduces the
    entire defect the walk field exists to prevent. What that costs is arithmetic
    and `test_the_floor_that_pooling_would_have_produced` pins it: the same twelve
    sessions give a floor of 0 within one walk and 8 pooled across two, on a corpus
    whose real effect is a fall of 4. A rule enforced only at the front door of a
    module is a rule the module's own exports walk around. `noise_floors` cannot
    check this itself — it is handed `State`s and never sees a walk — so it is
    checked at the last point where the information still exists.
    """

    rows = tuple(sessions)
    named = {item.walk for item in rows}
    if len(named) > 1:
        raise GroupingError(
            "these sessions name "
            + ", ".join(
                "no walk" if item is None else repr(item) for item in
                sorted(named, key=lambda item: (item is None, item or ""))
            )
            + ", and a state grouped across two driving protocols is not one "
            "experiment. Every difference between two runs of it includes the "
            "difference between the walks — which is what the noise floor is "
            "derived from, so the floor swallows exactly the effects it exists to "
            "let through. Select a walk first"
        )
    groups: dict[ToggleState, list[ObservationSession]] = {}
    for item in rows:
        if item.toggles is None:
            continue
        groups.setdefault(item.toggles, []).append(item)
    return tuple(
        State(toggles=state, sessions=tuple(rows))
        for state, rows in sorted(groups.items(), key=lambda pair: pair[0].text)
    )


def noise_floors(
    states: Sequence[State], endpoints: Iterable[str]
) -> dict[str, int | None]:
    """The largest difference two runs of one state produced, per path.

    Derived and never declared. Every state in the corpus contributes, including
    the ones where an effect is present: two sessions of one state are the same
    experiment, so whatever they differ by is not the toggle. That does mean a path
    whose counts are large carries a larger floor — `/feed/timeline/` reads 20 and
    23 under `disable_feed` and so must clear 3 — and it is the right direction to
    be wrong in.

    `None` for a path no state measured twice. Not `0`: a floor of zero says every
    difference is real, which is the most confident possible statement to make
    from a corpus that never repeated anything.
    """

    floors: dict[str, int | None] = {}
    for endpoint in endpoints:
        spreads = [
            spread
            for spread in (state.spread(endpoint) for state in states)
            if spread is not None
        ]
        floors[endpoint] = max(spreads) if spreads else None
    return floors


def _gap(inner: Sequence[int], outer: Sequence[int]) -> int:
    """How far two ranges are apart, or 0 when they touch or overlap.

    Whole ranges rather than averages. Two sessions per state means an average is
    two numbers over two, and an arm that overlaps its baseline has not shown the
    same thing twice — which is the replication rule, stated in arithmetic.
    """

    if not inner or not outer:
        return 0
    return max(min(inner) - max(outer), min(outer) - max(inner), 0)


def _accounts(
    counts: Mapping[str, int], total: int
) -> tuple[frozenset[str], bool]:
    """The paths that alone equal `total`, and whether several together could too.

    The second half is the one that matters, and it was missing until an
    adversarial pass produced the corpus that breaks without it. A toggle may block
    **more than one** live path — `manifest/hooks.json` declares three literals
    under `disable_feed` and five under `disable_reels` — and then the block total
    is their *sum*. A fourth, unrelated path whose own count happens to equal that
    sum is then the unique single-path explanation, and naming it is wrong twice
    over: it is not blocked, and the two that are read `unaffected`.

    Measured example, from live manifest shapes: `/feed/timeline/` 4 and
    `/feed/timeline_stream/` 1 both blocked gives 5 blocks, and
    `/discover/topical_explore` reading 5 was named as the blocked path.

    So a subset of two or more that also sums to the total makes the arm
    **unattributable**. Counted by the usual subset-sum table rather than
    enumerated, which is `O(paths x total)` and needs no cap on the watch list.
    Paths with no requests are excluded — a path nobody asked for cannot have been
    refused, and a zero would otherwise join any subset without changing its sum.
    """

    if total <= 0:
        return frozenset(), False
    live = {path: count for path, count in counts.items() if 0 < count <= total}
    singles = frozenset(path for path, count in live.items() if count == total)
    ways = [0] * (total + 1)
    ways[0] = 1
    for count in live.values():
        for value in range(total, count - 1, -1):
            ways[value] += ways[value - count]
    return singles, (ways[total] - len(singles)) > 0


def _attribute(arm: State, endpoints: Sequence[str]) -> tuple[str, ...]:
    """Which single path could account for every block this state reported.

    The block-accounting identity: with one toggle on, the guard throws once per
    request to a path that toggle blocks, so a path whose count equals the block
    total accounts for all of them. Two conditions, and both are needed.

    **Checked in every session and intersected**, because one session is routinely
    ambiguous — three of 439's watched paths read 3 in `439-reverse-stories`, and
    only one of them also reads 3 in the other.

    **And no combination of paths may explain the same total**, or the single-path
    answer is one explanation among several rather than the explanation. See
    :func:`_accounts`.

    Every watched path is a candidate, including ones no verdict could be given to.
    Filtering them out first could only turn an ambiguous answer into a confident
    wrong one.
    """

    candidates: set[str] | None = None
    for item in arm.sessions:
        if item.blocks is None:
            return ()
        singles, several = _accounts(
            {endpoint: item.counts.get(endpoint, 0) for endpoint in endpoints},
            item.blocks.total,
        )
        if several:
            return ()
        candidates = set(singles) if candidates is None else (candidates & singles)
    return tuple(sorted(candidates or ()))


def _declared_for(rules: Sequence[Any], endpoint: str) -> tuple[str, ...]:
    """The toggles whose rule has a literal matching `endpoint`, under its own kind.

    Applies each literal's **own** match kind rather than comparing strings, so
    `/clips/discover/stream/` reads as declared under `disable_reels` — the
    `/clips/discover` rule is a `contains` and catches it. Comparing text would
    have said "declared by nothing", which is the leading-slash grouping failure
    wearing different clothes.
    """

    found: set[str] = set()
    for rule in rules:
        for literal in rule.literals:
            hit = (
                literal.text in endpoint
                if literal.match == "contains"
                else endpoint.endswith(literal.text)
            )
            if hit:
                found.update(rule.toggles)
    return tuple(sorted(found))


def _rules(root: Path | str) -> tuple[tuple[Any, ...], str]:
    """The manifest's block rules, or `()` and the reason they could not be read.

    Never fatal. The manifest is context here; a report that refused to say what
    was *measured* because it could not read what is *declared* would have the
    dependency backwards.
    """

    from .guards import GuardError, rules_from_manifest  # noqa: PLC0415

    manifest = Path(root) / "manifest" / "hooks.json"
    try:
        return tuple(rules_from_manifest(manifest)), ""
    except (GuardError, OSError, json.JSONDecodeError, ValueError) as error:
        return (), f"{manifest}: {error}"


def classify(
    version: str,
    root: Path | str = ".",
    *,
    walk: str,
    path: Path | str | None = None,
) -> Grouping:
    """Derive the endpoint-to-toggle grouping for `version` from its sessions.

    `walk` is **required and names the driving protocol**, and the answer is over
    the sessions recorded under exactly that one. It can only *select*: a walk
    nobody recorded refuses instead of answering, so this is not the
    operator-supplies-the-safety-property shape — though the value in the store it
    selects from *was* typed by an operator, which the module docstring says
    plainly rather than leaving to be assumed. `observation.walks(version, root)`
    lists what is on record.

    Refuses, in this order: nothing recorded; every session vacuous; an evidential
    session stating no toggle state; no session recorded on the walk that was asked
    for; a walk whose own captures contradict it; no baseline; no single-toggle
    state; more than one build among the sessions the answer would rest on; and no
    literal watched by all of them. Each names what is missing, because each has a
    different fix.
    """

    if not isinstance(walk, str) or not walk.strip():
        raise GroupingError(
            f"walk must be the name of a driving protocol, got {walk!r}. Every "
            "comparison here is between two states measured the same way, and a "
            "session that made one pass over three surfaces is not comparable with "
            "one that made three rounds; `observation.walks(version, root)` lists "
            "the walks on record"
        )
    try:
        location = Path(path) if path is not None else store_path(version, root)
        sessions = read(version, root, path=location)
    except ObservationError as error:
        # One refusal channel. A caller of this module catches `GroupingError`,
        # and a bad version number leaking `ObservationError` past it would be an
        # escape that only shows up on the one input nobody tries.
        raise GroupingError(str(error)) from error

    if not sessions:
        raise GroupingError(
            f"there is no observation evidence for {version} ({location} holds no "
            "session). A grouping is a comparison between toggle states and there is "
            "nothing to compare"
        )
    usable = evidential(sessions)
    if not usable:
        raise GroupingError(
            f"all {len(sessions)} observation session(s) for {version} are vacuous: not "
            "one observed a single watched literal. A session that saw nothing is "
            "equally well explained by a build that was not observing, an empty capture "
            "and an app that never ran, so no difference between two of them means "
            "anything"
        )
    unstated = sorted(item.session_id for item in usable if item.toggles is None)
    if unstated:
        raise GroupingError(
            f"{len(unstated)} evidential session(s) for {version} state no toggle state: "
            + ", ".join(unstated)
            + ". They predate builds reporting their own configuration. A session that "
            "belongs to no state cannot be placed in the experiment, and the number of "
            "sessions per state is what the replication rule counts — so this is not a "
            "row to exclude quietly, it is a corpus whose design cannot be stated"
        )

    # Selected before anything is partitioned or measured. A floor derived from
    # two protocols is not a floor, so there is no stage of this that may see a
    # session recorded under another walk.
    on_walk = tuple(item for item in usable if item.walk == walk)
    if not on_walk:
        available = sorted({item.walk for item in usable if item.walk is not None})
        unwalked = sorted(item.session_id for item in usable if item.walk is None)
        raise GroupingError(
            f"no session for {version} was recorded on the walk {walk!r}. On record: "
            + ("; ".join(available) if available else "no walk at all")
            + (
                f". {len(unwalked)} evidential session(s) name no walk and cannot be "
                "compared against anything: " + ", ".join(unwalked)
                + ". They predate the field. Nothing can fill it in from the capture "
                "— a capture states which blocks were active and how long it ran, and "
                "never which script drove it — so where one is committed under "
                "`manifest/captures/<session_id>.log`, re-record from it with the "
                "`--walk` you actually ran: that reproduces every count, block and "
                "toggle and adds the one thing only you know. Where no capture was "
                "kept, the session has to be walked again"
                if unwalked
                else ""
            )
        )
    # Over everything claiming the walk, including the compound states `classify`
    # goes on to exclude by name. The question here is not "is this answer sound?"
    # but "does this name mean one thing in this store?", and a two-toggle session
    # filed under the name on a different protocol is evidence that it does not.
    contradiction = walk_dispute(on_walk)
    if contradiction:
        raise GroupingError(
            f"the {len(on_walk)} session(s) recorded on {walk!r} for {version} do not "
            "agree that they ran it: " + contradiction
            + ". A noise floor derived across two protocols is not a noise floor, and "
            "it is wrong in the direction that looks like an answer"
        )

    states = partition(on_walk)
    baselines = [state for state in states if state.is_baseline]
    if not baselines:
        raise GroupingError(
            f"no session for {version} was measured with every toggle off. The baseline "
            "is what every arm is compared against, and without it a count under one "
            "toggle has nothing to be a change *from*. The states on record are: "
            + "; ".join(state.text for state in states)
        )
    if len(baselines) > 1:
        # Two states can both report every toggle off while naming *different keys*:
        # a version that grew a sixth toggle has not measured the same experiment as
        # one that had five. Taking the first by sort order dropped the other's
        # sessions out of `used` with no warning, and — because they left before the
        # build check — took their build with them.
        raise GroupingError(
            f"{len(baselines)} different states for {version} report every toggle off, "
            "over different keys: "
            + "; ".join(
                f"{item.text} ({', '.join(item.session_ids)})" for item in baselines
            )
            + ". A build that reads a different set of preferences is not the same "
            "experiment, and there is no baseline for the arms to be compared against "
            "until one of them is named"
        )
    baseline = baselines[0]
    arms = tuple(state for state in states if state.arm is not None)
    if not arms:
        raise GroupingError(
            f"no session for {version} was measured with exactly one toggle on. An "
            "effect seen with two on belongs to either of them or to the pair, and "
            "nothing in the record separates those. The states on record are: "
            + "; ".join(state.text for state in states)
        )

    used = [*baseline.sessions, *(item for state in arms for item in state.sessions)]
    builds = sorted({item.build_sha256 for item in used})
    if len(builds) > 1:
        raise GroupingError(
            f"the {len(used)} session(s) this grouping would rest on come from "
            f"{len(builds)} builds: "
            + ", ".join(item[:12] for item in builds)
            + ". A toggle name is not a rule — what `disable_feed` blocks is decided by "
            "the manifest its build was rendered from — so a difference between two "
            "states would be a difference between two builds. `observation report` only "
            "warns about this because it answers within one state; this compares across "
            "them"
        )

    warnings: list[str] = []
    vacuous = sorted(item.session_id for item in sessions if item.vacuous)
    if vacuous:
        warnings.append(
            f"{len(vacuous)} of {len(sessions)} session(s) observed nothing and are "
            "excluded: " + ", ".join(vacuous)
            + ". A state left with one usable session classifies nothing"
        )
    compound = [state for state in states if not state.is_baseline and state.arm is None]
    if compound:
        warnings.append(
            f"{len(compound)} state(s) turn on more than one toggle and are excluded: "
            + "; ".join(f"{state.text} ({', '.join(state.session_ids)})" for state in compound)
            + ". An effect there belongs to either toggle or to the pair"
        )
    single = [state for state in (baseline, *arms) if not state.replicated]
    if single:
        warnings.append(
            f"{len(single)} state(s) were walked once and nothing is classified from "
            "them: "
            + "; ".join(f"{state.text} ({', '.join(state.session_ids)})" for state in single)
            + ". A finding that holds in one running order and not the other is an "
            "artefact, and one order cannot show which it is"
        )
    surfaces = sorted({item.surface for item in used})
    warnings.append(
        f"every verdict is bounded by the walk ({walk}) and by the surfaces it "
        "covered: " + ", ".join(surfaces)
        + ". A path only the Reels player requests is not observed by a session that "
        "stayed on the feed, and a path only a third round reaches is not observed by "
        "a walk that made one pass; either silence is about the session"
    )
    elsewhere = sorted(
        item.session_id for item in usable if item.walk is not None and item.walk != walk
    )
    if elsewhere:
        warnings.append(
            f"{len(elsewhere)} evidential session(s) were recorded on another walk and "
            "are excluded: " + ", ".join(elsewhere)
            + ". They are not worse evidence, they are evidence about a different "
            "protocol, and a difference measured across two of those is partly the "
            "difference between them"
        )
    unwalked = sorted(item.session_id for item in usable if item.walk is None)
    if unwalked:
        warnings.append(
            f"{len(unwalked)} evidential session(s) name no walk and are excluded: "
            + ", ".join(unwalked)
            + ". They predate the field and nothing in a capture can supply it; "
            "re-recording them from `manifest/captures/` with the walk that was run "
            "would make them comparable again"
        )
    # The positive control on the one check that guards a typed value. A check
    # that silently could not have fired is the failure this project keeps
    # repeating, so when it goes inert it says so where its verdict would go.
    inert = walk_evidence(on_walk)
    if inert:
        warnings.append(
            f"the walk {walk} is claimed and only partly evidenced: " + inert
            + ". The name was typed by whoever ran the session; the capture span is "
            "the only thing that can contradict it"
        )

    if not baseline.counted:
        warnings.append(
            f"the baseline {baseline.text} has session(s) with no block count: "
            + ", ".join(
                item.session_id for item in baseline.sessions if item.blocks is None
            )
            + ". Nothing can be called blocked while the control was never counted; "
            "re-record those sessions from their captures"
        )
    elif any(total for total in baseline.block_totals):
        warnings.append(
            f"the baseline {baseline.text} reports blocks "
            + baseline.blocks_text
            + " with every toggle off. Nothing should throw in that state, so either the "
            "capture spans a configuration change or the build is not the one it says. "
            "No path is called blocked while this holds"
        )

    baseline_clean = baseline.counted and not any(baseline.block_totals)

    # The population: what every session the answer rests on was watching. A path
    # one session watched and another did not has a zero in the second that means
    # "not looked for", and that is not the zero any verdict here is about.
    watched_sets = [set(item.watched) for item in used]
    endpoints = sorted(set.intersection(*watched_sets))
    if not endpoints:
        # An empty answer where "nothing was measured" is the truth. Every session
        # here watched something, so this is the shape where they watched
        # *different* things and no path was looked for by all of them — one build
        # per state, and the comparison is between two watch lists.
        raise GroupingError(
            f"no literal was watched by all {len(used)} session(s) for {version}, so "
            "there is no path any two states can be compared over. The watch lists are: "
            + "; ".join(
                f"{item.session_id} ({len(item.watched)})" for item in used
            )
        )
    partial = sorted(set.union(*watched_sets) - set(endpoints))
    if partial:
        warnings.append(
            f"{len(partial)} literal(s) were watched by some sessions and not others and "
            "are excluded: " + ", ".join(partial)
            + ". Their zero in a session that was not watching them means 'not looked "
            "for', which is not the silence any verdict here is about"
        )

    floors = noise_floors(states, endpoints)
    rules, rules_refusal = _rules(root)
    if rules_refusal:
        warnings.append(
            "what each path is declared under today could not be read, so the report "
            "shows measurement only: " + rules_refusal
        )

    # Three lists on purpose. `unreadable_arms` is toggle names, and is what the
    # BY TOGGLE view needs so that "governs nothing" and "we could not tell" never
    # share a line. `blind` is the short form that goes into every affected path's
    # reason, where five arms' worth of session ids would bury the sentence that
    # matters. `detail` is the long form for the WARNINGS block, where a reader
    # wants the session ids.
    #
    # An arm walked **once** belongs here and not only in the warning above. It is
    # already excluded from producing findings, and for a while that was all it
    # was: the corpus warned about it and `unaffected` carried on being returned
    # over it. A warning beside an assertion that still passes is the failure this
    # project keeps repeating, and "no toggle affects this path" cannot be said
    # while one of the toggles was tried once.
    unreadable_arms: dict[str, str] = {}
    blind: list[str] = []
    detail: list[str] = []
    for arm in arms:
        if not arm.replicated:
            unreadable_arms[arm.arm or ""] = "walked once, so nothing replicates"
            blind.append(f"{arm.arm} (walked once)")
            detail.append(
                f"{arm.arm}: only {', '.join(arm.session_ids)} — one running order "
                "cannot show whether a finding is a finding or an artefact"
            )
        elif not arm.counted:
            unreadable_arms[arm.arm or ""] = "its blocks were never counted"
            blind.append(f"{arm.arm} (no block count)")
            detail.append(
                f"{arm.arm}: no block count in "
                + ", ".join(
                    item.session_id for item in arm.sessions if item.blocks is None
                )
                + " — recorded before this host counted them; re-record from the capture"
            )
        elif arm.blocks_replicate is None:
            unreadable_arms[arm.arm or ""] = (
                f"its two sessions report blocks {arm.blocks_text} and disagree"
            )
            blind.append(f"{arm.arm} (blocks {arm.blocks_text})")
            detail.append(
                f"{arm.arm}: blocks "
                + ", ".join(
                    f"{item.session_id} {item.blocks.total}"  # type: ignore[union-attr]
                    for item in arm.sessions
                )
                + " — the block signal does not replicate across the two running orders, "
                "and Instagram emits these events at its discretion, so the zero is not "
                "proof that nothing was refused"
            )
    # The baseline is every arm's control, so a hole in it is a hole in all of them.
    # It does not go in `unreadable_arms`, which names toggles.
    if not baseline.replicated:
        blind.append("the baseline (walked once)")
        detail.append(
            f"the baseline: only {', '.join(baseline.session_ids)} — with one walk "
            "there is no measured spread for it, and every comparison is against a "
            "single number"
        )
    elif not baseline_clean:
        blind.append("the baseline (block evidence unusable)")
    if detail:
        warnings.append(
            f"evidence is unreadable for {len(detail)} state(s), so no path can be "
            "called blocked by them and no path can be called unaffected at all: "
            + "; ".join(detail)
        )

    classifications: list[Classification] = []
    for endpoint in endpoints:
        classifications.append(
            _classify_one(
                endpoint=endpoint,
                population=endpoints,
                baseline=baseline,
                arms=arms,
                states=states,
                floor=floors[endpoint],
                baseline_clean=baseline_clean,
                unreadable=tuple(blind),
                declared=None if rules_refusal else _declared_for(rules, endpoint),
            )
        )

    governing = {
        finding.toggle for item in classifications for finding in item.findings
    }
    # "Governs nothing" and "could not be read" are two different facts and the
    # second must not be reported in the words of the first. `disable_explore` on
    # 439 governs nothing *in this answer* because its block signal contradicts
    # itself, which is a reason to walk it again — not a finding about the toggle.
    blocked_from_answering = set(unreadable_arms)  # a dict; membership is by key
    idle = sorted(
        arm.arm
        for arm in arms
        if arm.arm and arm.arm not in governing
        and arm.arm not in blocked_from_answering
    )
    if idle:
        warnings.append(
            f"{len(idle)} toggle(s) were readable and govern nothing observable: "
            + ", ".join(idle)
            + ". On these surfaces, with this account, nothing they could block was "
            "ever requested and nothing they did block was ever reported"
        )
    silent = sorted(
        arm.arm
        for arm in arms
        if arm.arm and arm.arm not in governing
        and arm.arm in blocked_from_answering
    )
    if silent:
        warnings.append(
            f"{len(silent)} toggle(s) govern nothing in this answer only because their "
            "evidence could not be read: " + ", ".join(silent)
            + ". That is a session to walk again, not a finding about the toggle"
        )
    return Grouping(
        version=version,
        walk=walk,
        baseline=baseline,
        arms=arms,
        classifications=tuple(classifications),
        warnings=tuple(warnings),
        unreadable=dict(sorted(unreadable_arms.items())),
    )


def _classify_one(
    *,
    endpoint: str,
    #: Every path the answer is over. The block-accounting identity is a statement
    #: about *all* of them — "this is the only one whose count is the whole total" —
    #: so it cannot be computed from the one path being classified.
    population: Sequence[str],
    baseline: State,
    arms: Sequence[State],
    states: Sequence[State],
    floor: int | None,
    baseline_clean: bool,
    unreadable: Sequence[str],
    declared: tuple[str, ...] | None,
) -> Classification:
    """One path's verdict. Every branch names what it rests on."""

    observed = {state.label: state.counts(endpoint) for state in states}
    common = dict(
        endpoint=endpoint,
        noise_floor=floor,
        observed=observed,
        declared=declared,
    )
    seen_anywhere = any(any(counts) for counts in observed.values())
    if not seen_anywhere:
        return Classification(
            verdict=NEVER_REQUESTED,
            toggle=None,
            reason=(
                "zero in every session of every state. Watched for, walked for, and "
                "never once requested — recorded, not built"
            ),
            **common,
        )

    baseline_counts = baseline.counts(endpoint)
    if not all(baseline_counts):
        return Classification(
            verdict=UNCLASSIFIABLE,
            toggle=None,
            reason=(
                f"observed {', '.join(str(item) for item in baseline_counts)} across the "
                f"{len(baseline_counts)} baseline session(s), so it is not reliably "
                "requested with every toggle off. A zero under an arm cannot be told "
                "from a zero the baseline produces on its own, and a rise cannot be told "
                "from the same variability"
            ),
            **common,
        )

    findings: list[Finding] = []
    caveats: list[str] = []
    for arm in arms:
        if not arm.replicated:
            continue
        arm_counts = arm.counts(endpoint)
        gap = _gap(arm_counts, baseline_counts)
        moved = floor is not None and gap > floor

        if not any(arm_counts) and moved:
            findings.append(
                Finding(
                    kind=ERASED,
                    toggle=arm.arm or "",
                    reason=(
                        f"observed {', '.join(str(item) for item in baseline_counts)} in "
                        f"every baseline session and 0 in every session of {arm.label}; "
                        f"the fall of {gap} is larger than the {floor} two runs of one "
                        "state ever differed by. The request never reaches the guard, "
                        "which is what an erasure upstream of the URL looks like"
                    ),
                    corroboration=(
                        f"{arm.arm} reported {arm.blocks_text} block(s): an erased "
                        "path cannot be blocked, because nothing "
                        "ever reaches the code that throws",
                    ),
                )
            )
            continue

        if (
            baseline_clean
            and arm.blocks_replicate is True
            and all(arm_counts)
        ):
            attributed = _attribute(arm, population)
            if attributed == (endpoint,):
                totals = arm.blocks_text
                features = arm.features
                findings.append(
                    Finding(
                        kind=BLOCKED,
                        toggle=arm.arm or "",
                        reason=(
                            f"{arm.label} reported {totals} block(s) and the baseline "
                            "reported none; this is the only watched path whose own count "
                            f"({', '.join(str(item) for item in arm_counts)}) accounts for "
                            "the whole total in every session of the state. The path is "
                            "still requested, which is what a block does and an erasure "
                            "does not"
                        ),
                        corroboration=tuple(
                            [
                                "the feature named above each block header was "
                                + ", ".join(
                                    f"{name} {count}" for name, count in features.items()
                                )
                                + " — corroboration only; a feature is not a path"
                            ]
                            + (
                                [
                                    f"the count also moved by {gap}, more than the "
                                    f"{floor} two runs of one state ever differed by"
                                ]
                                if moved
                                else [
                                    "the count did not move beyond noise, which is why "
                                    "the block signal is needed at all"
                                ]
                            )
                        ),
                    )
                )
                continue

        if moved:
            caveats.append(
                f"{arm.label} moved it by {gap}, more than its floor of {floor}, and "
                "nothing here accounts for that: "
                + f"{arm.arm} reported {arm.blocks_text} block(s)"
                + f", counts {', '.join(str(item) for item in arm_counts)} against a "
                f"baseline of {', '.join(str(item) for item in baseline_counts)}"
            )

    if len(findings) == 1:
        finding = findings[0]
        return Classification(
            verdict=finding.kind,
            toggle=finding.toggle,
            reason=finding.reason,
            findings=tuple(findings),
            caveats=tuple(caveats),
            **common,
        )
    if len(findings) > 1:
        return Classification(
            verdict=UNCLASSIFIABLE,
            toggle=None,
            reason=(
                "more than one toggle was measured to affect it — "
                + "; ".join(f"{item.kind} by {item.toggle}" for item in findings)
                + ". The question this answers is which toggle a path belongs under, and "
                "two answers is not an answer; a human has to read both"
            ),
            findings=tuple(findings),
            caveats=tuple(caveats),
            **common,
        )

    if floor is None:
        return Classification(
            verdict=UNCLASSIFIABLE,
            toggle=None,
            reason=(
                "no state was walked twice, so there is no measured difference between "
                "two runs of one experiment and no floor to compare anything against"
            ),
            caveats=tuple(caveats),
            **common,
        )
    if caveats:
        # The caveats *are* the reason here, so they are not repeated as caveats:
        # a report that prints one sentence twice teaches the reader to skim it.
        return Classification(
            verdict=UNCLASSIFIABLE,
            toggle=None,
            reason=(
                "a toggle moved it beyond the noise floor and no mechanism accounts for "
                "the movement: " + "; ".join(caveats)
            ),
            **common,
        )
    if unreadable:
        return Classification(
            verdict=UNCLASSIFIABLE,
            toggle=None,
            reason=(
                "no toggle moved its count beyond the noise floor, but 'no toggle affects "
                "it' is a claim about every toggle, and the block evidence cannot be read "
                "for " + ", ".join(unreadable)
                + ". A path is only unaffected when every arm could have shown otherwise; "
                "see the warnings for what each arm is missing"
            ),
            caveats=tuple(caveats),
            **common,
        )
    return Classification(
        verdict=UNAFFECTED,
        toggle=None,
        reason=(
            f"every arm was readable, none erased it, none blocked it, and none moved "
            f"its count by more than the {floor} two runs of one state differed by"
        ),
        caveats=tuple(caveats),
        **common,
    )


# ------------------------------------------------------------------ reporting


def summary(
    version: str,
    root: Path | str = ".",
    *,
    walk: str,
    path: Path | str | None = None,
) -> dict[str, Any]:
    """Everything a report says, in one shape, so both output forms read it.

    One producer for both views. A human banner and a machine field going out of
    step is a defect this project has shipped — the JSON a script gates on was
    missing the warning the human form printed — so the refusal lands *in* the
    document rather than only on stderr, and `render` prints what it finds.
    """

    try:
        grouping = classify(version, root, walk=walk, path=path)
    except GroupingError as error:
        return {
            "schema_version": SCHEMA_VERSION,
            "version": version,
            "walk": walk,
            "unanswerable_reason": str(error),
            "baseline": None,
            "arms": [],
            "verdicts": [],
            "by_toggle": {},
            "unreadable_toggles": {},
            "warnings": [str(error)],
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "version": version,
        "walk": grouping.walk,
        "unanswerable_reason": "",
        "baseline": {
            "toggles_text": grouping.baseline.text,
            "session_ids": list(grouping.baseline.session_ids),
            "counted": grouping.baseline.counted,
            "blocks": list(grouping.baseline.block_totals),
            "blocks_text": grouping.baseline.blocks_text,
            "spans": list(grouping.baseline.spans),
        },
        "arms": [
            {
                "toggle": arm.arm,
                "toggles_text": arm.text,
                "session_ids": list(arm.session_ids),
                "replicated": arm.replicated,
                "counted": arm.counted,
                "blocks": list(arm.block_totals),
                "blocks_text": arm.blocks_text,
                "blocks_replicate": arm.blocks_replicate,
                "features": arm.features,
                "spans": list(arm.spans),
            }
            for arm in grouping.arms
        ],
        "verdicts": [item.to_dict() for item in grouping.classifications],
        "by_toggle": {key: list(value) for key, value in grouping.by_toggle.items()},
        "unreadable_toggles": dict(grouping.unreadable),
        "warnings": list(grouping.warnings),
    }


_ORDER = (BLOCKED, ERASED, UNAFFECTED, UNCLASSIFIABLE, NEVER_REQUESTED)


def _spans_text(spans: Sequence[Any]) -> str:
    """`116s, 118s` — or `unmeasured`, which is not the same as `0s`."""

    return ", ".join("unmeasured" if item is None else f"{item}s" for item in spans)


def render(report: Mapping[str, Any]) -> str:
    lines = [
        f"GROUPING  {report['version']}   walk {report['walk']}", "=" * 72, "",
    ]

    if report["unanswerable_reason"]:
        lines += [
            "  NOTHING CAN BE DERIVED", "",
            f"    {report['unanswerable_reason']}", "",
        ]
        return "\n".join(lines)

    baseline = report["baseline"]
    lines.append(
        f"  BASELINE  {baseline['toggles_text']}"
    )
    lines.append(
        f"    {', '.join(baseline['session_ids'])}   blocks {baseline['blocks_text']}"
        f"   spans {_spans_text(baseline['spans'])}"
    )
    lines.append("")
    lines.append("  ARMS")
    lines.append("")
    for arm in report["arms"]:
        if not arm["counted"]:
            replicate = "UNREADABLE (never counted)"
        elif not arm["replicated"]:
            replicate = "UNREADABLE (walked once)"
        else:
            replicate = {
                True: "blocks in every session",
                False: "no block in any session",
                None: "UNREADABLE (the two sessions disagree)",
            }[arm["blocks_replicate"]]
        lines.append(
            f"    {arm['toggle']:<18} {', '.join(arm['session_ids'])}"
            f"   spans {_spans_text(arm['spans'])}"
        )
        lines.append(
            f"      blocks {arm['blocks_text']}   {replicate}"
            + (
                "   "
                + ", ".join(f"{k} {v}" for k, v in sorted(arm["features"].items()))
                if arm["features"]
                else ""
            )
        )
    lines.append("")

    for verdict in _ORDER:
        rows = [item for item in report["verdicts"] if item["verdict"] == verdict]
        if not rows:
            continue
        lines.append(f"  {verdict.upper().replace('_', ' ')}")
        lines.append("")
        for row in rows:
            head = f"    {row['endpoint']}"
            if row["toggle"]:
                head += f"   <- {row['toggle']}"
            lines.append(head)
            declared = row["declared"]
            if declared is None:
                lines.append("      declared today: unknown (manifest unreadable)")
            else:
                lines.append(
                    "      declared today: "
                    + (", ".join(declared) if declared else "nothing")
                )
            lines.append(
                "      counts: "
                + "   ".join(
                    f"{state}={'/'.join(str(item) for item in counts)}"
                    for state, counts in sorted(
                        row["observed"].items(),
                        key=lambda pair: (pair[0] != "baseline", pair[0]),
                    )
                )
            )
            lines.append(f"      floor: {row['noise_floor']}")
            lines.append(f"      {row['reason']}")
            for finding in row["findings"]:
                for note in finding["corroboration"]:
                    lines.append(f"      + {note}")
            for note in row["caveats"]:
                lines.append(f"      ! {note}")
            lines.append("")

    lines.append("  BY TOGGLE")
    lines.append("")
    for toggle, paths in sorted(report["by_toggle"].items()):
        if paths:
            answer = ", ".join(paths)
        elif toggle in report["unreadable_toggles"]:
            answer = f"UNREADABLE — {report['unreadable_toggles'][toggle]}"
        else:
            answer = "nothing observable"
        lines.append(f"    {toggle:<18} {answer}")
    lines.append("")

    if report["warnings"]:
        lines += ["  WARNINGS", ""]
        for warning in report["warnings"]:
            lines.append(f"    {warning}")
        lines.append("")

    lines.append(
        "  This is a view over the sessions, recomputed every time and stored nowhere."
    )
    lines.append(
        "  Acting on it means a human editing url_block_rules in manifest/hooks.json."
    )
    return "\n".join(lines)


# ------------------------------------------------------------------------ CLI


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path("."))
    sub = parser.add_subparsers(dest="command", required=True)

    report = sub.add_parser(
        "report", help="which endpoints belong to which toggle, from measurement"
    )
    report.add_argument("--version", required=True)
    report.add_argument(
        "--walk",
        required=True,
        help="which driving protocol to compare within, e.g. three-round-v2. "
        "Required rather than inferred: a comparison across two walks measures the "
        "walks. `observation report` lists the ones on record",
    )
    report.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    try:
        data = summary(args.version, args.root, walk=args.walk)
    except (ObservationError, GroupingError, ValueError, OSError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        print(render(data))
    # A refusal exits 2 in *both* forms, and the reason is in the document either
    # way. Exit 0 otherwise: this measures, it does not gate.
    return 2 if data["unanswerable_reason"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

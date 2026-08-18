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
`observation.parse` reads both: the watched paths the app asked for, and the
`!blocked <literal>` lines the guard writes as it refuses them.

===============================================================================
  HOW A BLOCK IS ATTRIBUTED TO A PATH
===============================================================================

**The guard says so.** An observing build calls
`observe;->blocked("<literal>")` immediately before it throws, naming the literal
that matched, so the capture carries a refusal count per path and this module
reads it. A path is BLOCKED by toggle T when, under the state with only T on,
every session recorded at least one refusal of it, the baseline recorded none,
and the path was still requested — that last part is what tells a block from an
erasure, and it is the only part that comes from the counts.

**What that replaced, and why it had to go.** Until 2026-08-13 the only block
evidence was `java.io.IOException: Blocked by DFInsta setting` grepped out of
logcat. That line is there because *Instagram* catches our exception and files it
into its own error event — it cost nothing to read, so it became the signal. It
names a **feature** (`FEED_NOT_LOADING`) and never a path, so a path had to be
named by arithmetic: with one toggle on the guard throws once per request to a
path that toggle blocks, so

    blocks in the capture  ==  sum of the counts of the paths that toggle blocks

and a path whose own count equalled the total, uniquely and with no combination
of other paths also equalling it, was the blocked one. Two things killed it:

* **It under-reports, by feature and consistently.** `/discover/topical_explore`
  was refused 7, 6, 12 and 6 times across eight sessions on two Instagram
  versions and two walk protocols, and reported 1, 0, 1 and 0 — while
  `/feed/timeline/` reported 20/20, 23/23, 17/17 and 16/16 in the very same
  captures. Explore was simply unmeasurable through that channel.
* **The ambiguity check refused correct answers on real data.** In
  `439-3r-reverse-feed` `/feed/timeline/` had 17 requests and 17 blocks — exact —
  and was declined because `7 + 7 + 3 = 17` among three unrelated paths. Longer
  walks raise counts, more live paths give more coinciding subsets, so the
  derivation got *less* reliable exactly as the measurement got better. That is
  most of why the two walks disagreed about a version they agreed about.

Instagram's count is still parsed and still printed, beside ours and labelled.
**Nothing is derived from it.** A reader seeing 17 refusals recorded and 3 events
reported is seeing the reason this module stopped asking.

**A watched path that no rule names cannot have its own refusal count.** A refusal
names the literal the *rule* tested, so `/api/v1/clips/discover/stream/` refused by
the `/clips/discover` rule is recorded under `/clips/discover`. `_declared_for`
says which rules cover a path, and the report says "covered by" rather than
inventing a count.

**A build that could not report refusals says nothing, and that is not zero.** Its
sessions have `refusals is None`, its arm is unreadable by name, and no path is
called blocked or unaffected on its evidence. Every row committed before
2026-08-13 is in that shape, so 439's block half is unanswerable until it is
walked again — and unlike the walk, which the captures could supply, nothing can
be back-filled, because the lines were never written.

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
    def reporting(self) -> tuple[ObservationSession, ...]:
        """The sessions of this state whose build could report its own refusals.

        A build that never claimed `+blocked` could not have written a refusal
        line, so its silence is not a zero — `ObservationSession.refusals` is
        `None` and it is not here. Every row committed before 2026-08-13 is in
        that shape.

        **Answering from a subset is not cherry-picking, and the difference is
        that the subset is chosen before any answer is looked at.** These sessions
        are selected by a property of the build that produced them, exactly as
        `observation.stated` selects the sessions that named their toggle state
        and `walked` those that named their walk. A session with no refusal report
        does not disagree with one that has it; it says nothing, and pooling
        silence with evidence would let an old row veto a measurement rather than
        contribute to it.

        Its **counts** are still used, by every comparison in this module that
        rests on counts. Only the refusal questions are narrowed.
        """

        return tuple(item for item in self.sessions if item.refusals is not None)

    @property
    def reported(self) -> bool:
        """Can this state answer a refusal question, replicated?

        Two sessions at least, because one running order cannot tell a finding
        from an artefact — the same rule the count side has, applied to the
        sessions that can actually answer.
        """

        return len(self.reporting) > 1

    @property
    def partly_reporting(self) -> bool:
        """Some sessions can report refusals and some cannot.

        A different fact from "none can", with a different repair: the older rows
        are silent and can be withdrawn, where a state none of whose sessions can
        report needs walking again. The report says which.
        """

        return 0 < len(self.reporting) < len(self.sessions)

    def refusals(self, endpoint: str) -> tuple[int, ...] | None:
        """How often each *reporting* session refused `endpoint`, in recorded order.

        `None` when fewer than two sessions could report at all — absent, not
        zero. Otherwise every entry is a measurement, including the zeroes: a
        literal missing from a session's refusals was watched, requested or not,
        and not refused.
        """

        if not self.reported:
            return None
        return tuple(item.refusals.get(endpoint) for item in self.reporting)  # type: ignore[union-attr]

    def refusals_text(self, endpoint: str) -> str:
        """`20, 23` — or `not reported`, which is not the same as `0, 0`."""

        measured = self.refusals(endpoint)
        if measured is None:
            return "not reported"
        return ", ".join(str(count) for count in measured)

    @property
    def probing(self) -> tuple[Any, ...]:
        """The sessions that looked for probes at all. Same rule as `reporting`."""

        return tuple(item for item in self.sessions if item.probes is not None)

    @property
    def probed(self) -> bool:
        """Can this state answer "did our code run", replicated?"""

        return len(self.probing) > 1

    def executions(self, hook_id: str) -> tuple[int, ...] | None:
        """How often `hook_id` reported executing, per probing session.

        `None` when fewer than two sessions looked — absent, not zero. Otherwise
        every entry is a measurement, zeroes included: a hook missing from a
        session's probes was instrumented and did not run.
        """

        if not self.probed:
            return None
        return tuple(item.probes.count(hook_id) for item in self.probing)  # type: ignore[union-attr]

    def surfaces_for(self, endpoint: str) -> tuple[tuple[str, int], ...]:
        """`(surface, requests)` for one path across every annotated session here.

        Empty when no session in this state was walked with annotation — absent,
        not "seen nowhere". Summed across sessions because the question is which
        surface a path belongs to, and one session is not evidence of that.
        """

        totals: dict[str, int] = {}
        for item in self.sessions:
            if item.per_surface is None:
                continue
            for surface, count in item.per_surface.surfaces_for(endpoint):
                totals[surface] = totals.get(surface, 0) + count
        return tuple(sorted(totals.items(), key=lambda pair: (-pair[1], pair[0])))

    @property
    def refused_total(self) -> int | None:
        """Every refusal this state made, across all paths. `None` if unreportable.

        The baseline's is the one that matters: with every toggle off nothing
        should throw, so anything but zero means the build's own state line and
        its behaviour disagree and no verdict taken against it is safe.
        """

        if not self.reported:
            return None
        return sum(item.refusals.total for item in self.reporting)  # type: ignore[union-attr]


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
    #: What OUR code did, from the build's own probe lines: one line per hook that
    #: declares this literal, saying whether its patched site executed in each
    #: state. Its own field and not a caveat, because a caveat here is about a
    #: movement nothing explains, and this is about whether the machinery ran at
    #: all — the two read as each other if they share a list.
    execution: tuple[str, ...] = ()
    #: Where the app asked for it — which surface was on screen. Its own field for
    #: the same reason `execution` is: three different questions sharing one list
    #: is three questions nobody reads.
    seen_on: tuple[str, ...] = ()
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
            "execution": list(self.execution),
            "seen_on": list(self.seen_on),
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



def hooks_owning(literal: str, root: Path | str) -> tuple[str, ...]:
    """Which hooks declare `literal` as something they act on.

    Read from `semantic_deps`, which is the manifest's own statement of what a
    hook is about — not from `url_block_rules`, which belong to `tigon_url_block`
    alone and so say nothing about the Reels endpoints, whose hooks blank a path
    upstream instead of refusing a request.

    Never fatal, like every other manifest read here: a report that could not say
    which hook owns a path is worth less than one that says so and carries on.
    """

    from .assessment import spellings  # noqa: PLC0415
    from .hook_manifest import load_manifest  # noqa: PLC0415

    try:
        hooks = load_manifest(Path(root) / "manifest" / "hooks.json")
    except Exception:  # noqa: BLE001 - context, never the answer
        return ()
    wanted = {form for spelling in (literal,) for form in spellings(spelling)}
    owning = [
        hook.hook_id
        for hook in hooks
        if hook.status == "active"
        and any(form in wanted for dep in hook.semantic_deps for form in spellings(dep))
    ]
    return tuple(sorted(owning))


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

    if not baseline.reported:
        warnings.append(
            f"the baseline {baseline.text} has session(s) whose build could not report "
            "refusals: "
            + ", ".join(
                item.session_id for item in baseline.sessions if item.refusals is None
            )
            + ". Nothing can be called blocked while the control cannot say it refused "
            "nothing — and unlike the walk, this cannot be back-filled from the "
            "captures, because the build never wrote the lines. Walk it again with a "
            "build that claims `+blocked`"
        )
    elif baseline.refused_total:
        warnings.append(
            f"the baseline {baseline.text} refused "
            + str(baseline.refused_total)
            + " request(s) with every toggle off. Nothing should throw in that state, so "
            "either the capture spans a configuration change or the build is not the one "
            "it says. No path is called blocked while this holds"
        )
    if baseline.counted and any(total for total in baseline.block_totals):
        # Instagram's own count, kept as a second opinion and never as a basis.
        # It disagreeing with ours is worth saying; it is not worth deciding on.
        warnings.append(
            f"the baseline {baseline.text} also has Instagram reporting blocks "
            + baseline.blocks_text
            + " with every toggle off — corroboration only, and these events go missing"
        )

    baseline_clean = baseline.reported and not baseline.refused_total

    # Said once, over every state, and deliberately not as a refusal. A state with
    # both kinds of session answers from the ones that can answer, and the reader
    # has to be told how many that was — otherwise "blocked, in every session of
    # the state" reads as four sessions when it was two.
    mixed = [item for item in states if item.partly_reporting]
    if mixed:
        warnings.append(
            f"{len(mixed)} state(s) hold sessions from builds that report refusals and "
            "builds that cannot, so every refusal answer rests on the first kind only: "
            + "; ".join(
                f"{item.label} {len(item.reporting)} of {len(item.sessions)}"
                for item in mixed
            )
            + ". The silent rows still supply their request counts. Withdrawing them "
            "would make the two populations one again"
        )

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
        elif not arm.reported:
            unreadable_arms[arm.arm or ""] = "its build could not report refusals"
            blind.append(f"{arm.arm} (refusals not reported)")
            detail.append(
                f"{arm.arm}: {len(arm.reporting)} of {len(arm.sessions)} session(s) can "
                "report refusals, and two are needed — "
                + ", ".join(
                    item.session_id for item in arm.sessions if item.refusals is None
                )
                + " never claimed `+blocked`, so they could not have written a refusal "
                "line and their silence is not a zero. Nothing can be re-recorded from "
                "those captures; only re-measuring repairs it"
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
        blind.append("the baseline (refusal evidence unusable)")
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
                baseline=baseline,
                arms=arms,
                states=states,
                floor=floors[endpoint],
                baseline_clean=baseline_clean,
                unreadable=tuple(blind),
                declared=None if rules_refusal else _declared_for(rules, endpoint),
                root=root,
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


def _where_seen(
    endpoint: str,
    baseline: State,
    arms: Sequence[State],
) -> tuple[str, ...]:
    """Which surface was on screen when this path was requested, across all states.

    Kept apart from `_execution` because they answer different questions and the
    same care applies as with caveats: `_execution` is what OUR code did, this is
    where the APP asked, and a reader who has to work out which is which will
    stop reading both.

    Summed across every annotated session in every state, busiest surface first.
    One session is not evidence of where a path belongs. Silent when no session
    was walked with annotation — absent, never "seen nowhere".
    """

    totals: dict[str, int] = {}
    for state in (baseline, *arms):
        for surface, count in state.surfaces_for(endpoint):
            totals[surface] = totals.get(surface, 0) + count
    if not totals:
        return ()
    busiest = sorted(totals.items(), key=lambda pair: (-pair[1], pair[0]))
    return (
        "requested on " + ", ".join(f"{surface} x{count}" for surface, count in busiest),
    )


def _execution(
    endpoint: str,
    baseline: State,
    arms: Sequence[State],
    root: Path | str,
) -> tuple[str, ...]:
    """What our own code did about this path, per state, from the probe lines.

    A probe count says a patched site RAN. It never says a request was blocked —
    the site executes in every toggle state, because the toggle is tested inside
    the code the probe sits beside. What it answers is the other question, and it
    is the one counting cannot: a fall with no execution behind it is not ours,
    and a fall too small to clear the noise floor reads differently when the
    machinery demonstrably ran in every session of the arm. On 442 that was the
    whole distance between "we cannot say" and "we cannot say from counting".

    Silent when no hook declares the literal, or when no state looked for probes.
    A line here is a measurement; an absent one is not a zero.
    """

    owners = hooks_owning(endpoint, root)
    if not owners:
        return ()
    lines: list[str] = []
    for hook_id in owners:
        ran = looked = 0
        silent: list[str] = []
        for state in (baseline, *arms):
            counts = state.executions(hook_id)
            if counts is None:
                continue
            here = sum(1 for count in counts if count)
            ran += here
            looked += len(counts)
            if not here:
                silent.append(state.label)
        if not looked:
            continue
        # One line per hook, not one per state. Six identical sentences saying the
        # same thing about every state is how a reader learns to skip the section,
        # and `tigon_url_block` — which runs on every checked request — would print
        # exactly that under every endpoint in the report.
        if not ran:
            lines.append(
                f"{hook_id} never ran, in any of {looked} session(s) that looked — "
                "a movement here would not be ours"
            )
        elif not silent:
            lines.append(f"{hook_id} ran in all {looked} session(s), every state")
        else:
            lines.append(
                f"{hook_id} ran in {ran} of {looked} session(s), and not at all in "
                + ", ".join(sorted(silent))
            )
    return tuple(lines)


def _classify_one(
    *,
    endpoint: str,
    baseline: State,
    arms: Sequence[State],
    states: Sequence[State],
    floor: int | None,
    baseline_clean: bool,
    unreadable: Sequence[str],
    declared: tuple[str, ...] | None,
    root: Path | str = ".",
) -> Classification:
    """One path's verdict. Every branch names what it rests on."""

    observed = {state.label: state.counts(endpoint) for state in states}
    common = dict(
        endpoint=endpoint,
        noise_floor=floor,
        observed=observed,
        declared=declared,
        execution=_execution(endpoint, baseline, arms, root),
        seen_on=_where_seen(endpoint, baseline, arms),
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
                        (
                            f"{arm.arm} refused it {arm.refusals_text(endpoint)} time(s): "
                            "an erased path cannot be refused, because nothing ever "
                            "reaches the code that throws"
                        )
                        if arm.reported
                        else (
                            f"{arm.arm} could not report refusals, so nothing here "
                            "corroborates the mechanism — an erased path cannot be "
                            "refused, and this build cannot say it was not"
                        ),
                    ),
                )
            )
            continue

        # No arithmetic. The guard names the literal it refused at the moment it
        # refuses, so "did this rule fire on this path" is read rather than
        # derived. What replaced: an accounting identity over the capture's block
        # *total* plus a subset-sum check for other explanations of that total —
        # which existed only because the total came from Instagram's error events
        # and carried no path. It refused `439-3r-reverse-feed`, where
        # `/feed/timeline/` had 17 requests and 17 blocks, because `7 + 7 + 3 = 17`
        # among three unrelated paths.
        refusals = arm.refusals(endpoint)
        # `baseline.refusals` is `None` when the control could not report, and
        # `None or ()` would make "the baseline refused nothing" vacuously true —
        # a second check that reads as independent and can never be the one that
        # answers. It is spelled out, because the corpus shape that reaches here is
        # exactly the one re-walking produces: new arm sessions beside a baseline
        # nobody re-walked.
        # `baseline_clean` is "the baseline refused **nothing at all**", which is
        # the whole of the control: with every toggle off nothing should throw, so
        # a baseline that refused anything means the build's own state line and its
        # behaviour disagree and no verdict taken against it is safe. A per-path
        # `and the baseline did not refuse this one` used to sit here too and read
        # as a second, independent check — it could never be the one that fired,
        # because refusing this path would have made `refused_total` non-zero. A
        # guard that cannot fire is worse than no guard: it is a reason not to look.
        if baseline_clean and refusals is not None and all(arm_counts):
            if all(count > 0 for count in refusals):
                findings.append(
                    Finding(
                        kind=BLOCKED,
                        toggle=arm.arm or "",
                        reason=(
                            f"the guard itself recorded refusing it "
                            f"{arm.refusals_text(endpoint)} time(s) in {arm.label} and "
                            f"{baseline.refusals_text(endpoint)} in the baseline, against "
                            f"{', '.join(str(item) for item in arm_counts)} request(s). "
                            "The path is still requested, which is what a block does and "
                            "an erasure does not"
                        ),
                        corroboration=tuple(
                            [
                                f"Instagram reported {arm.blocks_text} block event(s) "
                                "across the whole state, over all paths — corroboration "
                                "only, and these go missing by feature"
                            ]
                            + (
                                [
                                    f"the count also moved by {gap}, more than the "
                                    f"{floor} two runs of one state ever differed by"
                                ]
                                if moved
                                else [
                                    "the count did not move beyond noise, which is why "
                                    "the refusal signal is needed at all"
                                ]
                            )
                        ),
                    )
                )
                continue

        if moved:
            # Two different sentences, because they are two different facts. A
            # movement no *measured* mechanism explains is a finding waiting for a
            # reader; a movement whose arm could not report refusals at all is a
            # session waiting to be walked again, and calling the second "nothing
            # accounts for it" would read as evidence about the app.
            caveats.append(
                f"{arm.label} moved it by {gap}, more than its floor of {floor}, and "
                + (
                    "nothing here accounts for that: "
                    f"{arm.arm} refused it {arm.refusals_text(endpoint)} time(s)"
                    if arm.reported
                    else "its build could not report refusals, so whether the guard "
                    f"refused it is unknown rather than no: {arm.arm}"
                )
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
    # A refusal names the literal the *rule* tested, so a watched path caught by a
    # broader literal has its refusals recorded under that other name and reads
    # zero under its own. That zero is measured, not absent, so nothing above
    # refuses it — and `unaffected` is a positive claim, the one verdict here
    # easiest to reach by accident. `/clips/discover/stream/` is the live example:
    # it *contains* `/clips/discover`, a `contains` literal under `disable_reels`.
    covered = [
        arm.arm
        for arm in arms
        if arm.arm
        and declared
        and arm.arm in declared
        and arm.reported
        and arm.refused_total
        and not any(arm.refusals(endpoint) or ())
    ]
    if covered:
        return Classification(
            verdict=UNCLASSIFIABLE,
            toggle=None,
            reason=(
                "nothing moved it and it records no refusal of its own, but "
                + ", ".join(sorted(covered))
                + " declares a rule that covers it and did refuse under another literal. "
                "A refusal names the literal the rule matched, so this path's own zero "
                "does not mean it was allowed through — and 'unaffected' would say it "
                "was"
            ),
            caveats=tuple(caveats),
            **common,
        )
    if unreadable:
        return Classification(
            verdict=UNCLASSIFIABLE,
            toggle=None,
            reason=(
                "no toggle moved its count beyond the noise floor, but 'no toggle affects "
                "it' is a claim about every toggle, and the refusal evidence cannot be "
                "read for " + ", ".join(unreadable)
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
            "reported": grouping.baseline.reported,
            "refused_total": grouping.baseline.refused_total,
            # Instagram's own count, carried so a reader can see the two side by
            # side. Nothing is derived from it.
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
                "reported": arm.reported,
                "refused_total": arm.refused_total,
                "counted": arm.counted,
                "blocks": list(arm.block_totals),
                "blocks_text": arm.blocks_text,
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
    refused = (
        "not reported" if baseline["refused_total"] is None
        else f"{baseline['refused_total']} refusal(s)"
    )
    lines.append(
        f"    {', '.join(baseline['session_ids'])}   {refused}"
        f"   spans {_spans_text(baseline['spans'])}"
    )
    lines.append("")
    lines.append("  ARMS")
    lines.append("")
    for arm in report["arms"]:
        if not arm["reported"]:
            state = "UNREADABLE (the build could not report refusals)"
        elif not arm["replicated"]:
            state = "UNREADABLE (walked once)"
        else:
            state = f"{arm['refused_total']} refusal(s) across every path"
        lines.append(
            f"    {arm['toggle']:<18} {', '.join(arm['session_ids'])}"
            f"   spans {_spans_text(arm['spans'])}"
        )
        # Ours first and Instagram's second, in that order and labelled, because
        # the two disagree by design: `/discover/topical_explore` was refused 7
        # times and reported once. A reader who sees only one number cannot tell
        # which signal a verdict rested on.
        lines.append(
            f"      {state}"
            + f"   (Instagram reported {arm['blocks_text']}"
            + (
                "   " + ", ".join(f"{k} {v}" for k, v in sorted(arm["features"].items()))
                if arm["features"]
                else ""
            )
            + ")"
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
            for note in row.get("seen_on", ()):
                # `@` for where the app asked; `*` for what our code did; `!` for
                # a movement nothing explains. Three marks because they are three
                # questions, and a reader should not have to parse prose to tell.
                lines.append(f"      @ {note}")
            for note in row.get("execution", ()):
                # A different mark from a caveat on purpose. `!` is "something
                # moved and nothing explains it"; `*` is "here is what our own
                # code did", which is the only line on this page that is not
                # about the app.
                lines.append(f"      * {note}")
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

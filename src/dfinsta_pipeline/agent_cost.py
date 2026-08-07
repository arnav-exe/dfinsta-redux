"""Stage 10, the measurement half: what each hook COST this port, so the claim can be falsified.

`pipeline_flowchart.md`, "The point of stage 10": *"the number to watch is agent
invocations per port, and it should fall with every version ported. A pipeline
whose agent count is flat is not learning."*

That number was measured nowhere. Which means the claim could not be wrong,
which in this project is the shape of a bug rather than a boast: the pipeline
could have been paying an agent for the same hook every version and the only
visible symptom would have been a port that succeeded. This module counts it, and
counts the two things that predict it moving:

  * **Supplier declines, with the stage they stopped at.** A deterministic
    supplier that starts declining is a rule rotting. Today that is invisible:
    :func:`~dfinsta_pipeline.capture_supply.run_supply_chain` falls through to the
    agent, the payload renders, the port succeeds, and the only trace is "an agent
    ran" — which is indistinguishable from "this version is genuinely new". A
    decline recorded with its ``stage`` tells those apart without reading prose.

  * **Measured selectivity, as numbers.** `resolve.search_hosts` computes
    ``classes_per_literal`` and ``co_located`` and throws them away; the capture
    supplier counts how many subtypes it tested and how many loaded the drawable
    and throws that away too. A margin narrowing from 10 candidates -> 1 hit to
    3 -> 1 is what a human needs to see *before* it reaches 1 -> 1 (the test
    excludes nothing and is passing vacuously) and then 0 (the rule is dead).

**Why this is not in `decisions.py`.** Decision memory holds what a run *learned*
— resolutions, misses, survival rates — and `manifest_update` already files the
learning half there. What a run *cost* is a different fact about a different
subject, and the three record kinds have no room for it:

  * an **escalation** is the single most important cost event and
    :class:`~dfinsta_pipeline.decisions.Resolution` cannot hold one — it requires a
    host descriptor, a smali path and an evidence chain, none of which an escalated
    hook has. Filing escalations as :class:`~dfinsta_pipeline.decisions.Miss`
    instead would be worse than losing them: a miss means the pipeline was
    confidently *wrong*, `DecisionMemory.recall` prints misses first under
    "has this hook bitten us before?", and a hook that merely needed an agent has
    not bitten anyone.
  * a **supplier attempt** has no shape there at all.
  * a **selectivity margin** exists only as prose inside a chain ``finding``, and
    the whole point of the query below is a trend over numbers.

So the ledger is a separate append-only file with the same discipline: no clock,
canonical JSON, never edits, never deduplicates, keyed by (hook_id, version), and
subject to exactly the same rules about what may never be written down.

**What may never be written here** — the same list `manifest_update` enforces,
for the same reason, because a cost ledger is read by the same people and one
leaked descriptor is one join key that returns the wrong class:

  * **no obfuscated descriptor, anywhere, in any field.** Supplier evidence is
    full of them (``"2 model subtypes load ...: LX/0Dxw;, LX/0E1a;"``), so every
    stored string is scrubbed through :func:`scrub` and then every record refuses
    one that survived. A human-written type such as
    ``Lcom/instagram/profile/actionbar/ProfileActionBar;`` is kept: it is the
    supplier's *selector*, it is a stable named type, and losing it would remove
    the reason a decline happened.
  * **no resource ids** — 103 of 11,737 drawable ids survived 430->439.
  * **no absolute paths, no smali paths.** A cost record needs neither.

Nothing here reads the clock: :func:`hook_costs` takes ``recorded_at`` from its
caller exactly as `decisions.stamped` and `manifest_update.resolution_records` do,
so a Temporal replay writes the line that is already on disk.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from .capture_supply import AGENT as AGENT_SUPPLIER
from .contracts import canonical_json
from .decisions import Compatibility, DecisionError, Key
from .manifest_update import (
    REDACTED_RESOURCE_ID,
    RESOURCE_ID,
    is_stable_named_type,
    redact,
    require_version,
    search_for,
    update_memory,
    winning_candidate,
)
from .resolve import HookResolution, HostSearch, Outcome, ResolveReport

SCHEMA_VERSION = 1

#: Committed next to `manifest/decisions.jsonl`, and for the same reason: the
#: trend is worth nothing unless it outlives the decode it was measured on.
DEFAULT_LEDGER_PATH = Path("manifest/agent_cost.jsonl")

RECORD_KIND = "hook_cost"


# ------------------------------------------------------------------- the routes

#: How a hook was paid for, worst first — the same ordering convention as
#: `resolve.Outcome`, so "the route" of a hook that took two of them is the most
#: expensive one, and :attr:`HookCost.agent_for` keeps the full detail.
ROUTE_NOT_RESOLVED = "not_resolved"
ROUTE_AGENT_PROPOSAL = "agent_proposal"
ROUTE_AGENT_SUPPLIER = "agent_supplier"
ROUTE_DETERMINISTIC_SUPPLIER = "deterministic_supplier"
ROUTE_MECHANICAL = "mechanical"
#: Not a cost at all: a re-run over a decode this pipeline already patched. Kept
#: as its own route rather than folded into ``mechanical`` because counting it as
#: mechanical would make a second run over one decode look like the pipeline had
#: just mechanised every hook it did no work on.
ROUTE_ALREADY_APPLIED = "already_applied"

ROUTES: tuple[str, ...] = (
    ROUTE_NOT_RESOLVED,
    ROUTE_AGENT_PROPOSAL,
    ROUTE_AGENT_SUPPLIER,
    ROUTE_DETERMINISTIC_SUPPLIER,
    ROUTE_MECHANICAL,
    ROUTE_ALREADY_APPLIED,
)

#: The routes that mean an agent was, or must be, run. `pipeline_flowchart.md`
#: defines escalation as the only way an agent runs, so this is not a judgement
#: call: it is the project's own definition, counted.
AGENT_ROUTES = frozenset({ROUTE_AGENT_PROPOSAL, ROUTE_AGENT_SUPPLIER})

#: What an agent was needed FOR. The question the query has to answer is "an
#: agent, for what?" — a host, a capture, or the whole patch are three different
#: costs, and only the first two are ones a manifest rule can retire.
NEED_HOST = "host"
NEED_PATCH = "patch"
NEED_CAPTURE = "capture"  # rendered as `capture:<name>`
#: The honest label for a NEEDS_AGENT this stage could not classify structurally.
#: Recorded rather than guessed, so a new escalation branch in `resolve._classify`
#: shows up as an unexplained cost instead of being silently filed as a host.
NEED_UNSPECIFIED = "unspecified"


# --------------------------------------------------------------- the scrubbers

#: Any type descriptor. Matched broadly on purpose: what is kept is decided by
#: :func:`~dfinsta_pipeline.manifest_update.is_stable_named_type`, which defaults
#: to refusing, so a descriptor shape this misses is a leak and a descriptor shape
#: it over-matches costs one word of prose.
DESCRIPTOR = re.compile(r"L[A-Za-z0-9_$/]+;")

WITHHELD_DESCRIPTOR = "<obfuscated-descriptor-withheld>"

#: An absolute path anywhere in a string, not merely at the start. `ResolveReport`
#: carries `str(decode.resolve())`, and `capture_supply`'s
#: ``STAGE_CANDIDATE_UNREADABLE`` decline quotes an OSError whose message holds the
#: full path — which is how one gets into an evidence line without anyone writing
#: it there.
ABSOLUTE_PATH = re.compile(r"(?P<lead>\A|[\s(\[<'\"])(?P<path>/[^\s'\"\]>]*/)")

WITHHELD_PATH = "<absolute-path-withheld>"


def scrub(text: str, *, paths: bool = True) -> str:
    """Every stored string passes through here first.

    Resource ids go; obfuscated descriptors go; a stable named type stays,
    because ``Lcom/instagram/profile/actionbar/ProfileActionBar;`` is the
    supplier's precondition and a decline that could not name it would say only
    that something was missing.

    *paths* is False for one caller: the keys of
    :attr:`Selectivity.detail`, which are API-path literals. ``/api/v1/clips/``
    and ``/home/arnav/work/`` are the same shape, and the literal is the strongest
    fingerprint this project has measured (93.9% survival) — so the one field that
    legitimately holds path-shaped text opts out of the path rule and keeps the
    other two.
    """
    if not isinstance(text, str):
        raise DecisionError(f"only strings are stored here, got {type(text).__name__}")
    text = redact(text)
    if paths:
        text = ABSOLUTE_PATH.sub(f"\\g<lead>{WITHHELD_PATH}", text)
    return DESCRIPTOR.sub(
        lambda match: match.group(0)
        if is_stable_named_type(match.group(0))
        else WITHHELD_DESCRIPTOR,
        text,
    )


def refuse_leak(text: str, where: str, *, paths: bool = True) -> str:
    """The second half of scrub-then-refuse. Raises rather than storing.

    :func:`scrub` is applied by the builder and this is applied by the record, so
    a future caller constructing a :class:`HookCost` by hand cannot skip the
    first. Belt and braces on purpose: `decisions.Step` refuses an absolute path
    the same way, and every one of these values would be a confident wrong answer
    rather than a missing one.
    """
    if RESOURCE_ID.search(text):
        raise DecisionError(
            f"{where}: {text!r} contains a resource id. Of 11,737 drawable names present "
            "in both 430 and 439, 103 kept their id; a recorded id is a fact with a "
            "99.1% chance of being false by the next port"
        )
    leaked = [
        found for found in DESCRIPTOR.findall(text) if not is_stable_named_type(found)
    ]
    if leaked:
        raise DecisionError(
            f"{where}: {text!r} names obfuscated descriptor(s) {leaked}. Every 430 host "
            "name still exists in 439 and names a different class, so a descriptor here "
            "is a join key that returns the wrong class. Pass it through scrub() — the "
            "stage and the counts are what this ledger is for"
        )
    if paths and ABSOLUTE_PATH.search(text):
        raise DecisionError(
            f"{where}: {text!r} contains an absolute path. It names one machine's "
            "workspace and the next run cannot open it"
        )
    return text


# -------------------------------------------------------------- the measurement


@dataclass(frozen=True)
class Selectivity:
    """How many candidates a discriminator started with, and how many survived it.

    One shape for both discriminators this pipeline has, because the warning is
    the same shape in both. ``by_literal``: the least selective literal alone
    leaves *candidates* classes and all of them together leave *hits*. A capture
    supplier: *candidates* subtypes were tested and *hits* of them loaded the
    drawable.

    The number to watch is *candidates* falling toward *hits*. At
    ``candidates == hits`` the test excluded nothing and is passing vacuously — it
    will keep passing until the day it returns nothing at all — and at
    ``hits == 0`` the rule is already dead. Both are visible here a version before
    they cost anything.
    """

    subject: str
    measure: str
    candidates: int
    hits: int
    detail: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        refuse_leak(self.subject, "selectivity subject")
        refuse_leak(self.measure, "selectivity measure")
        if not self.subject.strip():
            raise DecisionError("a selectivity measurement must name what it measured")
        for name, value in (("candidates", self.candidates), ("hits", self.hits)):
            if type(value) is not int or value < 0:
                raise DecisionError(
                    f"selectivity {self.subject}: {name} must be a count >= 0, got {value!r}"
                )
        if self.hits > self.candidates:
            raise DecisionError(
                f"selectivity {self.subject}: {self.hits} hits out of {self.candidates} "
                "candidates is impossible. A margin that cannot be true would be read as "
                "a widening one and hide the narrowing it actually is"
            )
        for key, value in self.detail.items():
            # API-path literals live here and `/api/v1/clips/` is the same shape
            # as `/home/arnav/work/`. The literal is the strongest fingerprint
            # measured (93.9%), so this is the field that opts out of the path
            # rule; the descriptor and resource-id rules still apply.
            refuse_leak(str(key), "selectivity detail key", paths=False)
            if type(value) is not int:
                raise DecisionError(
                    f"selectivity {self.subject}: detail {key!r} must be an int count"
                )

    @property
    def margin(self) -> int:
        """How many candidates this discriminator actually excluded."""
        return self.candidates - self.hits

    @property
    def failed(self) -> bool:
        return self.hits == 0

    @property
    def ambiguous(self) -> bool:
        return self.hits > 1

    @property
    def vacuous(self) -> bool:
        """Nothing was excluded: the discriminator is untested by this version."""
        return not self.failed and self.margin == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "measure": self.measure,
            "candidates": self.candidates,
            "hits": self.hits,
            "margin": self.margin,
            "detail": dict(self.detail),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Selectivity:
        return cls(
            subject=data["subject"],
            measure=data["measure"],
            candidates=data["candidates"],
            hits=data["hits"],
            detail=dict(data.get("detail", {})),
        )


@dataclass(frozen=True)
class SupplierAttempt:
    """One supplier that ran, and whether it answered or declined — and at which stage.

    The early-warning record. `capture_supply` is explicit that **a decline is a
    returned value with a machine-readable stage and a failure is an exception**,
    so a failure never reaches this ledger at all: it propagates out of the run.
    Everything recorded here is therefore a finding about the target, and
    ``stage`` is required for every one of them, so "the rule stopped applying"
    can be told from "the rule was not tried".
    """

    supplier: str
    captures: tuple[str, ...]
    answered: bool
    stage: str = ""
    reason: str = ""
    evidence: tuple[str, ...] = ()
    measured: tuple[Selectivity, ...] = ()

    def __post_init__(self) -> None:
        if not self.supplier.strip():
            raise DecisionError("a supplier attempt must name its supplier")
        refuse_leak(self.supplier, "supplier name")
        if self.answered and self.stage:
            raise DecisionError(
                f"{self.supplier}: answered and yet carries decline stage {self.stage!r}. "
                "A stage is the machine-readable half of a decline; on a winner it would "
                "make the query count an answer as a rotting rule"
            )
        if not self.answered and not self.stage.strip():
            raise DecisionError(
                f"{self.supplier}: a decline must carry the stage it stopped at. Without "
                "it the ledger records only that an agent ran, which is exactly the "
                "signal this module exists to disambiguate"
            )
        refuse_leak(self.stage, f"{self.supplier} stage")
        refuse_leak(self.reason, f"{self.supplier} reason")
        for line in self.evidence:
            refuse_leak(line, f"{self.supplier} evidence")
        for name in self.captures:
            refuse_leak(name, f"{self.supplier} capture name")

    @property
    def deterministic(self) -> bool:
        """Everything that is not the agent seam. Derived, never stored.

        A stored flag could disagree with the supplier name, and the whole query
        turns on which side of that line an answer came from.
        """
        return self.supplier != AGENT_SUPPLIER

    def to_dict(self) -> dict[str, Any]:
        return {
            "supplier": self.supplier,
            "captures": list(self.captures),
            "answered": self.answered,
            "deterministic": self.deterministic,
            "stage": self.stage,
            "reason": self.reason,
            "evidence": list(self.evidence),
            "measured": [item.to_dict() for item in self.measured],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SupplierAttempt:
        return cls(
            supplier=data["supplier"],
            captures=tuple(data.get("captures", ())),
            answered=bool(data["answered"]),
            stage=data.get("stage", ""),
            reason=data.get("reason", ""),
            evidence=tuple(data.get("evidence", ())),
            measured=tuple(Selectivity.from_dict(item) for item in data.get("measured", ())),
        )


@dataclass(frozen=True)
class HookCost:
    """What one hook cost in one version: the route, what an agent was needed for, the numbers.

    Keyed by (hook_id, version) like everything else in this project, and carrying
    no descriptor and no path — a cost is a fact about the *port*, not about a
    class, and there is nothing here for a later run to mistake for an answer.
    """

    hook_id: str
    version: str
    route: str
    outcome: str
    agent_for: tuple[str, ...] = ()
    attempts: tuple[SupplierAttempt, ...] = ()
    selectivity: tuple[Selectivity, ...] = ()
    note: str = ""
    recorded_at: str = ""

    def __post_init__(self) -> None:
        if not self.hook_id.strip():
            raise DecisionError("a cost record must name its hook")
        # Deliberately the same check :func:`hook_costs` already made. Removing
        # either one alone changes nothing — mutation-tested, and that is the
        # point: the builder fails fast before constructing anything, and the
        # record refuses a hand-built row that never went through the builder.
        require_version(self.version)
        if self.route not in ROUTES:
            raise DecisionError(
                f"{self.hook_id}@{self.version}: route {self.route!r} is not one of "
                f"{list(ROUTES)}. A free-text route would let a misspelling drop out of "
                "every count silently, which is how a flat agent number looks like a "
                "falling one"
            )
        if self.outcome not in {item.value for item in Outcome}:
            raise DecisionError(
                f"{self.hook_id}@{self.version}: outcome {self.outcome!r} is not a "
                f"resolve.Outcome"
            )
        if self.route in AGENT_ROUTES and not self.agent_for:
            raise DecisionError(
                f"{self.hook_id}@{self.version}: route {self.route!r} means an agent was "
                "run and yet nothing says what for. 'An agent ran' is the symptom this "
                "ledger exists to break down into a host, a capture, or a whole patch"
            )
        if self.route == ROUTE_MECHANICAL and self.agent_for:
            raise DecisionError(
                f"{self.hook_id}@{self.version}: a mechanical hook that needed an agent "
                f"for {list(self.agent_for)} is not mechanical. Recording it as one is "
                "how the count of agent invocations per port falls without anything "
                "having been learned"
            )
        refuse_leak(self.note, f"{self.hook_id} note")
        for need in self.agent_for:
            refuse_leak(need, f"{self.hook_id} agent_for")

    @property
    def key(self) -> Key:
        return Key(self.hook_id, self.version)

    @property
    def needed_agent(self) -> bool:
        """Did this hook cost an agent invocation, run or pending?"""
        return bool(self.agent_for)

    @property
    def declines(self) -> tuple[SupplierAttempt, ...]:
        return tuple(item for item in self.attempts if not item.answered)

    @property
    def deterministic_declines(self) -> tuple[SupplierAttempt, ...]:
        """A deterministic rule that stopped applying. The rot signal."""
        return tuple(item for item in self.declines if item.deterministic)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook_id,
            "version": self.version,
            "route": self.route,
            "outcome": self.outcome,
            "needed_agent": self.needed_agent,
            "agent_for": list(self.agent_for),
            "attempts": [item.to_dict() for item in self.attempts],
            "selectivity": [item.to_dict() for item in self.selectivity],
            "note": self.note,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> HookCost:
        return cls(
            hook_id=data["hook_id"],
            version=data["version"],
            route=data["route"],
            outcome=data["outcome"],
            agent_for=tuple(data.get("agent_for", ())),
            attempts=tuple(SupplierAttempt.from_dict(item) for item in data.get("attempts", ())),
            selectivity=tuple(Selectivity.from_dict(item) for item in data.get("selectivity", ())),
            note=data.get("note", ""),
            recorded_at=data.get("recorded_at", ""),
        )


def stamped(cost: HookCost, recorded_at: str) -> HookCost:
    """Attach a timestamp from outside, so nothing in this module reads the clock."""
    if not isinstance(cost, HookCost):
        raise DecisionError(f"stamped() takes a HookCost, got {type(cost).__name__}")
    return replace(cost, recorded_at=recorded_at)


# ------------------------------------------------------------ reading a report

#: SEAM. The capture supplier's counts exist only as prose:
#: `capture_supply.profile_action_bar_self_guard` appends
#: ``f"{len(hits)} of {len(subtypes)} subtypes load {drawable} ({id}): ..."`` to a
#: `Supplied.evidence` tuple of plain strings. What that class actually wants is a
#: typed ``measured: Mapping[str, int]`` alongside ``evidence``, filled at the
#: point of measurement — the same way `HostSearch.evidence` already carries
#: ``classes_per_literal`` as a dict rather than a sentence. Until that field
#: exists this pattern reads the shape back out, anchored at the start of the line
#: so it cannot match mid-sentence, and **records nothing when it does not match**
#: rather than guessing a number.
EVIDENCE_COUNT = re.compile(r"\A(?P<hits>\d+) of (?P<candidates>\d+) (?P<measure>[^:]+)")


def measured_from(attempt_supplier: str, evidence: Sequence[str]) -> tuple[Selectivity, ...]:
    """Structural counts recovered from a supplier's evidence lines. Fails closed."""
    out: list[Selectivity] = []
    for line in evidence:
        match = EVIDENCE_COUNT.match(line)
        if not match:
            continue
        out.append(
            Selectivity(
                subject=f"supplier:{attempt_supplier}",
                measure=scrub(match.group("measure").strip()),
                candidates=int(match.group("candidates")),
                hits=int(match.group("hits")),
            )
        )
    return tuple(out)


def _literal_selectivity(search: HostSearch, ordinal: int) -> Selectivity | None:
    """The `by_literal` evidence a run computes and `HookResolution.to_dict` keeps as a blob.

    ``candidates`` is the widest single literal: the number of classes that would
    remain if only the least selective literal were used. ``hits`` is
    ``co_located``. That pair is the argument that the *intersection* picked the
    host — and it is the pair that goes vacuous, one version before the
    fingerprint stops discriminating at all.
    """
    per_literal = search.evidence.get("classes_per_literal") or {}
    counts = [value for value in per_literal.values() if type(value) is int]
    co_located = search.evidence.get("co_located")
    if not counts or type(co_located) is not int:
        return None
    subject = "by_literal" if ordinal == 0 else f"by_literal#{ordinal + 1}"
    return Selectivity(
        subject=subject,
        measure=(
            "classes containing the least selective literal alone -> classes "
            "containing all of them"
        ),
        candidates=max(counts),
        hits=co_located,
        detail={
            scrub(str(key), paths=False): value
            for key, value in per_literal.items()
            if type(value) is int
        },
    )


def _needs(item: HookResolution) -> tuple[tuple[str, ...], str]:
    """What an escalated hook needs an agent FOR, decided structurally.

    Never by reading `HookResolution.reason`: `resolve._classify` writes three
    different NEEDS_AGENT prose strings and a fourth would be filed as whichever
    one it happened to resemble. Each branch below keys on a field instead, and
    the unmatched case is recorded as :data:`NEED_UNSPECIFIED` with a note rather
    than defaulted to a host.
    """
    unfilled = tuple(
        name for supply in item.supplies if not supply.ok for name in supply.missing
    )
    if unfilled:
        return tuple(f"{NEED_CAPTURE}:{name}" for name in unfilled), ""
    if any(
        search.kind == "by_agent" and not search.candidates for search in item.searches
    ):
        return (NEED_HOST,), ""
    if item.descriptor:
        # `requires_proposal`: the anchor matched, and the manifest payload is a
        # shape rather than a patch. The whole operation has to come from outside.
        return (NEED_PATCH,), ""
    return (NEED_UNSPECIFIED,), (
        "this hook escalated to an agent and no structural branch of this stage could "
        "say what for. A new NEEDS_AGENT branch in resolve._classify needs a branch here"
    )


def _cost_for(item: HookResolution, version: str, recorded_at: str) -> HookCost:
    descriptor = item.descriptor or (
        item.resolution.descriptor if item.resolution is not None else ""
    ) or ""
    found_by = winning_candidate(item, descriptor) if descriptor else ""

    attempts: list[SupplierAttempt] = []
    supplier_selectivity: list[Selectivity] = []
    for supply in item.supplies:
        captures = tuple(scrub(name) for name in supply.supply.names)
        for attempt in supply.attempts:
            evidence = tuple(scrub(line) for line in attempt.evidence)
            measured = measured_from(attempt.supplier, evidence)
            attempts.append(
                SupplierAttempt(
                    supplier=scrub(attempt.supplier),
                    captures=captures,
                    answered=attempt.ok,
                    stage=scrub(attempt.stage),
                    reason=scrub(attempt.declined),
                    evidence=evidence,
                    measured=measured,
                )
            )
            supplier_selectivity.extend(measured)

    selectivity: list[Selectivity] = []
    ordinal = 0
    for search in item.searches:
        if search.kind != "by_literal":
            continue
        measurement = _literal_selectivity(search, ordinal)
        ordinal += 1
        if measurement is not None:
            selectivity.append(measurement)
    selectivity.extend(supplier_selectivity)

    winners = [supply.supplier for supply in item.supplies if supply.ok]
    agent_for: list[str] = []
    note = ""

    if item.outcome is Outcome.ALREADY_APPLIED:
        route = ROUTE_ALREADY_APPLIED
        note = (
            "a re-run over a decode this pipeline already patched: this port paid nothing "
            "for this hook and learned nothing about what it would cost"
        )
    elif item.outcome is not Outcome.RESOLVED:
        route = ROUTE_NOT_RESOLVED
        if item.outcome is Outcome.NEEDS_AGENT:
            needs, note = _needs(item)
            agent_for.extend(needs)
        else:
            note = (
                f"escalated as {item.outcome.value} — this stage stopped rather than "
                "asking an agent, so it is a blocked port rather than an agent invocation"
            )
    else:
        if found_by == "by_agent":
            agent_for.append(NEED_HOST)
        agent_captures = tuple(
            f"{NEED_CAPTURE}:{name}"
            for supply in item.supplies
            if supply.supplier == AGENT_SUPPLIER
            for name in supply.supply.names
        )
        agent_for.extend(agent_captures)
        if found_by == "by_agent":
            route = ROUTE_AGENT_PROPOSAL
        elif agent_captures:
            route = ROUTE_AGENT_SUPPLIER
        elif winners:
            route = ROUTE_DETERMINISTIC_SUPPLIER
        else:
            route = ROUTE_MECHANICAL

    return stamped(
        HookCost(
            hook_id=item.hook_id,
            version=version,
            route=route,
            outcome=item.outcome.value,
            agent_for=tuple(agent_for),
            attempts=tuple(attempts),
            selectivity=tuple(selectivity),
            note=note,
        ),
        recorded_at,
    )


def hook_costs(report: ResolveReport, version: str, recorded_at: str) -> tuple[HookCost, ...]:
    """One cost record per hook in the report. Pure: no clock, no filesystem, no environment.

    Every hook produces one, including the escalations — which is the whole
    difference from :func:`~dfinsta_pipeline.manifest_update.resolution_records`,
    where an escalation deliberately produces nothing. The two stages record
    opposite halves of the same run: what was *learned* may only be written down
    when something was resolved, and what was *spent* is spent whether or not it
    was.
    """
    if not isinstance(report, ResolveReport):
        raise DecisionError(
            f"hook_costs() takes a ResolveReport, got {type(report).__name__}. A dict of "
            "the report would let a hand-edited file be recorded as a measurement"
        )
    version = require_version(version)
    return tuple(_cost_for(item, version, recorded_at) for item in report.resolutions)


# ------------------------------------------------------------------ the ledger


#: What a run stamped with nothing is called in prose. A hand-built record carries
#: ``recorded_at=""``; those records are one run rather than none, and saying so is
#: better than printing an empty field where a timestamp belongs.
UNSTAMPED = "(unstamped)"


@dataclass(frozen=True)
class CostRun:
    """One port ATTEMPT: every record a single :func:`record_run` call appended.

    The unit the central claim is actually about. "Agent invocations per port"
    counts one attempt at one version; two attempts at 439 — a run that stopped at
    resolve and the re-run that finished — are not two ports, and adding them
    together inflates the very number the project judges itself by, in proportion
    to how many times someone retried.

    **No schema change was needed to recover this.** :func:`record_run` takes a
    single caller-supplied *recorded_at* and stamps every record it writes with it,
    so the run identifier was already in the data: it is
    ``(version, recorded_at)``. Version is part of the key because the stamp is a
    caller's value rather than a clock reading, and two versions ported under one
    stamp are still two ports.
    """

    version: str
    recorded_at: str
    #: 1-based, in the order this ledger saw the run — see :meth:`CostLedger.runs_for`.
    ordinal: int
    #: How many runs the ledger holds for this version, so a run can always say
    #: what it is one of.
    of: int
    costs: tuple[HookCost, ...] = ()

    @property
    def agent_invocations(self) -> int:
        return sum(1 for cost in self.costs if cost.needed_agent)

    @property
    def stamp(self) -> str:
        return self.recorded_at or UNSTAMPED

    def summary(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "of": self.of,
            "recorded_at": self.recorded_at,
            "hooks": len(self.costs),
            "agent_invocations": self.agent_invocations,
        }


class CostLedger:
    """Append-only JSONL of what each port cost. Same shape and spirit as `DecisionMemory`.

    Never edits, never deduplicates. Two runs over one decode append two sets of
    records; collapsing them on write would be deciding which run was the real one,
    and both attempts genuinely cost what they cost.

    The *reading* is where a run has to be one run: :meth:`runs_for` splits a
    version's records back into the attempts that wrote them, and
    :func:`cost_report` reports one of those rather than a version's whole history.
    """

    def __init__(self, path: Path | str | None = None):
        self._path = Path(path) if path is not None else None
        self._costs: list[HookCost] = []

    # ------------------------------------------------------------------ write

    def record(self, cost: HookCost) -> HookCost:
        if not isinstance(cost, HookCost):
            raise DecisionError(
                f"this ledger holds HookCost records only, got {type(cost).__name__}"
            )
        self._costs.append(cost)
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            envelope = {
                "schema_version": SCHEMA_VERSION,
                "kind": RECORD_KIND,
                "record": cost.to_dict(),
            }
            with open(self._path, "a", encoding="utf-8") as handle:
                handle.write(canonical_json(envelope))
                handle.write("\n")
        return cost

    # ------------------------------------------------------------------- read

    @property
    def costs(self) -> tuple[HookCost, ...]:
        return tuple(self._costs)

    @property
    def versions(self) -> tuple[str, ...]:
        """Versions in the order the ledger first saw them — append order, not sorted.

        Sorting would put "1000" before "439", and the question the query asks is
        "what did the previous port cost", which is a fact about sequence.
        """
        seen: list[str] = []
        for cost in self._costs:
            if cost.version not in seen:
                seen.append(cost.version)
        return tuple(seen)

    def costs_for(self, version: str) -> tuple[HookCost, ...]:
        """EVERY record for this version, across every attempt at it.

        Deliberately still available and deliberately not what the query reports:
        it is the honest answer to "what is on file for 439", and it was the wrong
        answer to "what did the 439 port cost" — a version re-run once returns each
        hook twice. Use :meth:`runs_for` for anything that counts.
        """
        return tuple(cost for cost in self._costs if cost.version == version)

    def runs_for(self, version: str) -> tuple[CostRun, ...]:
        """This version's records split into the runs that wrote them, oldest first.

        Ordered by first appearance and never sorted by the timestamp: this file is
        append-only, so its order *is* the order the runs were recorded, whereas
        sorting the strings would compare a ``+05:30`` offset against a ``Z`` one
        and hand the word "latest" to the wrong run.
        """
        grouped: dict[str, list[HookCost]] = {}
        for cost in self._costs:
            if cost.version == version:
                grouped.setdefault(cost.recorded_at, []).append(cost)
        total = len(grouped)
        return tuple(
            CostRun(version, recorded_at, ordinal, total, tuple(costs))
            for ordinal, (recorded_at, costs) in enumerate(grouped.items(), start=1)
        )

    def latest_run(self, version: str) -> CostRun | None:
        """The most recent attempt at this version, whether or not it was the best one."""
        runs = self.runs_for(version)
        return runs[-1] if runs else None

    def select_run(self, version: str, selector: int | str | None = None) -> CostRun | None:
        """One run: the latest by default, or the ordinal or exact stamp asked for.

        Refuses a selector that matches nothing rather than falling back to the
        latest. A silent fallback is how a report ends up labelled as the run
        somebody asked for and computed from a different one.
        """
        runs = self.runs_for(version)
        if not runs:
            return None
        if selector is None:
            return runs[-1]
        if isinstance(selector, bool):
            raise DecisionError(f"run selector {selector!r} is not an ordinal or a timestamp")
        if isinstance(selector, str) and selector.isdigit():
            # The CLI hands over strings; an all-digit one is an ordinal, since no
            # `recorded_at` this ledger writes is bare digits.
            selector = int(selector)
        if isinstance(selector, int):
            if 1 <= selector <= len(runs):
                return runs[selector - 1]
            raise DecisionError(
                f"{version}: run {selector} does not exist; this ledger holds "
                f"{len(runs)} run(s) for it, numbered 1..{len(runs)}"
            )
        for run in runs:
            if run.recorded_at == selector:
                return run
        raise DecisionError(
            f"{version}: no run recorded at {selector!r}. Recorded: "
            f"{[run.stamp for run in runs]}"
        )

    def previous_version(self, version: str) -> str | None:
        versions = self.versions
        if version not in versions:
            return None
        position = versions.index(version)
        return versions[position - 1] if position else None

    @classmethod
    def load(cls, path: Path | str) -> CostLedger:
        ledger = cls(path)
        path = Path(path)
        if not path.exists():
            return ledger
        with open(path, "r", encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    if data.get("schema_version") != SCHEMA_VERSION:
                        raise ValueError(f"unsupported schema {data.get('schema_version')!r}")
                    if data.get("kind") != RECORD_KIND:
                        raise ValueError(f"unexpected record kind {data.get('kind')!r}")
                    ledger._costs.append(HookCost.from_dict(data["record"]))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                    raise DecisionError(
                        f"{path}:{number}: unreadable cost record: {error}"
                    ) from error
        return ledger


def open_ledger(path: Path | str = DEFAULT_LEDGER_PATH) -> CostLedger:
    """The ledger at *path*. Unlike decision memory there is no seed: the number
    this file holds has never been measured before, and inventing a starting point
    would make the first real port look like an improvement or a regression
    against a figure nobody took."""
    return CostLedger.load(Path(path))


def update_ledger(
    report: ResolveReport,
    version: str,
    recorded_at: str,
    *,
    path: Path | str = DEFAULT_LEDGER_PATH,
) -> tuple[HookCost, ...]:
    """Append this report's costs to the ledger on disk. Returns what was written."""
    costs = hook_costs(report, version, recorded_at)
    ledger = open_ledger(path)
    for cost in costs:
        ledger.record(cost)
    return costs


def record_run(
    report: ResolveReport,
    version: str,
    recorded_at: str,
    *,
    memory_path: Path | str | None = None,
    ledger_path: Path | str = DEFAULT_LEDGER_PATH,
    compatibility: Compatibility | None = None,
) -> dict[str, Any]:
    """Stage 10 in one call: the learning half to decision memory, the cost half here.

    Separate files, one call site. Splitting the write would let a port record
    what it learned and forget what it paid, which is the state the pipeline has
    been in until now.
    """
    if memory_path is None:
        written = update_memory(report, version, recorded_at, compatibility=compatibility)
    else:
        written = update_memory(
            report, version, recorded_at, path=memory_path, compatibility=compatibility
        )
    costs = update_ledger(report, version, recorded_at, path=ledger_path)
    return {"resolutions": written, "costs": costs}


# ------------------------------------------------------------------- the query


def _breakdown(costs: Sequence[HookCost]) -> dict[str, Any]:
    routes: dict[str, int] = {route: 0 for route in ROUTES}
    needs: dict[str, int] = {NEED_HOST: 0, NEED_CAPTURE: 0, NEED_PATCH: 0, NEED_UNSPECIFIED: 0}
    for cost in costs:
        routes[cost.route] += 1
        for need in cost.agent_for:
            needs[need.split(":", 1)[0]] += 1
    return {
        "hooks": len(costs),
        "agent_invocations": sum(1 for cost in costs if cost.needed_agent),
        "routes": routes,
        "by_need": needs,
        "blocked": sum(
            1
            for cost in costs
            if cost.route == ROUTE_NOT_RESOLVED and not cost.needed_agent
        ),
    }


def _rot(
    costs: Sequence[HookCost], earlier: Sequence[HookCost]
) -> list[dict[str, Any]]:
    """Deterministic suppliers that answered before and do not answer now.

    The visible symptom of this is "an agent ran", which is indistinguishable from
    "this version is genuinely new" — so it is computed here from the stage rather
    than left to be noticed.
    """
    answered_before = {
        (attempt.supplier, cost.hook_id)
        for cost in earlier
        for attempt in cost.attempts
        if attempt.answered and attempt.deterministic
    }
    tried_now = {
        (attempt.supplier, cost.hook_id) for cost in costs for attempt in cost.attempts
    }
    out: list[dict[str, Any]] = []
    for cost in costs:
        for attempt in cost.deterministic_declines:
            entry = {
                "hook_id": cost.hook_id,
                "supplier": attempt.supplier,
                "captures": list(attempt.captures),
                "stage": attempt.stage,
                "reason": attempt.reason,
                "fell_through_to": (
                    "an agent" if cost.route == ROUTE_AGENT_SUPPLIER else "nothing; the hook escalated"
                ),
                "answered_previously": (attempt.supplier, cost.hook_id) in answered_before,
            }
            out.append(entry)
    for supplier, hook_id in sorted(answered_before - tried_now):
        out.append(
            {
                "hook_id": hook_id,
                "supplier": supplier,
                "captures": [],
                "stage": "not_tried",
                "reason": (
                    "this supplier answered for this hook in the previous version and did "
                    "not run at all in this one. Either the manifest stopped asking it or "
                    "the hook stopped reaching the supply chain; neither is visible as a "
                    "decline"
                ),
                "fell_through_to": "not applicable",
                "answered_previously": True,
            }
        )
    return out


def _margins(
    costs: Sequence[HookCost], earlier: Sequence[HookCost]
) -> list[dict[str, Any]]:
    before = {
        (cost.hook_id, item.subject): item
        for cost in earlier
        for item in cost.selectivity
    }
    out: list[dict[str, Any]] = []
    for cost in costs:
        for item in cost.selectivity:
            was = before.get((cost.hook_id, item.subject))
            if item.failed:
                trend = "FAILED"
            elif item.vacuous:
                trend = "VACUOUS"
            elif item.ambiguous:
                trend = "AMBIGUOUS"
            elif was is None:
                trend = "first measurement"
            elif item.margin < was.margin:
                trend = "NARROWING"
            elif item.margin > was.margin:
                trend = "widening"
            else:
                trend = "stable"
            out.append(
                {
                    "hook_id": cost.hook_id,
                    "subject": item.subject,
                    "measure": item.measure,
                    "candidates": item.candidates,
                    "hits": item.hits,
                    "margin": item.margin,
                    "previous": was.to_dict() if was is not None else None,
                    "trend": trend,
                    "detail": dict(item.detail),
                }
            )
    return out


#: The verdicts the central claim can take. Spelled out so the query cannot
#: report "improving" for a number that did not move.
VERDICT_UNTESTABLE = "untestable"
VERDICT_FALLING = "falling"
VERDICT_FLAT = "flat"
VERDICT_RISING = "rising"

def _genuinely_at_floor(now: Mapping[str, Any], was: Mapping[str, Any]) -> bool:
    """Did this port reach zero agent invocations by RESOLVING, or by not trying?

    Three ways to spend nothing, and only one of them is the claim holding.

    **Fewer hooks.** Zero invocations across a manifest that shrank is a smaller
    problem being solved, not the same problem solved for free. Without this,
    coverage falls out of the metric silently -- the shape of a growing test count
    hiding a module with no tests.

    **Nothing resolved.** A wholly blocked port spends nothing because the stage
    *stopped rather than asking*, which `blocked` counts precisely so it cannot be
    read as cheapness. The first draft of `at_floor` congratulated exactly that,
    and the report contradicted itself four lines apart: "the port is blocked, not
    expensive" above "the claim holding rather than the pipeline stalling".

    **Nothing done.** A re-run over an already-patched decode resolves every hook
    `already_applied`, whose own note says the port "paid nothing for this hook and
    learned nothing about what it would cost". Zero there measures a no-op.

    So the floor means: no fewer hooks than last time, nothing blocked, and at
    least one hook actually resolved this port. Found by the tests written for
    `at_floor` within the hour, which is the argument for writing them.
    """

    if now["hooks"] < was["hooks"] or now["blocked"]:
        return False
    earned = ROUTE_MECHANICAL, ROUTE_DETERMINISTIC_SUPPLIER
    return any(now["routes"][route] for route in earned)


#: Flat, but at zero, which is the floor.
#:
#: Added 2026-08-07, when Instagram 441 produced the third point of the sequence:
#: 439 -> 2, 440 -> 0, 441 -> 0. The flowchart's claim is "agent invocations fall
#: with every port, and a flat count means the pipeline is not learning", and the
#: report duly said FLAT and "not learning" -- of a port that needed no agent at
#: all. **A count that has reached zero cannot fall, so `flat` there is measuring
#: the wrong thing.** At 2 -> 2 the wording was right; at 0 -> 0 it was not, and
#: the metric could not tell the two apart.
#:
#: This is deliberately NOT a fourth way of saying "good". It is refused when the
#: hook count fell, because zero invocations over three hooks is not the
#: achievement zero over seven is -- the same shape as a growing test count hiding
#: a module with no tests.
VERDICT_AT_FLOOR = "at_floor"


#: Which run got reported, and why — carried in the output rather than left to be
#: assumed, because "the latest" and "the one you asked for" are different claims
#: and a reader cannot tell them apart from the numbers.
SELECTED_LATEST = "latest"
SELECTED_EXPLICIT = "explicit"


def cost_report(
    ledger: CostLedger,
    version: str,
    previous: str | None = None,
    *,
    run: int | str | None = None,
) -> dict[str, Any]:
    """Per RUN: how many hooks needed an agent, for what, and against the previous port.

    The deliverable. `pipeline_flowchart.md` says the agent count "should fall
    with every version ported. A pipeline whose agent count is flat is not
    learning." This returns the number for one run of one version, the number for
    the latest run of the version before it, and the difference — so the sentence
    becomes a claim that can turn out to be false.

    **One run, not a version's history.** A version re-run once held every hook
    twice, every margin twice and every agent invocation twice, so retrying a
    failed port inflated the number the claim is made of. *run* selects which
    attempt by 1-based ordinal or exact ``recorded_at``; the default is the latest.

    **Latest, not best.** The default is the most recent attempt whether or not an
    earlier one was cheaper, because a query that reports a version's best run is
    a query whose number cannot rise — and a metric that cannot rise is a press
    release. The choice is not silent: the run reported, how many runs exist, and
    what each of the others cost are all in the output, and :func:`render` says
    outright when a run that is not being counted cost less.
    """
    if not isinstance(ledger, CostLedger):
        raise DecisionError(f"cost_report() takes a CostLedger, got {type(ledger).__name__}")
    require_version(version)
    runs = ledger.runs_for(version)
    reported = ledger.select_run(version, run)
    costs = reported.costs if reported is not None else ()
    if previous is None:
        previous = ledger.previous_version(version)
    # Latest-to-latest: "did the count fall between ports" compares the port that
    # stands as this version's result against the one that stands as the previous
    # version's, and an abandoned attempt at either end is not a port.
    prior_run = ledger.latest_run(previous) if previous else None
    earlier = prior_run.costs if prior_run is not None else ()

    now = _breakdown(costs)
    was = _breakdown(earlier) if earlier else None

    if was is None:
        verdict = VERDICT_UNTESTABLE
        delta = None
    else:
        delta = now["agent_invocations"] - was["agent_invocations"]
        if delta < 0:
            verdict = VERDICT_FALLING
        elif delta > 0:
            verdict = VERDICT_RISING
        elif now["agent_invocations"] == 0 and _genuinely_at_floor(now, was):
            verdict = VERDICT_AT_FLOOR
        else:
            verdict = VERDICT_FLAT

    return {
        "schema_version": SCHEMA_VERSION,
        "version": version,
        "previous_version": previous,
        "recorded": bool(costs),
        # Which attempt these numbers are, what else is on file for this version,
        # and which attempt the comparison is against. Without these three a
        # reader cannot tell one run's cost from a version's accumulated history.
        "run": (
            {
                **reported.summary(),
                "selected": SELECTED_LATEST if run is None else SELECTED_EXPLICIT,
            }
            if reported is not None
            else None
        ),
        "runs": [item.summary() for item in runs],
        "previous_run": prior_run.summary() if prior_run is not None else None,
        "now": now,
        "previous": was,
        "delta_agent_invocations": delta,
        "verdict": verdict,
        "agent_hooks": [
            {
                "hook_id": cost.hook_id,
                "route": cost.route,
                "outcome": cost.outcome,
                "needed_for": list(cost.agent_for),
                "note": cost.note,
            }
            for cost in costs
            if cost.needed_agent
        ],
        "retired": [
            hook_id
            for hook_id in sorted(
                {cost.hook_id for cost in earlier if cost.needed_agent}
                - {cost.hook_id for cost in costs if cost.needed_agent}
            )
        ],
        "newly_costly": [
            hook_id
            for hook_id in sorted(
                {cost.hook_id for cost in costs if cost.needed_agent}
                - {cost.hook_id for cost in earlier if cost.needed_agent}
            )
        ]
        if earlier
        else [],
        "rotting": _rot(costs, earlier),
        # A rule that ran and answered is not the same state as one that never
        # ran, and "no declines" is true of both. Kept apart so the healthy case
        # says which rules are holding rather than only that nothing broke.
        "holding": [
            {"hook_id": cost.hook_id, "supplier": attempt.supplier}
            for cost in costs
            for attempt in cost.attempts
            if attempt.answered and attempt.deterministic
        ],
        "selectivity": _margins(costs, earlier),
    }


def _run_lines(report: Mapping[str, Any]) -> list[str]:
    """Which attempt these numbers are, and what the other attempts cost.

    The header used to read as a fact about a version, and after a re-run it was
    a fact about no port at all. Every line here exists so the alternative to
    folding the runs together is not silently dropping them.
    """
    run = report.get("run")
    if run is None:
        return []
    runs = list(report.get("runs") or [])
    version = report["version"]
    lines = [
        f"  run {run['ordinal']} of {run['of']} for {version}, "
        f"recorded {run['recorded_at'] or UNSTAMPED}"
    ]
    if run["of"] == 1:
        lines.append("  It is the only run this ledger holds for this version.")
        return lines

    others = [item for item in runs if item["ordinal"] != run["ordinal"]]
    if run["selected"] == SELECTED_LATEST:
        lines.append(
            f"  Reporting the LATEST run whether or not it was the best one. "
            f"{len(others)} other run(s) for {version} are in this ledger and NOT ONE of "
            "their records is counted below:"
        )
    else:
        lines.append(
            f"  Reporting run {run['ordinal']} because it was asked for; the latest is run "
            f"{run['of']}. The other {len(others)} run(s) are not counted below:"
        )
    for item in others:
        lines.append(
            f"     run {item['ordinal']}   {item['recorded_at'] or UNSTAMPED}   "
            f"{item['hooks']} hook(s)   {item['agent_invocations']} agent invocation(s)"
        )
    lines.append("     (report one of those instead: --run <n>, or --run '<recorded_at>')")
    cheaper = sorted(
        (item for item in others if item["agent_invocations"] < run["agent_invocations"]),
        key=lambda item: item["agent_invocations"],
    )
    if cheaper:
        best = cheaper[0]
        lines.append(
            f"  NOTE: run {best['ordinal']} cost fewer agent invocations "
            f"({best['agent_invocations']}) than the run reported here "
            f"({run['agent_invocations']}), and the run reported here is still the one "
            "above. Reporting a version's cheapest attempt would make this number one "
            "that cannot rise, and a number that cannot rise is a press release."
        )
    return lines


def render(report: Mapping[str, Any]) -> list[str]:
    """The report as lines. Pure, so the CLI's output is testable."""
    lines: list[str] = []
    version = report["version"]
    if not report["recorded"]:
        lines.append(f"{version}: nothing recorded in this ledger.")
        lines.append(
            "  Not 'nothing cost anything' — no port has been measured for this version, "
            "and an unmeasured claim is not a satisfied one."
        )
        return lines

    now = report["now"]
    was = report["previous"]
    previous = report["previous_version"]

    lines.append(f"AGENT COST — {version}   ({now['hooks']} hook(s) resolved against this decode)")
    lines.extend(_run_lines(report))
    lines.append("")
    prior_run = report.get("previous_run")
    comparison = (
        f"   (was {was['agent_invocations']} on {previous}"
        + (
            f", its latest of {prior_run['of']} run(s)"
            if prior_run is not None and prior_run["of"] > 1
            else ""
        )
        + ")"
        if was is not None
        else ""
    )
    lines.append(f"  agent invocations: {now['agent_invocations']}{comparison}")
    for entry in report["agent_hooks"]:
        needed = ", ".join(entry["needed_for"])
        lines.append(f"     {entry['hook_id']:<40} {needed:<24} [{entry['outcome']}]")
        if entry["note"]:
            lines.append(f"        {entry['note']}")
    if not report["agent_hooks"]:
        lines.append("     (none — every hook resolved without one)")

    lines.append("")
    lines.append("  ROUTES")
    for route in ROUTES:
        count = now["routes"][route]
        before = f"   (was {was['routes'][route]})" if was is not None else ""
        if count or (was is not None and was["routes"][route]):
            lines.append(f"     {route:<24} {count}{before}")
    if now["blocked"]:
        lines.append(
            f"     ...of which {now['blocked']} escalated WITHOUT an agent question: the "
            "stage stopped rather than asking, so the port is blocked, not expensive"
        )

    lines.append("")
    if report["delta_agent_invocations"] is None:
        lines.append(
            "  VERDICT: untestable. Only one version is in this ledger, and 'agent "
            "invocations per port should fall' is a claim about a sequence."
        )
    else:
        delta = report["delta_agent_invocations"]
        verdict = report["verdict"]
        if verdict == VERDICT_FALLING:
            lines.append(
                f"  VERDICT: falling — {abs(delta)} fewer than {previous}. Retired: "
                f"{', '.join(report['retired']) or 'none named'}"
            )
        elif verdict == VERDICT_AT_FLOOR:
            lines.append(
                f"  VERDICT: at the floor — 0 agent invocations again, over "
                f"{now['hooks']} hook(s) against {was['hooks']}. The count cannot fall "
                "further, so this is the claim holding rather than the pipeline "
                "stalling."
            )
            lines.append(
                "     What would move next is SELECTIVITY, not the count: read the "
                "margins below. A fingerprint narrowing toward 1 -> 1 is how this "
                "reaches zero hooks resolved while still reporting zero agents."
            )
        elif verdict == VERDICT_FLAT:
            lines.append(
                f"  VERDICT: FLAT against {previous}. A pipeline whose agent count is flat "
                "is not learning — the same hooks cost the same agent this port."
            )
            if now["agent_invocations"] == 0:
                # The one case that reads like the floor and is not.
                lines.append(
                    f"     0 invocations, but over {now['hooks']} hook(s) against "
                    f"{was['hooks']} last time. Fewer hooks is a smaller problem, not a "
                    "cheaper solution — this is NOT the floor."
                )
        else:
            lines.append(
                f"  VERDICT: RISING — {delta} more than {previous}. Newly costly: "
                f"{', '.join(report['newly_costly']) or 'none named'}"
            )

    lines.append("")
    rotting = report["rotting"]
    if rotting:
        lines.append("  DETERMINISTIC RULES THAT DID NOT APPLY")
        for entry in rotting:
            mark = "!!" if entry["answered_previously"] else "  "
            lines.append(
                f"   {mark} {entry['supplier']} declined at '{entry['stage']}' for "
                f"{entry['hook_id']}"
            )
            lines.append(f"        {entry['reason']}")
            lines.append(f"        fell through to: {entry['fell_through_to']}")
            if entry["answered_previously"]:
                lines.append(
                    f"        it ANSWERED on {previous}. This is a rule rotting, not a new "
                    "version — and the port can still succeed while it happens."
                )
    elif report["holding"]:
        held = ", ".join(
            f"{entry['supplier']} for {entry['hook_id']}" for entry in report["holding"]
        )
        lines.append(f"  DETERMINISTIC RULES: {len(report['holding'])} held — {held}")
    else:
        lines.append(
            "  DETERMINISTIC RULES: none ran. Not 'none broke' — no capture supplier was "
            "asked anything this port."
        )

    lines.append("")
    margins = report["selectivity"]
    if margins:
        lines.append("  SELECTIVITY MARGINS   candidates -> hits")
        for entry in margins:
            before = (
                f"   ({previous}: {entry['previous']['candidates']} -> "
                f"{entry['previous']['hits']})"
                if entry["previous"]
                else ""
            )
            lines.append(
                f"     {entry['hook_id']:<40} {entry['subject']:<26} "
                f"{entry['candidates']} -> {entry['hits']}{before}   {entry['trend']}"
            )
            if entry["trend"] in {"NARROWING", "VACUOUS", "FAILED", "AMBIGUOUS"}:
                lines.append(f"        {entry['measure']}")
    else:
        lines.append("  SELECTIVITY: nothing measured")
    return lines


# ------------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    report_cmd = sub.add_parser("report", help="what one version cost, against the last one")
    report_cmd.add_argument("version", help="the version ported, e.g. 439")
    report_cmd.add_argument(
        "--previous", help="compare against this version instead of the preceding one"
    )
    report_cmd.add_argument(
        "--run",
        help=(
            "report this run of the version — a 1-based ordinal from `versions`, or an "
            "exact recorded_at. Default: the latest run, best or not"
        ),
    )
    report_cmd.add_argument(
        "--ledger",
        type=Path,
        default=DEFAULT_LEDGER_PATH,
        help=f"cost ledger JSONL (default {DEFAULT_LEDGER_PATH})",
    )
    report_cmd.add_argument("--json", action="store_true", help="print the report as JSON")

    # Extended rather than given a subcommand of its own: "which ports has this
    # ledger measured" and "how many times was each attempted" are one question,
    # and a second subcommand would be a second place that has to agree about what
    # a run is. The ordinals it prints are what `report --run` takes.
    versions_cmd = sub.add_parser(
        "versions", help="which ports this ledger has measured, and each attempt at them"
    )
    versions_cmd.add_argument(
        "--ledger", type=Path, default=DEFAULT_LEDGER_PATH, help="cost ledger JSONL"
    )

    args = parser.parse_args(argv)

    if not args.ledger.exists():
        print(
            f"note: no cost ledger at {args.ledger}. Nothing has been measured, which is "
            "not the same as nothing having been spent.",
            file=sys.stderr,
        )
    ledger = open_ledger(args.ledger)

    if args.command == "versions":
        for version in ledger.versions:
            runs = ledger.runs_for(version)
            # The version line is the LATEST run, for the same reason the report
            # is: summing the runs made a retried port look like an expensive one.
            latest = runs[-1]
            print(
                f"{version:>6}  {len(latest.costs):>3} hook(s)  "
                f"{latest.agent_invocations:>3} agent invocation(s)  "
                f"[latest of {len(runs)} run(s)]"
            )
            for run in runs:
                mark = "*" if run is latest else " "
                print(
                    f"        {mark} run {run.ordinal:<3} {run.stamp:<34} "
                    f"{len(run.costs):>3} hook(s)  "
                    f"{run.agent_invocations:>3} agent invocation(s)"
                )
        return 0 if ledger.versions else 1

    try:
        report = cost_report(ledger, args.version, args.previous, run=args.run)
    except DecisionError as error:
        # A --run that matches nothing exits rather than reporting a different run
        # under the label of the one asked for.
        print(f"error: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        for line in render(report):
            print(line)
    return 0 if report["recorded"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

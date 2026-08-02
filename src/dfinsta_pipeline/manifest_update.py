"""Stage 10, the decision-memory half: what one Resolve run learned, made durable.

Every port so far has thrown this away. `resolve.py` measures, per hook, exactly
the thing a future port needs — *which* fingerprint carried the host, and how
selective it actually was: `by_literal` records that three endpoint literals
appear in 4, 3 and 2 classes on their own and in exactly one class together, so
co-location is what picked the host and a single literal would not have. That
evidence lives on a `HostSearch` for the length of one process and is then
discarded; `HookResolution.to_dict` does not even serialise the resolution. So
the next version re-derives it, and the count of agent invocations per port stays
flat. A pipeline whose agent count is flat is not learning.

This module writes that finding down, as a `decisions.Resolution` — a technique
plus an evidence chain, never an answer.

**What may be written, and what may never be.** The whole value of decision
memory is destroyed by one careless field, because a store that returns a
confident wrong answer is worse than a store that returns nothing. Three rules,
each of which this module enforces rather than documents:

  * **An obfuscated descriptor is never a fingerprint.** `LX/05t2;` exists in
    both 430 and 439 and names a different class in each, so recording it as the
    signal that found a host would score ~100% survival and rank first in
    `decisions.precedence`. `decisions.FORBIDDEN_SIGNAL` already refuses the
    signal name; :func:`is_stable_named_type` refuses the descriptor itself
    everywhere else, and defaults to refusing. The descriptor is still stored —
    a record without it would not be a record — but only inside a
    `RecalledDescriptor`, which is version-scoped and cannot be applied.

  * **Registers, member names and paths are observations, not identity.** They
    are what moved between 430 and 439 (`set_app_context` differs by exactly one
    register, v0 -> v4), so they belong in the version-stamped chain, next to
    `smali_path`, whose own accessor is spelled `smali_path_then` for precisely
    this reason. They never reach `signals` or `technique`, which is what
    `Compatibility.evidence_fingerprint` is computed from: a fingerprint that
    moved every version would make every record permanently unreusable.

  * **A resource id is never written at all.** Of 11,737 drawable names present
    in both 430 and 439, 103 kept their id — 0.9%. Names survive; ids do not. An
    anchor capture can bind one, so binding values are redacted rather than
    trusted.

Nothing here reads the clock, the same way `decisions` and `evidence` do not:
:func:`resolution_records` is pure and takes `recorded_at` from its caller, so
two calls on one report produce byte-identical records and a Temporal replay
produces the same line that is already on disk.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from .decisions import (
    API_PATH_LITERAL,
    DEFAULT_MEMORY_PATH,
    STABLE_NAMED_TYPE,
    STRUCTURAL_SHAPE,
    Compatibility,
    DecisionError,
    DecisionMemory,
    RecalledDescriptor,
    Step,
    fingerprint_of,
    seeded_memory,
    stamped,
)
from .decisions import Resolution as ResolutionRecord
from .resolve import HookResolution, HostSearch, Outcome, ResolveReport

#: What carried a host that nothing mechanical points at. Not a fingerprint that
#: survives anything — it is the honest label for "an agent had to be run", and
#: it is the number stage 10 exists to drive down. A plain string, like the other
#: signals in `decisions`, so adding one needs no code change there.
AGENT_PROPOSAL = "agent_proposal"

#: What replaces a resource id in a recorded binding. Not the id, and not a blank
#: either: a caller reading the chain has to be able to tell "there was a value
#: here and it was withheld" from "the anchor bound nothing".
REDACTED_RESOURCE_ID = "<resource-id-withheld>"

#: An Android resource id in the application package. `0x7f` is the package byte
#: every app resource carries, so this matches `0x7f0812ab` and not the
#: MobileConfig flags (`0x81099a000034a6`) that are worth keeping.
RESOURCE_ID = re.compile(r"0[xX]7[fF][0-9a-fA-F]{6}")

#: A package segment a human wrote: lowercase, and long enough not to be `X`.
_PACKAGE_SEGMENT = re.compile(r"^[a-z][a-z0-9_]{2,}$")

#: A class segment a human wrote. Length alone is not enough — `A08` passes any
#: length test — so :func:`is_stable_named_type` also demands a lowercase letter.
_CLASS_SEGMENT = re.compile(r"^[A-Za-z][A-Za-z0-9_$]{2,}$")

#: The line every chain step cites. `Step` requires a real line >= 1 and the
#: resolver retains none: `hook_manifest.find_anchor_hits` knows where the anchor
#: matched, but `resolve_in_source` keeps only the matched TEXT in `Resolution`
#: and drops the position, so nothing downstream knows where in the file it was.
#: (A capture supplier gets the span directly from `find_anchor_hits`; it is not
#: carried through a `Resolution`.) Line 1 of a smali class is its
#: `.class` declaration — the one line whose content is known without reading the
#: file, and a real place for the next agent to open.
CLASS_DECLARATION_LINE = 1


def is_stable_named_type(descriptor: str) -> bool:
    """Does this descriptor look like a name a human wrote rather than one the obfuscator did?

    A whitelist, and it **defaults to False**, because the cost of the two errors
    is not symmetric. Refusing a genuinely stable type loses one sentence of
    technique. Accepting an obfuscated one records `LX/0DnT;` as the fingerprint
    that found a host, and the next version's run finds that name still present,
    on an unrelated class, and patches it.

    So: every package segment must be lowercase and at least three characters
    (`X` is not), and the class segment must contain a lowercase letter (`A08`
    does not).
    """
    if not isinstance(descriptor, str):
        return False
    if not descriptor.startswith("L") or not descriptor.endswith(";"):
        return False
    parts = descriptor[1:-1].split("/")
    if len(parts) < 2:
        return False
    if not all(_PACKAGE_SEGMENT.fullmatch(part) for part in parts[:-1]):
        return False
    leaf = parts[-1]
    if not _CLASS_SEGMENT.fullmatch(leaf):
        return False
    return any(character.islower() for character in leaf)


def redact(value: str) -> str:
    """Strip resource ids out of a value before it is written down.

    Applied to anchor bindings, which are the one way a hex id reaches a record:
    a `<id:any>` capture binds whatever the instruction held. 0.9% of drawable
    ids survived 430->439, so a recorded id is a fact with a nine-in-ten chance
    of being a lie by the next port, and string ids cannot be resolved at all.
    """
    return RESOURCE_ID.sub(REDACTED_RESOURCE_ID, value)


# ------------------------------------------------------------------- the parts


def _require_version(version: str) -> str:
    """The version label a record is keyed by. Not a decode path."""
    if not isinstance(version, str) or not version.strip():
        raise DecisionError(
            "version is required and must be a non-empty string; decision memory is "
            "keyed by (hook_id, version) and half a key files a record nothing can "
            "retrieve"
        )
    if "/" in version or "\\" in version:
        raise DecisionError(
            f"version {version!r} looks like a path, not a version label. "
            "`ResolveReport.decode` is an absolute path to one machine's workspace; "
            "pass the label the port is for, such as '439'."
        )
    return version


def _winning_candidate(item: HookResolution, descriptor: str) -> str:
    """How the winning host was found: ``named``, ``by_literal`` or ``by_agent``.

    Read off the candidate report rather than off the first search, because a
    hook may declare several fingerprints and the one that proposed the winner is
    the one that carried it.
    """
    for candidate in item.candidates:
        if candidate.descriptor == descriptor:
            return candidate.found_by
    return ""


def _search_for(item: HookResolution, found_by: str, descriptor: str) -> HostSearch | None:
    """The search whose evidence is about this host, not merely of the same kind."""
    same_kind = [search for search in item.searches if search.kind == found_by]
    for search in same_kind:
        if descriptor in search.candidates:
            return search
    return same_kind[0] if same_kind else None


def _literals(search: HostSearch | None) -> tuple[str, ...]:
    if search is None:
        return ()
    literals = search.evidence.get("literals", ())
    return tuple(str(literal) for literal in literals)


def _signals(found_by: str, descriptor: str) -> tuple[str, ...]:
    """Which fingerprint carried this host, in the vocabulary survival is measured in.

    `STRUCTURAL_SHAPE` is on every record because the host fingerprint only ever
    proposes candidates: the manifest's anchor pattern is what discriminated
    between them, and on the Reels hooks it matches cleanly in three classes, so
    it is genuinely half of what resolved the hook.

    A `named` fingerprint pointing at an obfuscated descriptor claims no host
    signal at all. It resolved here, and that says nothing whatever about the
    next version.
    """
    if found_by == "named" and is_stable_named_type(descriptor):
        return (STABLE_NAMED_TYPE, STRUCTURAL_SHAPE)
    if found_by == "by_literal":
        return (API_PATH_LITERAL, STRUCTURAL_SHAPE)
    if found_by == "by_agent":
        return (AGENT_PROPOSAL, STRUCTURAL_SHAPE)
    return (STRUCTURAL_SHAPE,)


def _technique(found_by: str, descriptor: str, literals: tuple[str, ...]) -> str:
    """The reusable sentence: how to find this host again, with no answer in it.

    Version-invariant on purpose. `Compatibility.evidence_fingerprint` is
    computed from this text plus the signals, so a count or a register in here
    would change the fingerprint on every port and make every stored record
    permanently unreusable — the opposite failure to a stale answer, and just as
    useless. The measured numbers go in the chain, which is version-stamped.
    """
    if found_by == "named" and is_stable_named_type(descriptor):
        return (
            f"resolve the host by the manifest's stable named type {descriptor}, then "
            "match the manifest anchor pattern inside it. Deterministic end to end: no "
            "agent was involved, and the same two steps resolve the next version."
        )
    if found_by == "named":
        return (
            "the manifest names this host by an obfuscated type. That is not a "
            "fingerprint: every 430 host name still exists in 439 and names a different "
            "class, so a name that resolves in the next version is evidence of nothing. "
            "Re-establish the host from something stable — a literal, a resource name, a "
            "non-obfuscated type — before trusting this route; only the anchor pattern "
            "carries over."
        )
    if found_by == "by_literal":
        listed = ", ".join(repr(literal) for literal in literals) or "the manifest literals"
        return (
            "resolve the host by co-located API-path literals: one class must contain all "
            f"of {listed}. Then match the manifest anchor pattern inside it. A single "
            "literal is not selective — each appears in several classes, analytics maps "
            "and prefetch allowlists included — so it is the intersection that picks the "
            "host out. Deterministic end to end: no agent was involved."
        )
    if found_by == "by_agent":
        return (
            "nothing in the manifest points at this host: an agent proposed the class and "
            "the manifest anchor pattern then matched inside it. This hook costs one "
            "agent invocation per port, which is the number stage 10 exists to drive "
            "down — generalising the proposal into an anchor pattern with captures is "
            "what would retire it."
        )
    return (
        f"the host search reported kind {found_by!r}, which this stage has no description "
        "for; only the manifest anchor pattern is known to have carried the hook. Treat "
        "the host as un-fingerprinted and re-establish it."
    )


def _search_step(
    found_by: str, descriptor: str, path: str, search: HostSearch | None
) -> Step:
    """Step one of the chain: how the candidate set was narrowed, with the numbers."""
    if found_by == "named":
        stable = is_stable_named_type(descriptor)
        return Step(
            action="look the manifest's named host up in this version's index",
            file=path,
            line=CLASS_DECLARATION_LINE,
            finding=(
                f"the index has {descriptor} in this version and it is a stable named "
                "type, so the same lookup is the right first move next version"
                if stable
                else "the index resolved the manifest's named host in this version, but "
                "the name is obfuscated and obfuscated names are recycled: it will "
                "resolve next version too, onto an unrelated class. The lookup "
                "succeeding here is not evidence that it will mean anything there."
            ),
        )
    if found_by == "by_literal":
        return Step(
            action="intersect the index over every literal the host must contain",
            file=path,
            line=CLASS_DECLARATION_LINE,
            finding=_selectivity(search),
        )
    if found_by == "by_agent":
        return Step(
            action="take the host from a proposal and confirm it exists in this index",
            file=path,
            line=CLASS_DECLARATION_LINE,
            finding=(
                "no mechanical fingerprint reaches this class, so the descriptor arrived "
                "from outside and was checked against the index before being used. That "
                "check proves the name exists in this version; it does not prove the "
                "class is the same one, because the names are recycled."
            ),
        )
    return Step(
        action="find the host with the manifest fingerprint",
        file=path,
        line=CLASS_DECLARATION_LINE,
        finding=(
            f"the host search reported kind {found_by!r}; this stage recorded no "
            "selectivity evidence for it, so the narrowing that happened here is unknown "
            "and must be re-derived rather than assumed"
        ),
    )


def _selectivity(search: HostSearch | None) -> str:
    """The measured evidence that co-location, not the literal, picked the host."""
    if search is None:
        return (
            "the winning candidate came from a by_literal fingerprint whose search was "
            "not recorded, so how selective it was is unknown"
        )
    literals = _literals(search)
    per_literal: Mapping[str, Any] = search.evidence.get("classes_per_literal", {}) or {}
    co_located = search.evidence.get("co_located")
    counted = [
        f"{literal!r} in {per_literal.get(literal, '?')}"
        for literal in literals
        if literal in per_literal
    ]
    if not counted:
        return (
            f"the host contains all of {list(literals)}; the per-literal class counts were "
            "not recorded, so the selectivity of this fingerprint is unmeasured here"
        )
    alone = ", ".join(counted)
    numbers = [value for value in per_literal.values() if isinstance(value, int)]
    widest = max(numbers) if numbers else None
    tail = (
        f" — the least selective literal alone would have left {widest} candidate(s)"
        if widest is not None
        else ""
    )
    return (
        f"class counts in this version: {alone}; all of them together in {co_located}. "
        f"Co-location is what selected the host{tail}, so a version that splits these "
        "literals empties the intersection and must escalate rather than pick one."
    )


def _anchor_step(path: str, occurrences: int, bindings: Mapping[str, str]) -> Step:
    """Step two: what the anchor matched, and what it bound in THIS version only."""
    if bindings:
        bound = ", ".join(
            f"{name}={redact(str(value))}" for name, value in sorted(bindings.items())
        )
    else:
        bound = "nothing — the anchor is entirely literal"
    return Step(
        action="match the manifest anchor pattern inside that class",
        file=path,
        line=CLASS_DECLARATION_LINE,
        finding=(
            f"the anchor matched {occurrences} time(s) and bound {bound}. Bindings are "
            "this version's registers, types and members and nothing more: 430->439 moved "
            "set_app_context's register from v0 to v4 without changing anything else, "
            "which is why they are recorded as an observation here and never as a "
            "fingerprint. Any resource id among them is withheld — 103 of 11,737 drawable "
            "ids survived that same step."
        ),
    )


# ----------------------------------------------------------------- the records


def resolution_records(
    report: ResolveReport,
    version: str,
    recorded_at: str,
    *,
    compatibility: Compatibility | None = None,
) -> tuple[ResolutionRecord, ...]:
    """The durable records this report earned. Pure: no clock, no filesystem, no environment.

    One record per hook the stage actually resolved. Nothing else produces one:

      * an escalation produces no record, because absence is never a pass and a
        `NEEDS_AGENT` hook has learned nothing worth handing on;
      * `ALREADY_APPLIED` produces no record either. It carries no resolution and
        no path — `resolve._classify` does not build one — and the run that first
        applied the patch already wrote this record. Writing a second would put
        two answers under one (hook_id, version), which is the state
        `DecisionMemory.conflicts_for` reports as memory contradicting itself.

    *compatibility* states the three identities this stage cannot know: what the
    feature is, how it is delivered, and which policy revision is in force. The
    fourth, `evidence_fingerprint`, is computed here from the technique and the
    signals, because this stage is the only thing that knows what the decision
    actually rested on — so supplying one is refused rather than overwritten.
    """
    if not isinstance(report, ResolveReport):
        raise DecisionError(
            f"resolution_records() takes a ResolveReport, got {type(report).__name__}. "
            "A dict of the report has already lost the resolution objects: "
            "`HookResolution.to_dict` never serialises them, which is the gap this "
            "module exists to close."
        )
    version = _require_version(version)
    base = Compatibility() if compatibility is None else compatibility
    if not isinstance(base, Compatibility):
        raise DecisionError(
            f"compatibility must be a Compatibility, got {type(base).__name__}; a bare "
            "mapping would let a misspelled dimension read as 'unknown' instead of failing"
        )
    if base.evidence_fingerprint.strip():
        raise DecisionError(
            "evidence_fingerprint is derived here from the technique and the signals, not "
            "supplied. A caller-stated fingerprint would let a record claim compatibility "
            "with a route this run did not take, and the reuse predicate would wave it "
            "through."
        )
    return tuple(
        record
        for record in (
            _record_for(item, version, recorded_at, base) for item in report.resolutions
        )
        if record is not None
    )


def _record_for(
    item: HookResolution,
    version: str,
    recorded_at: str,
    base: Compatibility,
) -> ResolutionRecord | None:
    if item.outcome is not Outcome.RESOLVED or item.resolution is None:
        return None
    site = item.resolution
    if not site.resolved:
        # A RESOLVED outcome carrying an unresolved resolution is not a state
        # `resolve` can produce; recording it would file the reason string as an
        # answer. Silence is the honest output for a report that contradicts itself.
        return None

    descriptor = site.descriptor or item.descriptor or ""
    if not descriptor:
        raise DecisionError(
            f"{item.hook_id}@{version}: resolved with no host descriptor. A resolution "
            "record with no host is not a record of anything."
        )
    if item.descriptor and site.descriptor and item.descriptor != site.descriptor:
        raise DecisionError(
            f"{item.hook_id}@{version}: the outcome names {item.descriptor} and its "
            f"resolution names {site.descriptor}. A record whose two halves disagree "
            "answers for whichever class the reader happens to read."
        )

    path = (site.smali_path or "").strip()
    if not path:
        raise DecisionError(
            f"{item.hook_id}@{version}: resolved with no smali path. The path is the only "
            "place the next agent can be sent to look, and `resolve._classify` sets it on "
            "every RESOLVED outcome."
        )
    if path.startswith("/"):
        raise DecisionError(
            f"{item.hook_id}@{version}: smali path {path!r} is absolute. Cite a path "
            "relative to the decode: an absolute one names one machine's workspace and "
            "the next run cannot open it."
        )

    found_by = _winning_candidate(item, descriptor)
    search = _search_for(item, found_by, descriptor)
    signals = _signals(found_by, descriptor)
    technique = _technique(found_by, descriptor, _literals(search))
    record = ResolutionRecord(
        hook_id=item.hook_id,
        version=version,
        host=RecalledDescriptor(item.hook_id, version, descriptor),
        smali_path=path,
        technique=technique,
        chain=(
            _search_step(found_by, descriptor, path, search),
            _anchor_step(path, site.occurrences, site.bindings),
        ),
        compatibility=replace(
            base, evidence_fingerprint=fingerprint_of(technique, list(signals))
        ),
        signals=signals,
    )
    return stamped(record, recorded_at)  # type: ignore[return-value]


# ------------------------------------------------------------------ the writer


def open_memory(path: Path | str = DEFAULT_MEMORY_PATH) -> DecisionMemory:
    """Decision memory at *path*, materialising the in-code seed the first time.

    The seed in `decisions.seed_records` is real — a confirmed dead settings hook
    on 430, a shipped no-op substitution, the measured survival rates — and until
    now it existed only in code, where `decisions.main` reports it with a "no
    decision memory" warning. A first write that produced a file containing only
    today's run would make the file look authoritative while silently omitting
    everything the project has already paid for, so the file starts as the seed
    and the run appends to it.
    """
    path = Path(path)
    if path.exists():
        return DecisionMemory.load(path)
    return seeded_memory(path)


def update_memory(
    report: ResolveReport,
    version: str,
    recorded_at: str,
    *,
    path: Path | str = DEFAULT_MEMORY_PATH,
    compatibility: Compatibility | None = None,
) -> tuple[ResolutionRecord, ...]:
    """Append what this report earned to decision memory on disk. Returns what was written.

    Deliberately not conditional on `report.complete`: the five hooks that
    resolve mechanically have something to hand on whether or not the two
    settings hooks escalated in the same run, and a memory that only records
    perfect runs never records the run where a hook first became mechanical.

    Deliberately not deduplicating either, mirroring `DecisionMemory.record`. Two
    runs over one decode append two identical records, which `conflicts_for`
    collapses to one answer; suppressing the second would mean deciding which of
    two records is the real one, and that decision belongs to a human looking at
    both.
    """
    records = resolution_records(report, version, recorded_at, compatibility=compatibility)
    memory = open_memory(path)
    for record in records:
        memory.record(record)
    return records

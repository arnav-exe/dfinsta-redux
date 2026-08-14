"""Stage 4a: turn changed surfaces into assessments a human can actually check.

Stage 3 says what changed. This says what is worth doing about it — and the hard
part is that "addictive" is a judgement, while this project's recurring failure
is a confident wrong answer.

A calibration experiment settled the shape before any of this was written. Seven
engagement signals were fixed from product mechanics first — autoplay, infinite
pagination, algorithmic ranking, prefetch, push, variable reward, engagement
telemetry — and only then measured against surfaces we already hold an opinion
about, with 40 random literals as a control. **Six were noise.** The composite
scored positives 1.43, negatives 0.90 and *control 1.18*: the random group landed
between the labelled ones, so summing those signals would have produced an
authoritative-looking number measuring roughly "how instrumented is this class".
Only prefetch separated, and weakly — 0.38 against a size-matched baseline of
0.17, where 64% of comparable literals carry some prefetch anyway.

So this module **computes no addictiveness score**, and the split between what is
measured and what is judged is enforced by the types rather than by convention.

What does work is the app's own bookkeeping. Instagram maintains curated arrays
of endpoints it treats as continuous content, and a class that enumerates several
of them together is declaring a group. That is checkable, per-version, and
produced by the adversary rather than by us — the same principle the evidence
ledger runs on.

The detector never names a class. It looks for any class enumerating several
endpoints that DFInsta already blocks, then reads whatever else that class lists;
the extras are the candidates. Measured on both versions it finds `LX/05jj` on
430 and `LX/03Ez` on 439 — the same group under different obfuscated names, each
carrying the same four endpoints DFInsta does not block. It degrades honestly
too: if a future version has no such class, the stage reports that it found no
grouping rather than inventing one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .contracts import ID_PATTERN, canonical_json
from .feature_gate import CANDIDATE_ID_PATTERN, MAX_CANDIDATE_ID
from .hook_index import HookIndex
from .hook_manifest import Hook


class AssessmentError(ValueError):
    """Raised when an assessment document cannot be read as one."""


class Strength(str, Enum):
    """How much weight a piece of evidence can bear.

    Recorded per item and never summed. The experiment is the reason: a composite
    of mostly-weak signals reads as authority it has not earned.
    """

    STRONG = "strong"
    MEDIUM = "medium"
    WEAK = "weak"


class Verdict(str, Enum):
    """What a human may decide about a candidate.

    ``OFFER_TOGGLE`` rather than ``BLOCK`` is the default shape for anything
    judged addictive, because the product rule is that an addictive feature gets
    a switch rather than a silent removal.
    """

    BLOCK = "block"
    OFFER_TOGGLE = "offer_toggle"
    IGNORE = "ignore"
    DEFER = "defer"


@dataclass(frozen=True)
class Evidence:
    """One MEASURED fact, independently re-derivable from the decode.

    Never a conclusion. ``detail`` carries what a reader needs to check it
    themselves, because the point of separating measurement from judgement is
    that a human can disagree with the reading without distrusting the facts.
    """

    kind: str
    strength: Strength
    summary: str
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "strength": self.strength.value,
            "summary": self.summary,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class Judgement:
    """A reading OF the evidence, by an agent or a human. Not evidence itself.

    Deliberately a separate type. If a judgement could be appended to the
    evidence list, the distinction would survive exactly as long as the next
    person's attention.
    """

    actor: str
    recommendation: Verdict
    reasoning: str
    #: What the judge could NOT establish. Stated because an admitted gap is
    #: worth more than a confident guess, and this is where a reader looks to
    #: decide how much the recommendation is worth.
    unresolved: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.actor.strip():
            raise ValueError("a judgement must name who made it")
        if not self.reasoning.strip():
            raise ValueError(
                "a judgement must give its reasoning; a bare recommendation is "
                "indistinguishable from a guess at the gate"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "recommendation": self.recommendation.value,
            "reasoning": self.reasoning,
            "unresolved": list(self.unresolved),
        }


@dataclass(frozen=True)
class Grouping:
    """A class that enumerates several endpoints the app treats alike."""

    descriptor: str
    known: tuple[str, ...]  # endpoints DFInsta already blocks
    novel: tuple[str, ...]  # everything else the same class lists

    @property
    def size(self) -> int:
        return len(self.known) + len(self.novel)

    @property
    def cohesion(self) -> float:
        """Share of the class's endpoints we already recognise.

        Surfaced because it is what separates a curated list from a string pool,
        and a human judging the inference deserves to see it rather than trust
        that a threshold was applied.
        """
        return len(self.known) / self.size if self.size else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "descriptor": self.descriptor,
            "known": list(self.known),
            "novel": list(self.novel),
            "size": self.size,
            "cohesion": round(self.cohesion, 3),
        }


@dataclass(frozen=True)
class Assessment:
    """One candidate, its measured evidence, and — separately — any judgement."""

    candidate_id: str
    literal: str
    measured: tuple[Evidence, ...]
    judgement: Judgement | None = None

    def __post_init__(self) -> None:
        """Refuse anything but `Evidence` in `measured`.

        The docstring claimed this split was enforced by the types; it was not,
        and the failure was silent rather than loud. A `Judgement` placed in
        `measured` after a STRONG item slipped past `strongest`'s short-circuit
        and was serialised into the **measured** array while the `judgement` key
        stayed null — an opinion laundered into the facts, in a document that
        still validated and whose counts still agreed.

        A property that the whole design rests on has to be refused at the
        boundary, the way `EvidenceClaim` refuses a bad producer, rather than
        surviving on everyone's attention.
        """
        for item in self.measured:
            if not isinstance(item, Evidence):
                raise TypeError(
                    f"{self.candidate_id}: measured evidence must be Evidence, got "
                    f"{type(item).__name__}. A judgement belongs in `judgement`; "
                    "putting it here would present an opinion as a measurement."
                )

    @property
    def strongest(self) -> Strength | None:
        for level in (Strength.STRONG, Strength.MEDIUM, Strength.WEAK):
            if any(item.strength is level for item in self.measured):
                return level
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "literal": self.literal,
            "strongest_evidence": self.strongest.value if self.strongest else None,
            "measured": [item.to_dict() for item in self.measured],
            "judgement": self.judgement.to_dict() if self.judgement else None,
        }


# ---------------------------------------------------------------- normalising


def spellings(literal: str) -> tuple[str, ...]:
    """The forms one endpoint is written in, for an exact index lookup.

    `normalise` exists so a manifest rule and an index literal *compare* equal.
    This exists so a rule can be *looked up*: `descriptors_with_literal` is an
    exact-match index, and the app writes `/clips/discover` where the manifest
    normalises to `clips/discover`.

    Slash variants only. Nothing here re-adds an `api/v1/` prefix, because that
    would look up a string the app may never carry and the point is to find the
    text as written.
    """

    bare = literal.strip().strip("/")
    if not bare:
        return ()
    return (bare, f"{bare}/", f"/{bare}", f"/{bare}/")


def normalise(literal: str) -> str:
    """Compare endpoints the way the app writes them, not the way we do.

    A manifest `semantic_deps` entry reads `/api/v1/clips/homecoming/` while the
    index holds `clips/homecoming/`. Without this they never match and the whole
    stage silently finds nothing — a failure that looks exactly like "no new
    features", which is the worst possible way for it to break.
    """
    value = literal.strip().lstrip("/")
    for prefix in ("api/v1/", "api/"):
        if value.startswith(prefix):
            value = value[len(prefix) :]
    return value


def looks_like_uri_rule(dep: str) -> bool:
    """Is this `semantic_deps` entry a URI-path rule at all?

    Not every dependency is one — `set_app_context` declares
    `Landroid/app/Application;->onCreate()V`, a method it must be able to anchor
    on. Treating that as a blocking rule is harmless today because nothing
    contains it, but the failure mode if a short non-path dep ever slipped
    through is that it would match most of a group and quietly empty the report,
    which is indistinguishable from "nothing new was found".
    """
    value = dep.strip()
    if not value or "->" in value or ";" in value:
        return False
    return "/" in value


def blocked_endpoints(hooks: Iterable[Hook]) -> set[str]:
    """What DFInsta already covers, read from the manifest rather than hardcoded."""
    out: set[str] = set()
    for hook in hooks:
        if hook.status != "active":
            continue
        for dep in hook.semantic_deps:
            if not looks_like_uri_rule(dep):
                continue
            normalised = normalise(dep)
            if normalised:
                out.add(normalised)
    return out


def is_blocked(literal: str, blocked: Iterable[str]) -> str | None:
    """Which rule, if any, already covers this endpoint. Returns the rule or None.

    Substring containment, not equality, because that is what the app-side code
    actually does: `throwIfBlocked` applies `endsWith` and `contains` against the
    request's URI path, so the rule `/discover/topical_explore` covers the
    literal `discover/topical_explore/` and `/clips/discover` covers
    `clips/discover/interest/stream/`.

    Equality was tried first and produced two false gaps out of six — endpoints
    reported as unprotected that are in fact blocked. At a gate that is worse
    than a miss: a human who checks one claim, finds it wrong, and discounts the
    rest has been actively misled by the report.

    The containment direction matters and is not symmetric. `feed/timeline/`
    does NOT cover `feed/timeline_stream/`, because the trailing slash is part
    of the rule — which is exactly why that one is a genuine gap.
    """
    target = normalise(literal)
    # Containment, OR equality once both sides are stripped of slashes. The
    # second is not a relaxation of the first: it merges `X` with `X/` and
    # nothing else. Measured on 440, `/clips/homecoming` normalises to
    # `clips/homecoming` and the rule is `clips/homecoming/`, so containment
    # fails on a trailing slash alone and an endpoint the app really does cover
    # was being counted as unknown.
    #
    # NOT symmetric containment, which was tried and destroys the load-bearing
    # case above: `feed/timeline/` would then cover `feed/timeline_stream/`, and
    # that is a genuine gap the whole stage exists to find. Equality cannot,
    # because `feed/timeline` != `feed/timeline_stream`.
    matches = [
        rule
        for rule in blocked
        if rule and (rule in target or rule.strip("/") == target.strip("/"))
    ]
    if not matches:
        return None
    # Longest first, then lexicographic. `blocked` arrives as a set, so taking
    # the first match made the cited rule depend on PYTHONHASHSEED: several rules
    # genuinely cover one endpoint (`clips/discover/` and `clips/discover/stream/`
    # both cover the latter) and the citation moved between runs on identical
    # input. The boolean never wavered, but the whole point of returning the rule
    # is so a report can cite it, and the gate requires two independent
    # derivations to agree byte-for-byte.
    #
    # Longest is also the more useful answer: the most specific rule is the one
    # that tells a reader why this endpoint is considered covered.
    return max(matches, key=lambda rule: (len(rule), rule))


# ------------------------------------------------------------------ groupings


#: A grouping must be mostly things we already recognise. Below this, the known
#: endpoints are a minority of what the class holds, which is the signature of a
#: generated global string pool rather than a curated list — `LX/0000` on 439
#: holds 3 seeds among 51 literals (0.06) while the real grouping holds 5 among 9
#: (0.56). `docs/PORT_430_MAPPING.md` already warns never to patch those pools;
#: this keeps them out of the report for the same reason.
#:
#: Cohesion rather than a higher seed count, because a seed threshold is a magic
#: number that happens to fit today's group and would miss a smaller one
#: tomorrow. This measures the property that actually distinguishes them.
MIN_COHESION = 0.4


def find_groupings(
    index: HookIndex,
    seeds: Iterable[str],
    min_seeds: int = 2,
    min_size: int = 2,
    min_cohesion: float = MIN_COHESION,
) -> list[Grouping]:
    """Classes that enumerate several *seed* endpoints, plus whatever else they list.

    Deliberately names no class. `LX/05jj` on 430 and `LX/03Ez` on 439 are the
    same grouping under different obfuscated names — resolving it by content is
    the only thing that survives a version bump, and it is the same reason host
    search uses co-located literals rather than a descriptor.

    ``min_seeds`` guards against a coincidence: one shared endpoint means
    nothing, and a global string pool that happens to hold everything would
    otherwise look like the strongest grouping in the app.
    """
    wanted = {normalise(seed) for seed in seeds if normalise(seed)}
    if not wanted:
        return []
    # Only classes that already hold a seed can be a group, so the search starts
    # from them rather than walking all 181,000 classes.
    #
    # Looked up in every spelling, because `normalise` strips the leading slash
    # and the index holds the app's own text. Measured on 440: the seed
    # `clips/discover` matches NO class, while `/clips/discover` matches `LX/1qi;`
    # — which also holds `delivery/background_prefetch`, the one signal that
    # survived the addictiveness calibration. A leading slash was hiding an
    # entire grouping, and it failed the way this stage fails worst: silently,
    # looking exactly like "no new features".
    candidates: set[str] = set()
    for literal in wanted:
        for spelling in spellings(literal):
            candidates.update(index.descriptors_with_literal(spelling))

    groupings: list[Grouping] = []
    for descriptor in candidates:
        literals = set(index.literals_in(descriptor))
        # Split by the SAME containment rule the gap check uses. Using set
        # membership here and containment there would let a covered endpoint sit
        # in `novel` and be reported as a gap.
        known = sorted(l for l in literals if is_blocked(l, wanted))
        if len(known) < min_seeds or len(literals) < min_size:
            continue
        novel = sorted(l for l in literals if not is_blocked(l, wanted))
        if len(known) / (len(known) + len(novel)) < min_cohesion:
            continue  # a string pool that happens to contain some seeds
        groupings.append(Grouping(descriptor, tuple(known), tuple(novel)))
    return sorted(groupings, key=lambda g: (-len(g.known), -g.size, g.descriptor))


# ----------------------------------------------------------------- assessing


def coverage_gaps(groupings: Sequence[Grouping], blocked: set[str]) -> list[tuple[str, Grouping]]:
    """Endpoints a grouping lists that DFInsta does not block.

    The strongest single output of this stage: the app itself says these belong
    with surfaces we already treat as distractions, and we let them through.
    """
    out: list[tuple[str, Grouping]] = []
    seen: set[str] = set()
    for grouping in groupings:
        for literal in grouping.novel:
            if literal in seen or is_blocked(literal, blocked):
                continue
            seen.add(literal)
            out.append((literal, grouping))
    return out


def assess_gap(
    literal: str, grouping: Grouping, extra: Sequence[Evidence] = ()
) -> Assessment:
    """Measured evidence for one unblocked endpoint inside a declared group.

    `extra` is measured evidence this module cannot compute, appended verbatim.
    Today that is what a phone did with the endpoint, which `device_evidence`
    mints because answering it means globbing `manifest/observations/` — and this
    module reads no filesystem, which is what keeps it deterministic under
    Temporal replay.

    Appended rather than merged, and typed as `Evidence`, so the boundary
    `Assessment.__post_init__` enforces still holds: a *reading of* the evidence
    is a `Judgement` and has nowhere to hide in here.
    """
    peers = ", ".join(grouping.known[:4])
    return Assessment(
        candidate_id=f"gap:{literal}",
        literal=literal,
        measured=tuple(extra) + (
            Evidence(
                "app_declared_grouping",
                Strength.STRONG,
                f"{grouping.descriptor} lists this endpoint alongside {len(grouping.known)} "
                f"that DFInsta already blocks ({peers})",
                {
                    "descriptor": grouping.descriptor,
                    "group_size": grouping.size,
                    "known_members": list(grouping.known),
                },
            ),
            Evidence(
                "coverage_gap",
                Strength.STRONG,
                f"no active hook blocks {literal!r}",
                {"literal": literal},
            ),
        ),
    )


def assess(
    index: HookIndex,
    hooks: Sequence[Hook],
    min_seeds: int = 2,
    *,
    extra_evidence: Mapping[str, Sequence[Evidence]] | None = None,
) -> tuple[list[Assessment], list[Grouping]]:
    """Every unblocked endpoint the app groups with ones we block.

    Returns the assessments and the groupings they came from, because a reader
    at the gate needs to see the grouping to judge whether the inference holds.

    `extra_evidence` is keyed by literal and supplied by the caller, following
    `suppressed` exactly: this module reads no filesystem, so anything measured
    off a phone or a store arrives already computed. A literal with no entry gets
    none, which is the shape a candidate had before device evidence existed.
    """
    blocked = blocked_endpoints(hooks)
    groupings = find_groupings(index, blocked, min_seeds=min_seeds)
    supplied = dict(extra_evidence or {})
    return [
        assess_gap(lit, g, supplied.get(lit, ()))
        for lit, g in coverage_gaps(groupings, blocked)
    ], groupings


def report(
    assessments: Sequence[Assessment],
    groupings: Sequence[Grouping],
    suppressed: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """The document that goes to CAS and is hash-pinned into the gate."""
    settled = dict(suppressed or {})
    open_candidates = [a for a in assessments if a.candidate_id not in settled]
    return {
        "schema_version": 1,
        "note": (
            "Measured evidence and judgements are separate and must stay that way. "
            "No addictiveness score is computed: a calibration experiment found six of "
            "seven a-priori engagement signals to be noise, so a composite would read "
            "as authority it has not earned."
        ),
        "groupings": [g.to_dict() for g in groupings],
        "candidates": [a.to_dict() for a in open_candidates],
        # Reported rather than dropped: a human at this gate can see what a human
        # at the last one decided, and why the list is shorter than the grouping.
        "settled": [
            {"candidate_id": a.candidate_id, **dict(settled[a.candidate_id])}
            for a in assessments
            if a.candidate_id in settled
        ],
        "counts": {
            "groupings": len(groupings),
            "candidates": len(open_candidates),
            "settled": len(assessments) - len(open_candidates),
            "judged": sum(1 for a in open_candidates if a.judgement is not None),
        },
    }


# --------------------------------------------------- the bytes the gate pins


DOCUMENT_SCHEMA_VERSION = 1


def document(
    index: HookIndex,
    hooks: Sequence[Hook],
    min_seeds: int = 2,
    suppressed: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    extra_evidence: Mapping[str, Sequence[Evidence]] | None = None,
) -> dict[str, Any]:
    """The whole of stage 4a as one call: index and hooks in, gate document out.

    Exists so that "the assessment" names exactly one thing. `assess` returns two
    values and `report` combines them, and every caller that wants the document
    has to remember to do both in the right order — which is the kind of
    invariant that survives on attention until it does not.

    ``suppressed`` maps a candidate id to the ruling that settled it. A human who
    ruled `ignore` said "we looked and decided not to block this"; nothing in the
    app records that, so without suppression the candidate returns at every gate
    and is re-decided at every gate. It is **passed in, never read from a file
    here** — this module reads no filesystem, which is what keeps it
    deterministic under Temporal replay.

    Suppressed candidates are *reported*, not deleted. A shorter list with no
    explanation would leave a human unable to see what a predecessor decided, and
    `candidate_ids` reads only `candidates`, so the gate covers what is still
    open while the document still carries the record of what is closed.

    ``extra_evidence`` is measured evidence from outside the decode, keyed by
    literal — today, what a phone did with the endpoint. Same rule as
    ``suppressed``: computed by the caller, never read here. It **must** join the
    operation key of whatever records this document, or an input that changes the
    output without changing the key makes `record` refuse with a message about
    two derivations disagreeing that names the wrong cause.
    """
    assessments, groupings = assess(
        index, hooks, min_seeds=min_seeds, extra_evidence=extra_evidence
    )
    return report(assessments, groupings, suppressed)


def canonical_bytes(value: Mapping[str, Any]) -> bytes:
    """The exact bytes that go to CAS and whose digest the gate pins.

    Measured: 3,696 / 3,844 / 3,831 bytes on Instagram 430 / 439 / 440, identical
    across `PYTHONHASHSEED` values and repeated calls, containing no absolute
    paths. That reproducibility is what lets the admitting side **recompute** the
    assessment rather than adopt a caller's bytes — the difference between "these
    bytes were handed to me" and "these bytes are what this code computes from
    this recorded input".
    """
    return canonical_json(value).encode("utf-8")


def candidate_ids(value: Any) -> tuple[str, ...]:
    """The candidate ids a gate request must pin, read from the document itself.

    **The one decoder.** Both the side that prepares the gate and the client that
    re-derives its subject must extract this list the same way; two decoders, or
    one that takes the list from a caller, and the two derivations diverge for a
    reason the human never touched. `feature_gate.validate_submission` never
    re-reads the assessment blob, so nothing downstream would catch it.

    Order is the document's own, because
    `FeatureGateRequestV1` compares the list positionally. Every rule below is a
    refusal rather than a filter: a document this cannot read is not one to guess
    about.
    """
    if not isinstance(value, Mapping):
        raise AssessmentError("assessment document must be a mapping")
    version = value.get("schema_version")
    # `1.0 == 1` and `True == 1`, so a value comparison alone accepts a JSON float
    # and a JSON boolean. `hook_index.load` guards the same way for the same
    # reason; a document whose schema tag is `true` is not a document to guess at.
    if not isinstance(version, int) or isinstance(version, bool) or version != DOCUMENT_SCHEMA_VERSION:
        raise AssessmentError(f"unsupported assessment document schema {version!r}")
    candidates = value.get("candidates")
    if not isinstance(candidates, (list, tuple)):
        raise AssessmentError("assessment document has no candidates array")
    out: list[str] = []
    for position, entry in enumerate(candidates):
        if not isinstance(entry, Mapping):
            raise AssessmentError(f"candidate {position} is not a mapping")
        identifier = entry.get("candidate_id")
        if type(identifier) is not str:
            raise AssessmentError(f"candidate {position} has no string candidate_id")
        if len(identifier) > MAX_CANDIDATE_ID or not CANDIDATE_ID_PATTERN.fullmatch(identifier):
            raise AssessmentError(f"candidate {position}: invalid candidate_id {identifier!r}")
        out.append(identifier)
    if not out:
        # A gate over nothing would present a human with an empty list and record
        # their approval of it. `FeatureGateRequestV1` refuses it too; refusing
        # here as well means the failure is named where the document is read.
        raise AssessmentError(
            "assessment document has no candidates; there is nothing to gate on"
        )
    duplicates = sorted({name for name in out if out.count(name) > 1})
    if duplicates:
        raise AssessmentError(f"duplicate candidate_id(s): {', '.join(duplicates)}")
    return tuple(out)


def policy_revision(path: Path | str) -> str:
    """The manifest's `policy_revision`, which `load_manifest` reads and discards.

    It is one of the four dimensions `decisions.py` makes a decision's
    reusability hang on, and it is a required field of the gate request — so a
    value that exists on disk and reaches nothing is a wire that looks connected.
    A separate reader rather than a changed `load_manifest` signature: every
    existing caller wants the hooks and nothing else.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        # One error type out of this module. A caller that catches
        # `AssessmentError` and still eats a bare `FileNotFoundError` has a
        # handler that looks complete and is not.
        raise AssessmentError(f"{path}: cannot read the manifest: {error}") from error
    revision = data.get("policy_revision")
    if type(revision) is not str or not ID_PATTERN.fullmatch(revision):
        raise AssessmentError(
            f"{path}: policy_revision must be an identifier, got {revision!r}"
        )
    return revision

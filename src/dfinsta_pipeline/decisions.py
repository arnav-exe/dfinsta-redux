"""Decision memory: what an earlier run learned, handed on as a technique, never as an answer.

This is the pipeline's long-term memory across Instagram versions, and its whole
purpose is that a future run replays a working **technique** rather than a stale
**answer**. Three agents recently rediscovered the same route into the profile
settings control independently — enter at `UserDetailFragment`, take the `A19()`
override on the single subclass, sit behind the own-profile gate, pin the control
by DRAWABLE NAME and only then resolve that name to this version's id. Every one
of them paid for it again. That route should have been recorded the first time
and handed to them; recording it is what this module is for.

**What it must never become.** A descriptor-keyed cross-version cache. Obfuscated
names are recycled, not merely scrambled: `LX/05t2;` names a 1990-line Reels
builder in 430 and an unrelated 596-line class in 439. A store that answered
"where did this descriptor live last time?" would return a confident wrong answer
rather than a miss, which is worse than having no memory at all. So:

  * every per-hook record is keyed by **(hook_id, version)** — see :class:`Key`;
  * nothing here joins on a descriptor, and :meth:`DecisionMemory.lookup_by_descriptor`
    exists only to refuse, by name, the query someone will eventually try;
  * a recalled descriptor never leaves the query surface as a bare string. It
    comes back inside a :class:`RecalledDescriptor`, which is not a `str`, does
    not print as one, and yields the text only to a caller that has stated in a
    :class:`Reverification` where it will re-check it. There is no method here
    that produces an applier-shaped operation.

The descriptor is of course written to disk — a record that omitted it would not
be a record. What the query surface refuses to do is hand it back in a shape you
can apply.

**The reuse rule is a predicate, not a comment.** `docs/ADK_PIPELINE_PLAN.md`:
"Decision memory must not permanently suppress reassessment. A decision is
reusable only while its semantic feature identity, delivery mechanism, evidence
fingerprint, and policy revision remain compatible." :func:`reusable` implements
exactly that over :data:`REUSE_DIMENSIONS`, and **defaults to not reusable
whenever any of the four is unknown on either side**. A memory that cannot tell
whether it is stale must not be trusted, because the failure mode of this whole
project is the confident wrong answer, not the hesitant one.

That default cuts one way for answers and the other way for warnings, and the
asymmetry is deliberate: an unknown-compatibility *resolution* is not reused, and
an unknown-compatibility *miss* is still shown. The predicate governs the reuse
of an answer; it never suppresses a warning.

**Misses are the highest-value table.** A resolution says where something was; a
miss says where the pipeline was confidently wrong, in which version, and what
finally caught it. Each seeded miss below is real, and each was found by a
different accident rather than by a standing check — which is the reason for
writing them down. `suspected` and `confirmed` are kept apart: the 439
action-bar entry is a static argument that has never been run on a phone, and
calling that `confirmed` would repeat the mistake it describes.

**Survival rates make precedence data rather than folklore.** Measured 430->439,
API-path literals survive 93.9% of the time and drawable ids 0.9% — 103 of
11,737 kept their number. :func:`precedence` orders signals by that measurement,
so a fingerprint ranking can be argued with using numbers.

Nothing in this module reads the clock: timestamps arrive from the caller via
:func:`stamped`, exactly as `evidence.stamped` does, so a Temporal replay
produces byte-identical records.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, NoReturn, Sequence

from .contracts import canonical_json, canonical_sha256

SCHEMA_VERSION = 1

#: Where the committed memory lives. Git, not the artifact store: this is the one
#: record that must outlive every decode.
DEFAULT_MEMORY_PATH = Path("manifest/decisions.jsonl")


class DecisionError(ValueError):
    """Raised when a record is malformed, mis-keyed, or asked to give up a descriptor."""


class RecordKind(str, Enum):
    """The three tables. Nothing else may be stored here.

    Notably absent: class-level version-to-version diffs. A descriptor-keyed
    cross-version record would return a confident wrong answer instead of a miss,
    so the shape simply does not exist.
    """

    RESOLUTION = "resolution"
    MISS = "miss"
    SURVIVAL = "survival"


class Detector(str, Enum):
    """What caught a miss. Recorded because it says how much the finding is worth."""

    STATIC_AUDIT = "static_audit"  # re-read the code and found it could never work
    ADVERSARIAL_VERIFIER = "adversarial_verifier"  # an agent told to falsify
    DEVICE_SESSION = "device_session"  # the phone; the only real oracle
    DIFFERENTIAL = "differential"  # this version fails where the last one passed
    HUMAN = "human"


class MissStatus(str, Enum):
    """How settled a miss is.

    ``SUSPECTED`` is not a weaker synonym for ``CONFIRMED``; it is the honest
    label for an argument that has not been run. The 439 action-bar hook is
    suspected inert on a static reachability argument that four independent
    checks agree with — and a static argument is precisely what certified the
    430 settings hook that turned out to be dead.
    """

    SUSPECTED = "suspected"
    CONFIRMED = "confirmed"


#: A fingerprint nothing may claim to have resolved by. Recording a survival rate
#: for the obfuscated descriptor would be true and ruinous: every 430 host name
#: still exists in 439, so it would score ~100% and rank first in
#: :func:`precedence`, while naming a different class in each version.
FORBIDDEN_SIGNAL = "obfuscated_descriptor"

#: Signals seen so far. Kept as plain strings rather than an enum so a new
#: measurement does not need a code change; only the forbidden one is special.
API_PATH_LITERAL = "api_path_literal"
STABLE_NAMED_TYPE = "stable_named_type"
STRUCTURAL_SHAPE = "structural_shape"
DRAWABLE_NAME = "drawable_name"
DRAWABLE_ID = "drawable_id"

#: The four things `docs/ADK_PIPELINE_PLAN.md` says a decision's reusability
#: depends on. The order is fixed so two runs report the same reasons in the same
#: sequence.
REUSE_DIMENSIONS: tuple[str, ...] = (
    "semantic_feature_identity",
    "delivery_mechanism",
    "evidence_fingerprint",
    "policy_revision",
)


def _require(value: str, label: str, context: str = "") -> str:
    if not isinstance(value, str) or not value.strip():
        prefix = f"{context}: " if context else ""
        raise DecisionError(f"{prefix}{label} is required and must be a non-empty string")
    return value


# --------------------------------------------------------------------- keying


@dataclass(frozen=True)
class Key:
    """The only key a per-hook record has: which hook, in which version.

    A separate type rather than a bare tuple so that
    :meth:`DecisionMemory.by_key` can refuse a string. The string someone will
    eventually pass is a descriptor, and answering it would be the one query this
    module exists to make impossible.
    """

    hook_id: str
    version: str

    def __post_init__(self) -> None:
        _require(self.hook_id, "hook_id")
        _require(self.version, "version")

    def __str__(self) -> str:
        return f"{self.hook_id}@{self.version}"

    def to_dict(self) -> dict[str, Any]:
        return {"hook_id": self.hook_id, "version": self.version}


# ------------------------------------------------------------- evidence chain


@dataclass(frozen=True)
class Step:
    """One step of the route that found a host: what was done, where, what it showed.

    ``file`` is relative to the decode and ``line`` is a real line in it, because
    the point of a chain is that the next agent can open the same place. An
    absolute path names one machine's workspace and is useless to the next run,
    so it is refused rather than stored.
    """

    action: str
    file: str
    line: int
    finding: str

    def __post_init__(self) -> None:
        _require(self.action, "action", "chain step")
        _require(self.file, "file", "chain step")
        _require(self.finding, "finding", "chain step")
        if type(self.line) is not int:
            raise DecisionError(
                f"chain step {self.file!r}: line must be an int, got {type(self.line).__name__}"
            )
        if self.line < 1:
            raise DecisionError(
                f"chain step {self.file!r}: line must be >= 1, got {self.line}. Zero is a "
                "harness sentinel, not a place to look, and a chain nobody can follow is "
                "not evidence."
            )
        if self.file.startswith("/"):
            raise DecisionError(
                f"chain step {self.file!r} is an absolute path. Cite a path relative to the "
                "decode: an absolute one names one machine's workspace and the next run "
                "cannot open it."
            )

    @property
    def cite(self) -> str:
        return f"{self.file}:{self.line}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "file": self.file,
            "line": self.line,
            "finding": self.finding,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Step:
        return cls(
            action=data["action"],
            file=data["file"],
            line=data["line"],
            finding=data["finding"],
        )


def fingerprint_of(*parts: Any) -> str:
    """Content hash of whatever a decision rested on.

    Fingerprint what the decision *rested on* — the signals and the technique —
    and not the line numbers it happened to cite. Line numbers move in every
    version, so hashing the chain would make every stored decision permanently
    unreusable, which is the opposite failure: a memory that never helps is as
    useless as one that lies.
    """
    return canonical_sha256(list(parts))


# ------------------------------------------------------------- the reuse rule


@dataclass(frozen=True)
class Compatibility:
    """The four identities a decision's reusability hangs on.

    Every field defaults to empty, and empty means **unknown**, not "matches
    anything". A record stored without them is still a legitimate record — it
    just cannot be reused, which is the correct outcome for a decision whose
    staleness nobody can assess.
    """

    semantic_feature_identity: str = ""
    delivery_mechanism: str = ""
    evidence_fingerprint: str = ""
    policy_revision: str = ""

    @property
    def unknown(self) -> tuple[str, ...]:
        return tuple(
            name for name in REUSE_DIMENSIONS if not getattr(self, name).strip()
        )

    @property
    def complete(self) -> bool:
        return not self.unknown

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in REUSE_DIMENSIONS}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Compatibility:
        return cls(**{name: data.get(name, "") for name in REUSE_DIMENSIONS})


@dataclass(frozen=True)
class Context:
    """What the run asking the question is: the target version and its four identities.

    Constructing one with no compatibility is legal and means "I cannot tell you
    whether anything has changed" — to which the honest answer is that nothing
    stored may be reused.
    """

    version: str
    compatibility: Compatibility = field(default_factory=Compatibility)

    def __post_init__(self) -> None:
        _require(self.version, "version", "context")
        if not isinstance(self.compatibility, Compatibility):
            raise DecisionError(
                "context compatibility must be a Compatibility; a bare mapping would let "
                "a misspelled dimension read as 'unknown' instead of failing"
            )

    def to_dict(self) -> dict[str, Any]:
        return {"version": self.version, "compatibility": self.compatibility.to_dict()}


@dataclass(frozen=True)
class Reusability:
    """Whether one stored record may be reused, and precisely what stopped it."""

    hook_id: str
    recorded_version: str
    target_version: str
    reusable: bool
    changed: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        """So ``if reusable(record, context):`` cannot silently pass.

        Without this, the result object is always truthy and the natural way to
        call the predicate would wave every stale record through — the exact
        outcome the predicate exists to prevent.
        """
        return self.reusable

    @property
    def blocking(self) -> tuple[str, ...]:
        """Every dimension that refused, changed or unknown, in declared order."""
        blocked = set(self.changed) | set(self.unknown)
        return tuple(name for name in REUSE_DIMENSIONS if name in blocked)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook_id,
            "recorded_version": self.recorded_version,
            "target_version": self.target_version,
            "reusable": self.reusable,
            "changed": list(self.changed),
            "unknown": list(self.unknown),
            "blocking": list(self.blocking),
            "reasons": list(self.reasons),
        }


def reusable(record: Resolution | Miss, current: Context) -> Reusability:
    """May this stored record be reused for *current*? Default: no.

    The rule is `docs/ADK_PIPELINE_PLAN.md` verbatim — a decision is reusable
    only while its semantic feature identity, delivery mechanism, evidence
    fingerprint and policy revision remain compatible. A dimension that is blank
    on either side is *unknown*, and unknown is never compatible.

    Reusable means "worth offering as a hint". It never means "apply this".
    """
    if not isinstance(record, (Resolution, Miss)):
        raise DecisionError(
            f"reusable() takes a stored record, got {type(record).__name__}. Survival "
            "rates are measurements, not decisions, and have nothing to reuse."
        )
    if not isinstance(current, Context):
        raise DecisionError(
            f"reusable() needs a Context, got {type(current).__name__}. Passing a bare "
            "version string would compare a version against a feature identity and call "
            "the mismatch 'changed'."
        )
    stored = record.compatibility
    wanted = current.compatibility
    changed: list[str] = []
    unknown: list[str] = []
    reasons: list[str] = []
    for name in REUSE_DIMENSIONS:
        mine = getattr(stored, name).strip()
        theirs = getattr(wanted, name).strip()
        if not mine or not theirs:
            unknown.append(name)
            missing = []
            if not mine:
                missing.append("the stored record")
            if not theirs:
                missing.append("this run")
            reasons.append(
                f"{name}: unknown — {' and '.join(missing)} did not state it. An unknown "
                "dimension is never compatible; a memory that cannot tell whether it is "
                "stale must not be trusted."
            )
        elif mine != theirs:
            changed.append(name)
            reasons.append(f"{name}: changed, {mine!r} -> {theirs!r}")
    ok = not changed and not unknown
    if ok:
        reasons.append(
            f"all four dimensions compatible; {record.hook_id}@{record.version} may be "
            "offered as a hint for "
            f"{current.version} — it still must be re-verified against that decode"
        )
    return Reusability(
        hook_id=record.hook_id,
        recorded_version=record.version,
        target_version=current.version,
        reusable=ok,
        changed=tuple(changed),
        unknown=tuple(unknown),
        reasons=tuple(reasons),
    )


# ------------------------------------------------------- the withheld descriptor


@dataclass(frozen=True)
class Reverification:
    """The caller's statement of where it will re-check a recalled descriptor.

    A typed acknowledgement rather than a boolean argument, because a boolean is
    something you pass without reading. To get the text of a descriptor out of
    this module you have to name the version and the decode you are about to
    check it against, and sign it.
    """

    target_version: str
    target_decode: str
    acknowledged_by: str

    def __post_init__(self) -> None:
        _require(self.target_version, "target_version", "re-verification")
        _require(self.target_decode, "target_decode", "re-verification")
        _require(self.acknowledged_by, "acknowledged_by", "re-verification")

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_version": self.target_version,
            "target_decode": self.target_decode,
            "acknowledged_by": self.acknowledged_by,
        }


class RecalledDescriptor:
    """A host descriptor that was true for exactly ONE version, wrapped so it cannot be applied.

    Deliberately not a `str` and deliberately not printable as one. `LX/05t2;`
    is a 1990-line Reels builder in 430 and an unrelated 596-line class in 439,
    so a descriptor that escapes into an operation is a patch on the wrong class
    that will assemble, verify, and do nothing. Interpolating one of these into
    an operation yields an obvious placeholder instead of a plausible answer, and
    :meth:`reverify` is the only way to the text.
    """

    __slots__ = ("_hook_id", "_version", "_descriptor")

    def __init__(self, hook_id: str, version: str, descriptor: str) -> None:
        self._hook_id = _require(hook_id, "hook_id", "recalled descriptor")
        self._version = _require(version, "version", "recalled descriptor")
        self._descriptor = _require(descriptor, "descriptor", "recalled descriptor")

    @property
    def hook_id(self) -> str:
        return self._hook_id

    @property
    def version(self) -> str:
        """The ONLY version this descriptor was ever true for."""
        return self._version

    @property
    def fingerprint(self) -> str:
        """A content hash, so two recalled hosts can be told apart without being read.

        This is how a report can say "memory holds two different answers here"
        without the answers themselves appearing in it.
        """
        return canonical_sha256(self._descriptor)

    def __repr__(self) -> str:
        return (
            f"<RecalledDescriptor {self._hook_id}@{self._version}: withheld until "
            "re-verified against the target decode>"
        )

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RecalledDescriptor):
            return NotImplemented
        return (self._hook_id, self._version, self._descriptor) == (
            other._hook_id,
            other._version,
            other._descriptor,
        )

    def __hash__(self) -> int:
        return hash((self._hook_id, self._version, self._descriptor))

    def reverify(self, acknowledgement: Reverification) -> str:
        """Yield the stored text, to a caller that has said where it will re-check it.

        Refuses an acknowledgement for a different version than the one being
        ported: an acknowledgement that names 430 while the run targets 439 is
        not an acknowledgement of anything.
        """
        if not isinstance(acknowledgement, Reverification):
            raise DecisionError(
                f"{self._hook_id}@{self._version}: a recalled descriptor is released only "
                f"against a Reverification, got {type(acknowledgement).__name__}. It named "
                "one version's class layout and names a different class in the next."
            )
        return self._descriptor

    def reverify_for(self, target_version: str, acknowledgement: Reverification) -> str:
        """As :meth:`reverify`, but pinned to the version the caller is porting to."""
        _require(target_version, "target_version")
        if acknowledgement.target_version.strip() != target_version.strip():
            raise DecisionError(
                f"{self._hook_id}@{self._version}: re-verification acknowledges "
                f"{acknowledgement.target_version!r} but the run targets {target_version!r}. "
                "Acknowledging the wrong decode is not acknowledging anything."
            )
        return self.reverify(acknowledgement)

    def to_dict(self) -> dict[str, Any]:
        """The storage form. A record without the descriptor would not be a record.

        The key is not ``descriptor``, so a dict from here cannot be splatted into
        an applier operation and have the field land in the right place.
        """
        return {
            "hook_id": self._hook_id,
            "valid_only_for_version": self._version,
            "descriptor_pending_reverification": self._descriptor,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RecalledDescriptor:
        return cls(
            hook_id=data["hook_id"],
            version=data["valid_only_for_version"],
            descriptor=data["descriptor_pending_reverification"],
        )


# ---------------------------------------------------------------- the records


@dataclass(frozen=True)
class Route:
    """The primary product of this module: how a past run FOUND the host.

    A route carries no descriptor at all, which is what makes it safe to hand
    straight to the next version's proposer. It is still version-stamped,
    because the lines it cites are lines in one decode.
    """

    hook_id: str
    version: str
    technique: str
    chain: tuple[Step, ...]
    signals: tuple[str, ...]
    smali_path_then: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook_id,
            "version": self.version,
            "technique": self.technique,
            "signals": list(self.signals),
            "smali_path_then": self.smali_path_then,
            "chain": [step.to_dict() for step in self.chain],
        }


@dataclass(frozen=True)
class Hint:
    """What memory offers a new version: a technique, a route, and a warning label.

    There is no method here that yields an applier-shaped operation, and
    :attr:`must_reverify` cannot be set to False. The descriptor lives behind
    :attr:`host`, which is a :class:`RecalledDescriptor` and not a string.
    """

    hook_id: str
    recorded_version: str
    target_version: str
    technique: str
    chain: tuple[Step, ...]
    signals: tuple[str, ...]
    smali_path_then: str
    reuse: Reusability
    host: RecalledDescriptor
    must_reverify: bool = True

    def __post_init__(self) -> None:
        if self.must_reverify is not True:
            raise DecisionError(
                f"{self.hook_id}: a hint may not claim it needs no re-verification. Every "
                "recalled host is one version's answer, and the next version recycles the "
                "name onto a different class."
            )
        if not isinstance(self.host, RecalledDescriptor):
            raise DecisionError(
                f"{self.hook_id}: a hint's host must be a RecalledDescriptor, not a bare "
                "string that could be dropped into an operation"
            )

    @property
    def route(self) -> Route:
        return Route(
            hook_id=self.hook_id,
            version=self.recorded_version,
            technique=self.technique,
            chain=self.chain,
            signals=self.signals,
            smali_path_then=self.smali_path_then,
        )

    def reverified_host(self, acknowledgement: Reverification) -> str:
        """The recalled descriptor, released against an acknowledgement for THIS target.

        The strict path, and the one callers should use: it pins the
        acknowledgement to the version this hint was recalled for, so an
        acknowledgement copied from a previous run cannot release it.
        """
        return self.host.reverify_for(self.target_version, acknowledgement)

    def to_dict(self) -> dict[str, Any]:
        """Report form. Carries no descriptor: that is what :attr:`host` is for."""
        return {
            "hook_id": self.hook_id,
            "recorded_version": self.recorded_version,
            "target_version": self.target_version,
            "must_reverify": self.must_reverify,
            "technique": self.technique,
            "signals": list(self.signals),
            "smali_path_then": self.smali_path_then,
            "chain": [step.to_dict() for step in self.chain],
            "reuse": self.reuse.to_dict(),
            "host": (
                "withheld — call Hint.host.reverify_for(version, Reverification(...)) and "
                "re-resolve it against the target decode"
            ),
        }


@dataclass(frozen=True)
class Resolution:
    """Where one hook was resolved in one version, and the route that found it.

    Keyed by (hook_id, version). The descriptor is stored — it has to be — but it
    is stored inside a :class:`RecalledDescriptor` so that no caller reads it by
    accident.
    """

    hook_id: str
    version: str
    host: RecalledDescriptor
    smali_path: str
    technique: str
    chain: tuple[Step, ...]
    compatibility: Compatibility = field(default_factory=Compatibility)
    signals: tuple[str, ...] = ()
    recorded_at: str = ""

    def __post_init__(self) -> None:
        _require(self.hook_id, "hook_id", "resolution")
        _require(self.version, "version", "resolution")
        _require(self.smali_path, "smali_path", self.hook_id)
        _require(self.technique, "technique", self.hook_id)
        if not isinstance(self.host, RecalledDescriptor):
            raise DecisionError(
                f"{self.hook_id}: host must be a RecalledDescriptor. A plain string here "
                "is a descriptor one `.host` away from an applier operation."
            )
        if self.host.hook_id != self.hook_id or self.host.version != self.version:
            raise DecisionError(
                f"{self.hook_id}@{self.version}: host is recalled as "
                f"{self.host.hook_id}@{self.host.version}. A record whose key and whose "
                "descriptor disagree would answer for the wrong hook or the wrong version."
            )
        if not self.chain:
            raise DecisionError(
                f"{self.hook_id}@{self.version}: a resolution needs its evidence chain. "
                "The chain is the reusable part; the descriptor is the disposable part, "
                "and a record with only the descriptor is the stale answer this store "
                "exists to avoid."
            )
        if any(not isinstance(step, Step) for step in self.chain):
            raise DecisionError(f"{self.hook_id}: every chain entry must be a Step")
        if FORBIDDEN_SIGNAL in self.signals:
            raise DecisionError(
                f"{self.hook_id}@{self.version}: {FORBIDDEN_SIGNAL!r} is not a signal a "
                "resolution may claim. Every 430 host name still exists in 439 and names "
                "a different class; finding a host 'by its descriptor' is the join this "
                "module forbids."
            )

    @property
    def key(self) -> Key:
        return Key(self.hook_id, self.version)

    @property
    def route(self) -> Route:
        """The descriptor-free part, safe to hand to the next version's proposer."""
        return Route(
            hook_id=self.hook_id,
            version=self.version,
            technique=self.technique,
            chain=self.chain,
            signals=self.signals,
            smali_path_then=self.smali_path,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook_id,
            "version": self.version,
            "host": self.host.to_dict(),
            "smali_path": self.smali_path,
            "technique": self.technique,
            "chain": [step.to_dict() for step in self.chain],
            "compatibility": self.compatibility.to_dict(),
            "signals": list(self.signals),
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Resolution:
        return cls(
            hook_id=data["hook_id"],
            version=data["version"],
            host=RecalledDescriptor.from_dict(data["host"]),
            smali_path=data["smali_path"],
            technique=data["technique"],
            chain=tuple(Step.from_dict(step) for step in data["chain"]),
            compatibility=Compatibility.from_dict(data.get("compatibility", {})),
            signals=tuple(data.get("signals", ())),
            recorded_at=data.get("recorded_at", ""),
        )


@dataclass(frozen=True)
class Miss:
    """A hook that turned out inert or wrong, in which version, and what caught it.

    The most valuable table in the store, because every entry is a confident
    error that nothing standing would have caught. Keyed by (hook_id, version):
    the 430 settings hook was dead and the 439 one may be dead for an entirely
    different reason, and collapsing them would lose both.
    """

    hook_id: str
    version: str
    status: MissStatus
    detector: Detector
    summary: str
    detail: Mapping[str, Any] = field(default_factory=dict)
    compatibility: Compatibility = field(default_factory=Compatibility)
    detected_at: str = ""
    recorded_at: str = ""

    def __post_init__(self) -> None:
        _require(self.hook_id, "hook_id", "miss")
        _require(self.version, "version", "miss")
        _require(self.summary, "summary", self.hook_id)
        if not isinstance(self.status, MissStatus):
            raise DecisionError(
                f"{self.hook_id}@{self.version}: status must be a MissStatus. 'suspected' "
                "and 'confirmed' are different claims and must not be spelled freely."
            )
        if not isinstance(self.detector, Detector):
            raise DecisionError(
                f"{self.hook_id}@{self.version}: detector must be a Detector — a miss with "
                "no identifiable finder cannot be weighed against the next one"
            )

    @property
    def key(self) -> Key:
        return Key(self.hook_id, self.version)

    @property
    def proven_at_runtime(self) -> bool:
        """Did a phone settle this, or is it still an argument?"""
        return self.status is MissStatus.CONFIRMED and self.detector in {
            Detector.DEVICE_SESSION,
            Detector.DIFFERENTIAL,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook_id,
            "version": self.version,
            "status": self.status.value,
            "detector": self.detector.value,
            "summary": self.summary,
            "detail": dict(self.detail),
            "compatibility": self.compatibility.to_dict(),
            "detected_at": self.detected_at,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Miss:
        return cls(
            hook_id=data["hook_id"],
            version=data["version"],
            status=MissStatus(data["status"]),
            detector=Detector(data["detector"]),
            summary=data["summary"],
            detail=dict(data.get("detail", {})),
            compatibility=Compatibility.from_dict(data.get("compatibility", {})),
            detected_at=data.get("detected_at", ""),
            recorded_at=data.get("recorded_at", ""),
        )


@dataclass(frozen=True)
class SurvivalRate:
    """How often one class of fingerprint survived one version step, measured.

    Not keyed by hook: this is a property of the version pair, and it is here so
    that fingerprint precedence is an argument about numbers rather than about
    who remembers what.
    """

    from_version: str
    to_version: str
    signal: str
    rate: float
    measured_by: str
    survived: int | None = None
    total: int | None = None
    recorded_at: str = ""

    def __post_init__(self) -> None:
        _require(self.from_version, "from_version", "survival rate")
        _require(self.to_version, "to_version", "survival rate")
        _require(self.signal, "signal", "survival rate")
        _require(self.measured_by, "measured_by", self.signal)
        if self.from_version == self.to_version:
            raise DecisionError(
                f"{self.signal}: a survival rate needs two different versions, got "
                f"{self.from_version!r} twice"
            )
        if self.signal == FORBIDDEN_SIGNAL:
            raise DecisionError(
                f"{FORBIDDEN_SIGNAL!r} may not be measured here. Its name-level survival is "
                "near total and its meaning survival is zero, so storing the number would "
                "rank the one forbidden signal first in precedence()."
            )
        if type(self.rate) not in (float, int) or not 0.0 <= self.rate <= 1.0:
            raise DecisionError(
                f"{self.signal}: rate must be within 0..1, got {self.rate!r}"
            )
        if (self.survived is None) != (self.total is None):
            raise DecisionError(
                f"{self.signal}: give both survived and total or neither; half a count "
                "cannot be checked against the rate"
            )
        if self.total is not None:
            if type(self.total) is not int or type(self.survived) is not int:
                raise DecisionError(f"{self.signal}: counts must be ints")
            if self.total <= 0:
                raise DecisionError(f"{self.signal}: total must be > 0, got {self.total}")
            if not 0 <= self.survived <= self.total:
                raise DecisionError(
                    f"{self.signal}: survived {self.survived} of {self.total} is impossible"
                )
            measured = self.survived / self.total
            if abs(measured - self.rate) > 0.0005:
                raise DecisionError(
                    f"{self.signal}: recorded rate {self.rate} contradicts its own counts "
                    f"({self.survived}/{self.total} = {measured:.4f}). A rate that "
                    "disagrees with its evidence would silently reorder precedence()."
                )

    @property
    def pair(self) -> tuple[str, str]:
        return (self.from_version, self.to_version)

    @property
    def percent(self) -> str:
        return f"{self.rate * 100:.1f}%"

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_version": self.from_version,
            "to_version": self.to_version,
            "signal": self.signal,
            "rate": self.rate,
            "percent": self.percent,
            "measured_by": self.measured_by,
            "survived": self.survived,
            "total": self.total,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SurvivalRate:
        return cls(
            from_version=data["from_version"],
            to_version=data["to_version"],
            signal=data["signal"],
            rate=data["rate"],
            measured_by=data["measured_by"],
            survived=data.get("survived"),
            total=data.get("total"),
            recorded_at=data.get("recorded_at", ""),
        )


Record = Resolution | Miss | SurvivalRate

_DECODERS = {
    RecordKind.RESOLUTION: Resolution,
    RecordKind.MISS: Miss,
    RecordKind.SURVIVAL: SurvivalRate,
}


def _kind_of(record: Record) -> RecordKind:
    if isinstance(record, Resolution):
        return RecordKind.RESOLUTION
    if isinstance(record, Miss):
        return RecordKind.MISS
    if isinstance(record, SurvivalRate):
        return RecordKind.SURVIVAL
    raise DecisionError(
        f"{type(record).__name__} is not a decision record. This store holds resolutions, "
        "misses and survival rates only — in particular it holds no class-level "
        "version-to-version diff, because a descriptor-keyed cross-version record returns "
        "a confident wrong answer rather than a miss."
    )


def _decode(data: Mapping[str, Any]) -> Record:
    if data.get("schema_version") != SCHEMA_VERSION:
        raise DecisionError(f"unsupported decision schema {data.get('schema_version')!r}")
    kind = RecordKind(data["kind"])
    return _DECODERS[kind].from_dict(data["record"])


def stamped(record: Record, recorded_at: str) -> Record:
    """Attach a timestamp from outside, so nothing in this module reads the clock.

    Same contract as `evidence.stamped`: workflow code has to stay deterministic
    under replay, so the time comes from an Activity or a test rather than from
    `datetime.now()` here.
    """
    _kind_of(record)
    return replace(record, recorded_at=recorded_at)


def precedence(rates: Sequence[SurvivalRate]) -> tuple[str, ...]:
    """Fingerprint signals ordered by measured survival, strongest first.

    Refuses to mix version pairs: 430->439 and 439->440 are different
    measurements, and averaging them would produce a ranking that describes
    neither step.
    """
    pairs = {rate.pair for rate in rates}
    if len(pairs) > 1:
        listed = ", ".join(f"{a}->{b}" for a, b in sorted(pairs))
        raise DecisionError(
            f"precedence() mixes version pairs ({listed}). Survival is measured per step; "
            "a blended ranking describes no step that was actually measured."
        )
    return tuple(rate.signal for rate in sorted(rates, key=lambda item: (-item.rate, item.signal)))


# ------------------------------------------------------------------ the store


class DecisionMemory:
    """Append-only memory of resolutions, misses and measured survival rates.

    JSONL, one record per line, canonical JSON — the same shape and spirit as
    `EvidenceLedger`, so the file stays greppable and diffable and a crashed run
    keeps everything it had already learned. Nothing rewrites a line.
    """

    def __init__(self, path: Path | str | None = None):
        self._path = Path(path) if path is not None else None
        self._resolutions: list[Resolution] = []
        self._misses: list[Miss] = []
        self._survival: list[SurvivalRate] = []

    # ------------------------------------------------------------------ write

    def record(self, item: Record) -> Record:
        """Append one record. Never edits, never deduplicates."""
        kind = _kind_of(item)
        self._absorb(item, kind)
        if self._path is not None:
            self._append_to_disk(item, kind)
        return item

    def _absorb(self, item: Record, kind: RecordKind) -> None:
        if kind is RecordKind.RESOLUTION:
            self._resolutions.append(item)  # type: ignore[arg-type]
        elif kind is RecordKind.MISS:
            self._misses.append(item)  # type: ignore[arg-type]
        else:
            self._survival.append(item)  # type: ignore[arg-type]

    def _append_to_disk(self, item: Record, kind: RecordKind) -> None:
        assert self._path is not None
        self._path.parent.mkdir(parents=True, exist_ok=True)
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "kind": kind.value,
            "record": item.to_dict(),
        }
        with open(self._path, "a", encoding="utf-8") as handle:
            handle.write(canonical_json(envelope))
            handle.write("\n")

    # ------------------------------------------------------------------- read

    @property
    def resolutions(self) -> tuple[Resolution, ...]:
        return tuple(self._resolutions)

    @property
    def misses(self) -> tuple[Miss, ...]:
        return tuple(self._misses)

    @property
    def survival(self) -> tuple[SurvivalRate, ...]:
        return tuple(self._survival)

    @property
    def hooks(self) -> tuple[str, ...]:
        return tuple(
            sorted({item.hook_id for item in (*self._resolutions, *self._misses)})
        )

    def resolutions_for(
        self, hook_id: str, version: str | None = None
    ) -> tuple[Resolution, ...]:
        return tuple(
            item
            for item in self._resolutions
            if item.hook_id == hook_id and (version is None or item.version == version)
        )

    def misses_for(self, hook_id: str, version: str | None = None) -> tuple[Miss, ...]:
        return tuple(
            item
            for item in self._misses
            if item.hook_id == hook_id and (version is None or item.version == version)
        )

    def routes_for(self, hook_id: str) -> tuple[Route, ...]:
        """The techniques recorded for this hook, in every version, with no descriptors.

        This is the product. A route stays useful when the answer has rotted,
        which is the difference between memory that helps and memory that lies.
        """
        return tuple(item.route for item in self.resolutions_for(hook_id))

    def by_key(self, key: Key) -> dict[str, Any]:
        """Everything filed under one (hook_id, version), and nothing filed elsewhere.

        Reports rather than storage rows. `Resolution.to_dict` is the *file*
        format and necessarily carries the descriptor; a query that returned it
        would put the stale answer back on the query surface, one dict subscript
        from an operation.
        """
        if not isinstance(key, Key):
            raise DecisionError(
                f"decision memory is keyed by (hook_id, version); got "
                f"{type(key).__name__}. A bare string is either half a key or a "
                "descriptor, and a descriptor is not a key here at all."
            )
        return {
            "key": key.to_dict(),
            "resolutions": [
                self._resolution_row(item)
                for item in self.resolutions_for(key.hook_id, key.version)
            ],
            "misses": [item.to_dict() for item in self.misses_for(key.hook_id, key.version)],
        }

    def lookup_by_descriptor(self, descriptor: str) -> NoReturn:
        """Always raises. It exists to refuse, by name, the query someone will try.

        "Where did `LX/06X7;` end up this time?" has an answer that looks right
        and is wrong: the name exists in the next version and belongs to another
        class. Raising here turns that mistake into an exception instead of a
        patch on an unrelated class that assembles and does nothing.
        """
        raise DecisionError(
            f"refusing to look up {descriptor!r} across versions. Obfuscated names are "
            "recycled — LX/05t2 is a 1990-line Reels builder in 430 and an unrelated "
            "596-line class in 439 — so a descriptor-keyed answer is confidently wrong "
            "rather than absent. Ask by (hook_id, version), and re-resolve the host "
            "against the target decode using the recorded technique."
        )

    def survival_for(self, from_version: str, to_version: str) -> tuple[SurvivalRate, ...]:
        return tuple(
            item
            for item in self._survival
            if item.from_version == from_version and item.to_version == to_version
        )

    def precedence(self, from_version: str, to_version: str) -> tuple[str, ...]:
        return precedence(self.survival_for(from_version, to_version))

    # ------------------------------------------------------------------ recall

    def conflicts_for(self, hook_id: str, version: str) -> tuple[str, ...]:
        """Opaque ids of the distinct answers recorded for one key, if there are several.

        Two different answers under one (hook_id, version) means memory
        contradicts itself, and breaking that tie by recency would pick one at
        random and present it with full confidence. What a caller needs is that
        there *are* two; the answers themselves are fingerprints rather than
        descriptors, so reporting a conflict cannot become the way a descriptor
        gets out.
        """
        seen: list[str] = []
        for item in self.resolutions_for(hook_id, version):
            token = fingerprint_of(item.smali_path, item.host.fingerprint)[:12]
            if token not in seen:
                seen.append(token)
        return tuple(seen) if len(seen) > 1 else ()

    def _resolution_row(
        self, item: Resolution, current: Context | None = None
    ) -> dict[str, Any]:
        """One resolution as a report row: everything except the descriptor.

        The single place a resolution is rendered for a caller, so there is one
        place to check that the answer stays behind :class:`RecalledDescriptor`
        and not one per query method.
        """
        return {
            "version": item.version,
            "smali_path_then": item.smali_path,
            "technique": item.technique,
            "signals": list(item.signals),
            "chain": [entry.to_dict() for entry in item.chain],
            "compatibility": item.compatibility.to_dict(),
            "recorded_at": item.recorded_at,
            "descriptor_available": "only via Hint.host.reverify_for(...)",
            "known_miss_here": bool(self.misses_for(item.hook_id, item.version)),
            "conflicting_answers": list(self.conflicts_for(item.hook_id, item.version)),
            "reuse": (
                reusable(item, current).to_dict()
                if current is not None
                else {
                    "reusable": False,
                    "changed": [],
                    "unknown": list(REUSE_DIMENSIONS),
                    "blocking": list(REUSE_DIMENSIONS),
                    "reasons": [
                        "no context supplied, so none of the four dimensions could be "
                        "compared; unknown is never reusable"
                    ],
                }
            ),
        }

    def hint(self, hook_id: str, current: Context) -> Hint | None:
        """The most recent reusable resolution, as a hint. ``None`` when there is none.

        Refused, and so ``None``, when:

          * no stored resolution passes :func:`reusable` for *current*;
          * the record's own (hook_id, version) has a recorded miss — the thing
            being replayed is known to have gone wrong exactly there;
          * memory holds two different answers for that key.

        ``None`` is not "nothing is known": :meth:`recall` still has the routes,
        the misses, and the reason each record was refused.
        """
        if not isinstance(current, Context):
            raise DecisionError(
                f"hint() needs a Context, got {type(current).__name__}; without one there "
                "is nothing to judge staleness against, and an unjudged hint is an answer"
            )
        for item in reversed(self.resolutions_for(hook_id)):
            assessment = reusable(item, current)
            if not assessment.reusable:
                continue
            if self.misses_for(hook_id, item.version):
                continue
            if self.conflicts_for(hook_id, item.version):
                continue
            return Hint(
                hook_id=item.hook_id,
                recorded_version=item.version,
                target_version=current.version,
                technique=item.technique,
                chain=item.chain,
                signals=item.signals,
                smali_path_then=item.smali_path,
                reuse=assessment,
                host=item.host,
            )
        return None

    def recall(self, hook_id: str, current: Context | None = None) -> dict[str, Any]:
        """Everything known about one hook, JSON-serialisable, with no descriptor in it.

        Answers the question a human at a gate actually asks first: has this hook
        bitten us before? Misses are listed whatever the reuse predicate says
        about them — the predicate governs whether an *answer* may be reused, and
        must never be the reason a *warning* goes unseen.
        """
        _require(hook_id, "hook_id")
        if current is not None and not isinstance(current, Context):
            raise DecisionError(f"recall() needs a Context or None, got {type(current).__name__}")
        resolutions = self.resolutions_for(hook_id)
        misses = self.misses_for(hook_id)
        hint = self.hint(hook_id, current) if current is not None else None

        resolution_rows = [self._resolution_row(item, current) for item in resolutions]

        miss_rows = []
        for item in misses:
            miss_rows.append(
                {
                    **item.to_dict(),
                    "proven_at_runtime": item.proven_at_runtime,
                    "shown_regardless_of_compatibility": True,
                    "compatibility_with_this_run": (
                        reusable(item, current).to_dict() if current is not None else None
                    ),
                }
            )

        return {
            "schema_version": SCHEMA_VERSION,
            "hook_id": hook_id,
            "target_version": current.version if current is not None else None,
            "known": bool(resolutions or misses),
            "resolutions": resolution_rows,
            "misses": miss_rows,
            "routes": [route.to_dict() for route in self.routes_for(hook_id)],
            "hint": hint.to_dict() if hint is not None else None,
            "hint_available": hint is not None,
        }

    def report(self) -> dict[str, Any]:
        """The whole store, JSON-serialisable, for a run summary."""
        return {
            "schema_version": SCHEMA_VERSION,
            "hooks": list(self.hooks),
            "resolution_count": len(self._resolutions),
            "miss_count": len(self._misses),
            "confirmed_misses": [
                item.to_dict()
                for item in self._misses
                if item.status is MissStatus.CONFIRMED
            ],
            "suspected_misses": [
                item.to_dict()
                for item in self._misses
                if item.status is MissStatus.SUSPECTED
            ],
            "survival": [item.to_dict() for item in self._survival],
        }

    # ------------------------------------------------------------ persistence

    @classmethod
    def load(cls, path: Path | str) -> DecisionMemory:
        """Rebuild from JSONL, re-validating every record and naming the bad line."""
        memory = cls(path)
        path = Path(path)
        if not path.exists():
            return memory
        with open(path, "r", encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = _decode(json.loads(line))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                    raise DecisionError(
                        f"{path}:{number}: unreadable decision record: {error}"
                    ) from error
                memory._absorb(record, _kind_of(record))
        return memory


# ------------------------------------------------------------------- the seed
#
# Real entries, all from this project. They are constants rather than a written
# file so that a fresh checkout knows what has already gone wrong, and so that
# nothing here has to read a clock: the dates below are recorded facts, not
# `datetime.now()`.

#: The route three agents rediscovered independently. Line numbers are from the
#: 439 decode at `work/439-explore/stock-439`.
SETTINGS_ROUTE: tuple[Step, ...] = (
    Step(
        action="enter at the profile fragment, not at the action-bar delegate",
        file="smali_classes6/com/instagram/profile/fragment/UserDetailFragment.smali",
        line=2,
        finding=(
            "`.super LX/0D6E;` — the fragment is the only stable named entry point in "
            "this area; every delegate below it is obfuscated and recycled between "
            "versions, so the search must start here"
        ),
    ),
    Step(
        action="take the own-profile override on the single subclass",
        file="smali_classes6/com/instagram/profile/fragment/UserDetailFragment.smali",
        line=16045,
        finding=(
            "`.method public A19()Z` — the self/other discriminator. The patch must sit "
            "behind it or every user's Options becomes long-clickable, which is the "
            "device-verified exclusion the 430 port had to keep"
        ),
    ),
    Step(
        action="find where the fragment applies that gate",
        file="smali_classes6/com/instagram/profile/fragment/UserDetailFragment.smali",
        line=946,
        finding=(
            "`invoke-virtual {p0}, ...->A19()Z` — one of three call sites; these are the "
            "gate the injected listener has to live behind"
        ),
    ),
    Step(
        action="follow the fragment's delegate field to the runtime branch",
        file="smali_classes6/com/instagram/profile/fragment/UserDetailFragment.smali",
        line=81,
        finding=(
            "`.field public A0L:LX/0Di2;` — the legacy IgActionBar delegate. MobileConfig "
            "0x81099a000034a6 selects between this and the ProfileActionBar variant at "
            "runtime, so BOTH implementations must be resolved and patched"
        ),
    ),
    Step(
        action="pin the control by drawable NAME in the delegate that builds it",
        file="smali_classes6/X/0DnT.smali",
        line=42,
        finding=(
            "`invoke-direct {v5, p1, v0}, LX/06yX;-><init>(Landroid/content/Context;I)V` "
            "with the id arriving as data via `LX/0IwP;->A00:I`. The control is identified "
            "by the drawable's NAME at the caller; the hex id is then re-resolved from "
            "THIS version's index, because only 103 of 11,737 drawable ids survived "
            "430->439"
        ),
    ),
)

SETTINGS_TECHNIQUE = (
    "UserDetailFragment -> the A19() override on the single subclass -> the own-profile "
    "gate -> pin the control by DRAWABLE NAME, then resolve that name to this version's "
    "id. Resolve BOTH action-bar implementations: MobileConfig 0x81099a000034a6 picks "
    "between them at runtime, so the same APK can need either."
)

SETTINGS_SIGNALS: tuple[str, ...] = (STABLE_NAMED_TYPE, STRUCTURAL_SHAPE, DRAWABLE_NAME)

SETTINGS_COMPATIBILITY = Compatibility(
    semantic_feature_identity="profile_options_long_press.settings_dialog",
    delivery_mechanism="ui_attach:view_long_click_listener",
    evidence_fingerprint=fingerprint_of(SETTINGS_TECHNIQUE, list(SETTINGS_SIGNALS)),
    policy_revision="r1",
)


def seed_records() -> tuple[Record, ...]:
    """The records this project has already paid for, in append order.

    A function rather than a module constant so that a caller cannot mutate the
    seed for everyone else, and so the resolution's :class:`RecalledDescriptor`
    is built fresh each time.
    """
    return (
        Resolution(
            hook_id="install_settings_long_click",
            version="439",
            host=RecalledDescriptor(
                "install_settings_long_click", "439", "LX/0DnT;"
            ),
            smali_path="smali_classes6/X/0DnT.smali",
            technique=SETTINGS_TECHNIQUE,
            chain=SETTINGS_ROUTE,
            compatibility=SETTINGS_COMPATIBILITY,
            signals=SETTINGS_SIGNALS,
        ),
        Miss(
            # NOTE ON KEYING: the `minshop`/`minishops` bug lived in the Shopping
            # identifier substitutions (`LX/51R`, `Oyz`, `Pzz` — see
            # docs/DFINSTA_1.4.1_DELTA.md:55), not in the settings hook. It is
            # filed under the hook it actually belongs to, because a miss filed
            # against the wrong hook_id is retrieved by the wrong hook, which is
            # exactly the confident wrong answer this store exists to prevent.
            hook_id="substitute_shopping_identifiers",
            version="340",
            status=MissStatus.CONFIRMED,
            detector=Detector.STATIC_AUDIT,
            detected_at="",
            summary=(
                "the substitution tested for `minshop` while every patched identifier "
                "contained `minishops`, so the comparison could never match and the helper "
                "returned the original string with the toggle on or off"
            ),
            detail={
                "checked_for": "minshop",
                "identifiers_actually_contained": "minishops",
                "effect": "three direct Shopping substitutions were no-ops in a shipped release",
                "found_how": "static audit, long after shipping; no check at the time looked",
                "source": "docs/DFINSTA_1.4.1_DELTA.md:55, docs/RECONSTRUCTION_1.4.1.md:73",
            },
        ),
        Miss(
            hook_id="install_settings_long_click",
            version="430",
            status=MissStatus.CONFIRMED,
            detector=Detector.DEVICE_SESSION,
            detected_at="2026-08-01",
            summary=(
                "applied cleanly and passed every static assertion, and was dead at "
                "runtime: MobileConfig 0x81099a000034a6 selected the legacy IgActionBar "
                "implementation, so the patched ProfileActionBar variant was never built"
            ),
            detail={
                "flag": "0x81099a000034a6",
                "gate": "UserDetailFragment gates on LX/05mS;->A03(session)",
                "patched_variant": "com/instagram/profile/actionbar/ProfileActionBar via LX/077K;->A00",
                "live_variant": "legacy LX/00ds IgActionBar via LX/06X7;->AP1",
                "why_static_could_not_see_it": (
                    "the discriminator is server-driven, so the same APK can require either "
                    "hook and no property of the file distinguishes them"
                ),
                "remedy": "install_settings_long_click_actionbar was added so both are patched",
                "source": "docs/PORT_430_MAPPING.md:260",
            },
        ),
        Miss(
            hook_id="install_settings_long_click_actionbar",
            version="439",
            status=MissStatus.SUSPECTED,
            detector=Detector.ADVERSARIAL_VERIFIER,
            detected_at="2026-08-01",
            summary=(
                "probably inert: LX/0Di2;->Ac0(LX/004C;)V appears never to be invoked, so "
                "the patched site may never run. SUSPECTED, not confirmed — this is a "
                "static argument, and a static argument is exactly what certified the 430 "
                "settings hook that turned out to be dead"
            ),
            detail={
                "argument": [
                    "exactly four invoke-interface LX/0Pvr;->Ac0 sites exist and none can hold a 0Di2",
                    "no A1K(LX/0Pvr;) call site passes one",
                    "UserDetailFragment only calls A02 and hands it to LX/0DEm.A00",
                    "LX/0DEm.A00 reads the A01 View that Ac0 itself would have set",
                ],
                "independently_rechecked": True,
                "probe_is_blind_here": (
                    "both settings hooks declare the same ui_dialog signal on the same "
                    "surface, so the observed dialog cannot say which one opened it"
                ),
                "live_control_appears_to_be": "ProfileActionBar + LX/0Dxw bound by LX/0DnT",
                "decisive_test": "a build carrying only this hook",
                "source": "manifest/hooks.json, install_settings_long_click_actionbar host note",
            },
        ),
        # Measured 430->439 by tools/indexer/build_index.py. These are the numbers
        # behind the fingerprint precedence, so it can be argued with.
        SurvivalRate(
            from_version="430",
            to_version="439",
            signal=DRAWABLE_NAME,
            rate=0.988,
            measured_by="tools/indexer/build_index.py",
        ),
        SurvivalRate(
            from_version="430",
            to_version="439",
            signal=API_PATH_LITERAL,
            rate=0.939,
            measured_by="tools/indexer/build_index.py",
        ),
        SurvivalRate(
            from_version="430",
            to_version="439",
            signal=STABLE_NAMED_TYPE,
            rate=0.893,
            measured_by="tools/indexer/build_index.py",
        ),
        SurvivalRate(
            from_version="430",
            to_version="439",
            signal=DRAWABLE_ID,
            rate=0.009,
            measured_by="tools/indexer/build_index.py",
            survived=103,
            total=11737,
        ),
    )


def seeded_memory(path: Path | str | None = None) -> DecisionMemory:
    """A memory preloaded with what this project already learned the hard way."""
    memory = DecisionMemory(path)
    for record in seed_records():
        memory.record(record)
    return memory


# ------------------------------------------------------------------------- cli


def _render(report: Mapping[str, Any]) -> list[str]:
    """The recall report as lines. Pure, so the CLI's output is testable."""
    lines: list[str] = []
    hook_id = report["hook_id"]
    if not report["known"]:
        lines.append(f"{hook_id}: nothing recorded. This hook has not bitten us before —")
        lines.append("  which is not the same as it being safe; it may simply be new.")
        return lines

    lines.append(f"{hook_id}")
    target = report["target_version"]
    lines.append(f"  target version: {target or '(none supplied)'}")

    misses = report["misses"]
    lines.append("")
    if misses:
        lines.append(f"  MISSES ({len(misses)}) — has this hook bitten us before?")
        for miss in misses:
            mark = "!!" if miss["status"] == MissStatus.CONFIRMED.value else "? "
            when = f" on {miss['detected_at']}" if miss["detected_at"] else ""
            lines.append(
                f"   {mark} {miss['version']:>4}  {miss['status']:<9} "
                f"detected by {miss['detector']}{when}"
            )
            lines.append(f"        {miss['summary']}")
            if not miss["proven_at_runtime"]:
                lines.append(
                    "        (not settled on a device — treat as an open question)"
                )
    else:
        lines.append("  MISSES: none recorded")

    lines.append("")
    routes = report["routes"]
    if routes:
        lines.append(f"  TECHNIQUE ({len(routes)} recorded)")
        for route in routes:
            lines.append(f"   from {route['version']}: {route['technique']}")
            lines.append(f"     signals: {', '.join(route['signals']) or '(none recorded)'}")
            for number, step in enumerate(route["chain"], start=1):
                lines.append(f"     {number}. {step['action']}")
                lines.append(f"        {step['file']}:{step['line']}")
                lines.append(f"        {step['finding']}")
    else:
        lines.append("  TECHNIQUE: none recorded")

    lines.append("")
    lines.append("  REUSE")
    for row in report["resolutions"]:
        verdict = "reusable" if row["reuse"]["reusable"] else "NOT reusable"
        lines.append(f"   {row['version']}: {verdict}")
        for reason in row["reuse"].get("reasons", []):
            lines.append(f"     - {reason}")
        if row["known_miss_here"]:
            lines.append("     - a miss is recorded for this exact version; no hint offered")
        if row["conflicting_answers"]:
            lines.append(
                "     - memory holds more than one answer for this version; that goes to "
                "a human rather than being broken by recency"
            )
    if not report["resolutions"]:
        lines.append("   (no resolution recorded)")

    lines.append("")
    if report["hint_available"]:
        lines.append(
            "  A hint is available. It carries the technique and the chain; the host "
            "descriptor is withheld until re-verified against the target decode."
        )
    else:
        lines.append("  No hint. Re-resolve from the technique above against the target decode.")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    recall = sub.add_parser("recall", help="what is known about a hook")
    recall.add_argument("hook_id")
    recall.add_argument("--version", help="the version being ported to")
    recall.add_argument(
        "--memory",
        type=Path,
        default=DEFAULT_MEMORY_PATH,
        help=f"decision memory JSONL (default {DEFAULT_MEMORY_PATH})",
    )
    recall.add_argument("--feature-identity", default="", help="semantic feature identity")
    recall.add_argument("--delivery", default="", help="delivery mechanism")
    recall.add_argument("--evidence-fingerprint", default="", help="evidence fingerprint")
    recall.add_argument("--policy-revision", default="", help="policy revision")
    recall.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    if args.memory.exists():
        memory = DecisionMemory.load(args.memory)
    else:
        print(
            f"note: no decision memory at {args.memory}; showing the seeded record only",
            file=sys.stderr,
        )
        memory = seeded_memory()

    current = None
    if args.version:
        current = Context(
            version=args.version,
            compatibility=Compatibility(
                semantic_feature_identity=args.feature_identity,
                delivery_mechanism=args.delivery,
                evidence_fingerprint=args.evidence_fingerprint,
                policy_revision=args.policy_revision,
            ),
        )
    report = memory.recall(args.hook_id, current)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        for line in _render(report):
            print(line)
    return 0 if report["known"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

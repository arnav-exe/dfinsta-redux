"""Stage 3: diff two versions at the layer that survives obfuscation.

Given a baseline Instagram version and a new one, report what CHANGED in the
API surface, so a later stage can ask "is this new feature addictive?" and put
the answer plus its grounding in front of a human. This module produces
**evidence only**. It has no opinion about addictiveness, and a function here
that returned ``addictive: true`` would be the wrong shape: the judgement is a
later stage with a durable human gate, and the gate is worthless if the
evidence was already pre-judged.

===============================================================================
  DIFF THE STABLE-STRING LAYER.  NEVER THE CLASS LAYER.
===============================================================================

Measured on this repo's own 430 and 439 indexes, not assumed:

* Obfuscated class names are **recycled**, not merely renamed. ``LX/05t2``
  exists in both versions and is a different class in each (1990 lines carrying
  Reels endpoints in 430, 596 lines carrying none in 439). A class-level diff
  reports "everything changed" and buries every real signal in it.
* Of the 11,737 drawable names present in both versions, only 103 keep their
  hex id — **99.1% are renumbered**. Resources are therefore diffed BY NAME;
  an id diff is pure noise. :class:`SurfaceSnapshot` keeps ids in a separate
  mapping that the diff never reads, so this is structural rather than a rule
  someone has to remember. What the ids are still good for is measuring their
  own instability: :meth:`SurfaceSnapshot.resource_id_stability`.
* API-path string literals survive a version bump at **93.9%**, stable named
  types (``Lcom/instagram/...``, ``Lcom/facebook/...``) at **89.3%**. Those two
  are the signal, and every rate this module prints is recomputed from the
  indexes it was handed rather than quoted from this docstring.
* String resource ids are unresolvable under sparse resource encoding (~555 of
  ~19,000 exposed), so they are never indexed and never diffed.

The one place a per-version descriptor is legitimately used is *within* one
version: how many classes hold a literal, and which literals share a class.
Those numbers are compared across versions; the descriptors that produced them
are not, and never appear in the report — :meth:`SurfaceDiff.to_dict` emits
counts precisely so nothing downstream can be tempted to join on a name.

Why co-location is reported at all: Shopping "dissolved into other endpoints"
between versions and stopped being blockable by one rule. That transition is
invisible in an added/removed diff — the literal is present on both sides — and
shows up only as a change in how many classes carry it.

Usage
-----
    python -m dfinsta_pipeline.surface_diff <baseline-index-dir> <target-index-dir>
        [--manifest manifest/hooks.json] [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any, Iterable, Mapping

from .hook_index import API_SURFACE_FILENAME, HookIndex, IndexUnusable

SCHEMA_VERSION = 1
REPORT_KIND = "dfinsta.surface_diff"

#: What :func:`endpoint_family` returns for anything that is not an API path.
#: Kept out of the blocked-family set: a manifest ``semantic_deps`` entry may be
#: a method reference rather than a path, and folding those into one "unknown"
#: family would make unrelated literals look already-blocked.
UNKNOWN_FAMILY = "unknown"

#: Delivery branch = the COST of blocking, which is the field a human needs
#: before deciding. From ``docs/adk_pipeline_design.md``.
BRANCH_ENDPOINT = "A_endpoint"  # its own path: one rule in throwIfBlocked
BRANCH_INLINE = "B_inline"  # rides inside another response: fragile rewriter
BRANCH_UNKNOWN = "C_unknown"  # neither shown: a human has to look

FEATURE_TYPE_PREFIX = "Lcom/instagram/"
#: ``Lcom/instagram/clips/intf/Foo;`` groups under ``com/instagram/clips``.
PACKAGE_PREFIX_DEPTH = 3

DEFAULT_MANIFEST = Path("manifest/hooks.json")

STABLE_LAYER_NOTE = (
    "Diffed on the stable-string layer (API-path literals, stable named types, "
    "resource NAMES). Obfuscated descriptors are recycled across Instagram "
    "versions and resource ids are renumbered, so neither appears in this report "
    "and neither may be used as a cross-version join key."
)


class SurfaceDiffError(Exception):
    """Raised when the manifest this stage reads cannot be understood.

    Index problems keep raising :class:`~dfinsta_pipeline.hook_index.IndexUnusable`,
    because that is the error the driver already handles.
    """


# --------------------------------------------------------------------------
# literal shape
# --------------------------------------------------------------------------

_WEB_SCHEMES = frozenset({"http", "https"})
_API_VERSION = re.compile(r"^v\d+$")
#: ``%s``, ``{clip_id}``, ``:direct_v2_thread_id`` and bare numbers name nothing.
_PLACEHOLDER = re.compile(r"^(?:%[a-z]|\{[^}]*\}|:[a-z0-9_]+|\d+)$")
#: ``fallback_source_media.jpg``, ``magisk.img``, ``libs.txt`` — a file, not an endpoint.
_FILENAME = re.compile(r"^[^/]*\.[a-z0-9]{1,5}$")
_API_PREFIX = re.compile(r"(?:^|/)api/v\d+/")


def _split_scheme(text: str) -> tuple[str, str]:
    """``https://a.b/c/d`` -> ``("https", "c/d")``; ``ig://home`` -> ``("ig", "home")``.

    For a web URL the first segment is a hostname and names no feature, so it is
    dropped. For an app scheme (``ig://``, ``instagram://``) the first segment IS
    the destination, so it is kept.
    """
    scheme, _, rest = text.partition("://")
    if scheme in _WEB_SCHEMES:
        rest = rest.partition("/")[2]
    return scheme, rest


def endpoint_family(literal: str) -> str:
    """Coarse family of an API path, so related endpoints cluster in the report.

    ``clips/discover/``, ``/api/v1/clips/homecoming/`` and
    ``https://www.instagram.com/reels/videos/{clip_id}`` are three strings and
    two of them are the same feature area; without this they arrive at a human
    as N unrelated literals. The family is the first segment that names
    something: the ``api/vN`` prefix is stripped because every endpoint carries
    it, and placeholder segments are skipped because ``%s`` names nothing.

    The family is syntactic on purpose: ``reels/`` and ``clips/`` stay separate
    families even though they are one product surface. Deciding they are the
    same thing is a judgement, and judgement belongs at the human gate, not in
    an alias table nobody maintains.

    Returns :data:`UNKNOWN_FAMILY` for anything that is not an API path,
    including type descriptors and method references — an uppercase letter is
    the same cheap rejection the indexer's ``looks_like_api_path`` uses.
    """
    if not isinstance(literal, str):
        return UNKNOWN_FAMILY
    text = literal.strip()
    if not text or "/" not in text:
        return UNKNOWN_FAMILY
    if any(character.isupper() for character in text):
        return UNKNOWN_FAMILY
    if "://" in text:
        text = _split_scheme(text)[1]
    segments = [segment for segment in text.split("/") if segment]
    if segments and segments[0] == "api":
        segments = segments[1:]
        if segments and _API_VERSION.match(segments[0]):
            segments = segments[1:]
    for segment in segments:
        if _PLACEHOLDER.match(segment):
            continue
        return segment
    return UNKNOWN_FAMILY


def is_endpoint_path(literal: str) -> bool:
    """Is this literal a request path in its own right?

    This is the whole of the A/B distinction, so it is deliberately
    conservative: a false A tells a human "one line in ``throwIfBlocked`` and
    you are done" about something that is not an endpoint, whereas a false C
    only says "look at this yourself". Wrong in the cheap direction.

    Accepted: a relative path with at least two named segments and Instagram's
    trailing slash (``clips/discover/``), or anything under ``api/vN/``.
    Rejected: app-scheme deep links (``ig://reels_home`` never becomes an
    outgoing API request), web URLs that carry no ``api/vN`` path, single-segment
    paths, and anything whose segments look like filenames.

    The trailing slash carries that much weight because it is measured, not
    stylistic: of the relative literals in the 439 index, the 1,488 that end in
    ``/`` are the endpoint layer (``feed/timeline/``, ``feed/reels_tray/``,
    ``clips/discover/``), while the 489 that do not are dominated by cache
    directories, ``/proc`` paths and image assets. Instagram writes both
    ``feed/timeline`` and ``feed/timeline/``; only the second is the request
    path, and only the second is claimed to be one line of work.
    """
    if endpoint_family(literal) == UNKNOWN_FAMILY:
        return False
    text = literal.strip()
    if "://" in text:
        scheme, rest = _split_scheme(text)
        if scheme not in _WEB_SCHEMES:
            return False
        text = "/" + rest
        if not _API_PREFIX.search(text):
            return False
    segments = [segment for segment in text.split("/") if segment]
    if any(_FILENAME.match(segment) for segment in segments):
        return False
    if segments and segments[0] == "api" and len(segments) > 1 and _API_VERSION.match(segments[1]):
        return True
    return len(segments) >= 2 and text.endswith("/")


def normalise_literal(literal: str) -> str:
    """Key under which two spellings of one path compare equal.

    The manifest writes ``/feed/timeline/`` because that is what the outgoing
    URI looks like; the smali constant is ``feed/timeline``. Without this the
    manifest's own blocked endpoints would read as absent from the index.
    """
    return literal.strip().strip("/")


# --------------------------------------------------------------------------
# stable named types
# --------------------------------------------------------------------------


def package_prefix(descriptor: str, depth: int = PACKAGE_PREFIX_DEPTH) -> str:
    """Grouping key for a stable type: ``Lcom/instagram/clips/intf/Foo;`` -> ``com/instagram/clips``.

    1,569 types were added and 1,909 removed between 430 and 439. That list is
    not a report; the package a type lives in is, because it says WHICH AREA of
    the app grew. The class name is never part of the key, so a package with one
    class does not become its own group.
    """
    body = descriptor[1:] if descriptor.startswith("L") else descriptor
    if body.endswith(";"):
        body = body[:-1]
    parts = [part for part in body.split("/") if part]
    if len(parts) <= 1:
        return body
    return "/".join(parts[: min(depth, len(parts) - 1)])


#: Leaf names that are build glue rather than a feature.
_GLUE_LEAF_NAMES = frozenset({"R", "BuildConfig", "Manifest"})


def looks_feature_bearing(descriptor: str) -> bool:
    """Would a human reading a feature report learn anything from this type name?

    Filters the added stable types down to the ones worth showing: under
    ``Lcom/instagram/`` (``com/facebook`` and ``androidx`` are infrastructure),
    and not an inner, anonymous or synthetic class — ``$1`` and
    ``$$ExternalSyntheticLambda0`` churn with every recompile and say nothing
    about what the app gained.
    """
    if not isinstance(descriptor, str) or not descriptor.startswith(FEATURE_TYPE_PREFIX):
        return False
    body = descriptor[1:-1] if descriptor.endswith(";") else descriptor[1:]
    if "$" in body:
        return False
    leaf = body.rsplit("/", 1)[-1]
    return bool(leaf) and leaf not in _GLUE_LEAF_NAMES


# --------------------------------------------------------------------------
# what DFInsta already blocks
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BlockedSurface:
    """The stable strings DFInsta's manifest already depends on, by family.

    Read from ``semantic_deps`` rather than hardcoded: the manifest is the
    source of truth for what this mod blocks, and a list copied into this module
    would be wrong the first time a hook is added or dropped.
    """

    literals: frozenset[str]
    families: frozenset[str]
    hooks_by_literal: Mapping[str, tuple[str, ...]]
    hooks_by_family: Mapping[str, tuple[str, ...]]
    source: str = ""
    #: Hooks excluded because they are no longer active. Recorded rather than
    #: dropped silently, so "why is this family not listed?" has an answer.
    inactive_hooks: tuple[str, ...] = ()

    @classmethod
    def from_hooks(cls, hooks: Iterable[Mapping[str, Any]], source: str = "") -> BlockedSurface:
        literal_hooks: dict[str, list[str]] = {}
        family_hooks: dict[str, list[str]] = {}
        inactive: list[str] = []
        for entry in hooks:
            if not isinstance(entry, Mapping):
                raise SurfaceDiffError(f"manifest hook entry is a {type(entry).__name__}, expected an object")
            hook_id = str(entry.get("hook_id", "<unnamed>"))
            # A dropped hook does not block anything, so counting its deps would
            # report a family as covered when nothing covers it any more.
            if str(entry.get("status", "active")) != "active":
                inactive.append(hook_id)
                continue
            deps = entry.get("semantic_deps") or ()
            if isinstance(deps, str) or not isinstance(deps, (list, tuple)):
                # A bare string iterates one character at a time and quietly
                # registers nonsense families rather than failing.
                raise SurfaceDiffError(f"{hook_id}: semantic_deps must be a list of strings")
            for dep in deps:
                if not isinstance(dep, str):
                    raise SurfaceDiffError(f"{hook_id}: semantic_deps must be strings")
                key = normalise_literal(dep)
                if key:
                    literal_hooks.setdefault(key, []).append(hook_id)
                family = endpoint_family(dep)
                if family != UNKNOWN_FAMILY:
                    family_hooks.setdefault(family, []).append(hook_id)
        return cls(
            literals=frozenset(literal_hooks),
            families=frozenset(family_hooks),
            hooks_by_literal={key: tuple(sorted(set(value))) for key, value in sorted(literal_hooks.items())},
            hooks_by_family={key: tuple(sorted(set(value))) for key, value in sorted(family_hooks.items())},
            source=source,
            inactive_hooks=tuple(sorted(set(inactive))),
        )

    @classmethod
    def from_manifest(cls, path: Path | str) -> BlockedSurface:
        """Read ``semantic_deps`` out of the hook manifest.

        Deliberately a lighter read than
        :func:`dfinsta_pipeline.hook_manifest.load_manifest`: this stage needs
        only the declared stable strings, and requiring every anchor, payload
        and marker to be valid would make feature *discovery* fail for a reason
        that has nothing to do with discovery — while discovery is exactly what
        tells you the manifest needs editing.
        """
        path = Path(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError as error:
            raise SurfaceDiffError(f"cannot read manifest {path}: {error}") from error
        except json.JSONDecodeError as error:
            raise SurfaceDiffError(f"manifest {path} is malformed: {error}") from error
        if not isinstance(data, dict):
            raise SurfaceDiffError(f"manifest {path} holds a {type(data).__name__}, expected an object")
        if data.get("schema_version") != SCHEMA_VERSION:
            raise SurfaceDiffError(
                f"manifest {path} has schema_version {data.get('schema_version')!r}, "
                f"expected {SCHEMA_VERSION}"
            )
        hooks = data.get("hooks")
        if not isinstance(hooks, list):
            raise SurfaceDiffError(f"manifest {path} has no hooks list")
        return cls.from_hooks(hooks, source=str(path))

    # ---------------------------------------------------------------- lookup

    def _match(self, literal: str) -> tuple[str, tuple[str, ...]]:
        key = normalise_literal(literal)
        hooks = self.hooks_by_literal.get(key)
        if hooks:
            return "literal", hooks
        family = endpoint_family(literal)
        if family != UNKNOWN_FAMILY:
            hooks = self.hooks_by_family.get(family)
            if hooks:
                return "family", hooks
        return "", ()

    def matches(self, literal: str) -> bool:
        return bool(self._match(literal)[1])

    def match_kind(self, literal: str) -> str:
        """``"literal"`` for the same endpoint, ``"family"`` for the same area, ``""`` for neither."""
        return self._match(literal)[0]

    def hooks_for(self, literal: str) -> tuple[str, ...]:
        return self._match(literal)[1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "families": sorted(self.families),
            "literals": sorted(self.literals),
            "hooks_by_family": {key: list(value) for key, value in self.hooks_by_family.items()},
            "inactive_hooks": list(self.inactive_hooks),
        }


# --------------------------------------------------------------------------
# one version's surface
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SurfaceSnapshot:
    """One version's stable-string surface — everything the diff is allowed to see.

    ``api_paths`` maps a literal to the descriptors that carry it. Those
    descriptors are per-version and are used ONLY for within-version questions
    (how many classes hold this literal, which literals share a class). They are
    never compared across snapshots and never serialised.

    Resource **names** live in ``resources``; resource **ids** live in a
    separate mapping the diff never reads. That separation is the guard: 99.1%
    of shared drawable names were renumbered between 430 and 439, so an id-keyed
    diff reports almost every drawable as changed. The ids are kept only so
    :meth:`resource_id_stability` can re-measure that instead of citing it.
    """

    label: str
    decode_path: str
    content_hash: str
    api_paths: Mapping[str, tuple[str, ...]]
    stable_types: frozenset[str]
    resources: Mapping[str, frozenset[str]]
    resource_types: tuple[str, ...]
    resource_ids: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    # ------------------------------------------------------------------ load

    @classmethod
    def load(cls, index_dir: Path | str) -> SurfaceSnapshot:
        """Read one index directory.

        Validation is delegated to :meth:`HookIndex.load` so a broken index
        fails the same way here as everywhere else in the package
        (:class:`IndexUnusable`, which the driver already handles). The surface
        file is then read a second time because the reader answers point queries
        and exposes no iteration; the 63 MB structural file is never touched by
        either.
        """
        index_dir = Path(index_dir)
        index = HookIndex.load(index_dir)
        header = index.header
        try:
            surface = json.loads((index_dir / API_SURFACE_FILENAME).read_text(encoding="utf-8"))
        except OSError as error:  # pragma: no cover - load() already opened it
            raise IndexUnusable(f"cannot read {index_dir / API_SURFACE_FILENAME}: {error}") from error
        return cls.from_surface(surface, header, label=str(header.get("decode_name") or index_dir.name))

    @classmethod
    def from_surface(
        cls,
        surface: Mapping[str, Any],
        header: Mapping[str, Any],
        label: str = "",
    ) -> SurfaceSnapshot:
        """Build from an already-parsed ``api_surface.json`` plus its header."""
        raw_paths = surface.get("api_paths") or {}
        raw_resources = surface.get("resources") or {}
        # The header is the authority on which resource types were indexed, not
        # the data. `hook_index.resource_id` refuses an unindexed type even when
        # the surface happens to carry it, because "not indexed" and "not
        # present" are different answers; ignoring that data here keeps the two
        # readers from disagreeing about what the index knows.
        resource_types = tuple(header.get("resource_types_indexed") or ())
        return cls(
            label=label or str(header.get("decode_name") or ""),
            decode_path=str(header.get("decode_path") or ""),
            content_hash=str(header.get("content_hash") or ""),
            api_paths={literal: tuple(hosts) for literal, hosts in raw_paths.items()},
            stable_types=frozenset(surface.get("stable_types") or ()),
            resources={
                resource_type: frozenset(raw_resources.get(resource_type) or ())
                for resource_type in resource_types
            },
            resource_types=resource_types,
            resource_ids={
                resource_type: dict(raw_resources.get(resource_type) or {})
                for resource_type in resource_types
            },
        )

    # --------------------------------------------------------------- queries

    @cached_property
    def _by_class(self) -> Mapping[str, frozenset[str]]:
        """Descriptor -> the literals it carries. Within THIS version only."""
        inverted: dict[str, set[str]] = {}
        for literal, hosts in self.api_paths.items():
            for host in hosts:
                inverted.setdefault(host, set()).add(literal)
        return {host: frozenset(literals) for host, literals in inverted.items()}

    def descriptors_with_literal(self, literal: str) -> tuple[str, ...]:
        return tuple(self.api_paths.get(literal, ()))

    def class_count(self, literal: str) -> int:
        """How many classes carry *literal* here. The only co-location number that travels."""
        return len(self.api_paths.get(literal, ()))

    def literals_in(self, descriptor: str) -> frozenset[str]:
        return self._by_class.get(descriptor, frozenset())

    def resource_names(self, resource_type: str) -> frozenset[str]:
        """Names of an indexed resource type.

        Raises for a type this index did not index, mirroring
        :meth:`HookIndex.resource_id`: an empty set would let "this index cannot
        answer" pass as "the version has none", and at diff scale that reads as
        the app deleting every drawable it has.
        """
        if resource_type not in self.resource_types:
            raise IndexUnusable(
                f"resource type {resource_type!r} is not indexed in {self.label or self.decode_path!r} "
                f"(indexed: {', '.join(self.resource_types) or 'none'})"
            )
        return self.resources.get(resource_type, frozenset())

    def resource_id_stability(self, resource_type: str, other: SurfaceSnapshot) -> float | None:
        """Fraction of names shared with *other* that also kept their hex id.

        This is the ONLY use of resource ids in this module, and it exists to
        keep "99.1% of drawables are renumbered" a measurement rather than
        folklore. It is a diagnostic, never an input to the diff.
        """
        mine = self.resource_ids.get(resource_type, {})
        theirs = other.resource_ids.get(resource_type, {})
        shared = set(mine) & set(theirs)
        if not shared:
            return None
        return sum(1 for name in shared if mine[name] == theirs[name]) / len(shared)

    def counts(self) -> dict[str, Any]:
        return {
            "api_paths": len(self.api_paths),
            "stable_types": len(self.stable_types),
            "resources": {
                resource_type: len(self.resources.get(resource_type, ()))
                for resource_type in self.resource_types
            },
        }

    def to_dict(self) -> dict[str, Any]:
        """Identity and sizes only. The contents are the diff's job, not the snapshot's."""
        return {
            "label": self.label,
            "decode_path": self.decode_path,
            "content_hash": self.content_hash,
            "resource_types_indexed": list(self.resource_types),
            "counts": self.counts(),
        }


# --------------------------------------------------------------------------
# diff parts
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CategoryDiff:
    """What one category of stable string gained and lost, and how much survived."""

    category: str
    added: tuple[str, ...]
    removed: tuple[str, ...]
    baseline_count: int
    target_count: int
    shared_count: int

    @classmethod
    def between(cls, category: str, baseline: Iterable[str], target: Iterable[str]) -> CategoryDiff:
        before, after = set(baseline), set(target)
        return cls(
            category=category,
            added=tuple(sorted(after - before)),
            removed=tuple(sorted(before - after)),
            baseline_count=len(before),
            target_count=len(after),
            shared_count=len(before & after),
        )

    @property
    def survival_rate(self) -> float | None:
        """Shared / baseline: the measured fraction of this layer that a version bump kept.

        ``None`` for an empty baseline, not 0.0. Nothing survived out of nothing
        is neither a total wipeout nor perfect survival, and reporting 0% would
        make an empty or unindexed baseline look like the app deleted the layer.
        """
        if self.baseline_count == 0:
            return None
        return self.shared_count / self.baseline_count

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)

    def to_dict(self) -> dict[str, Any]:
        rate = self.survival_rate
        return {
            "category": self.category,
            "baseline_count": self.baseline_count,
            "target_count": self.target_count,
            "shared_count": self.shared_count,
            "added_count": len(self.added),
            "removed_count": len(self.removed),
            "survival_rate": None if rate is None else round(rate, 6),
            "added": list(self.added),
            "removed": list(self.removed),
        }


@dataclass(frozen=True)
class PackageDelta:
    """One package prefix's net change in stable named types."""

    prefix: str
    baseline_count: int
    target_count: int
    added: int
    removed: int

    @property
    def churn(self) -> int:
        return self.added + self.removed

    def to_dict(self) -> dict[str, Any]:
        return {
            "prefix": self.prefix,
            "baseline_count": self.baseline_count,
            "target_count": self.target_count,
            "added": self.added,
            "removed": self.removed,
            "net": self.target_count - self.baseline_count,
        }


@dataclass(frozen=True)
class ColocationChange:
    """A literal present in both versions that moved between classes.

    This is the Shopping signal. An added/removed diff cannot see it — the
    literal is on both sides — yet going from one class to several is exactly
    what "dissolved into other endpoints" looks like from the index, and it is
    the difference between one blocking rule working and silently covering a
    fraction of the traffic.
    """

    literal: str
    family: str
    baseline_classes: int
    target_classes: int

    @property
    def delta(self) -> int:
        return self.target_classes - self.baseline_classes

    @property
    def direction(self) -> str:
        return "spread" if self.delta > 0 else "concentrated"

    def to_dict(self) -> dict[str, Any]:
        return {
            "literal": self.literal,
            "family": self.family,
            "baseline_classes": self.baseline_classes,
            "target_classes": self.target_classes,
            "delta": self.delta,
            "direction": self.direction,
        }


@dataclass(frozen=True)
class Candidate:
    """One changed literal, with the evidence a human needs — and no verdict.

    ``delivery_branch`` is the field that matters: it states the COST of
    blocking before anyone decides whether to. ``maps_to_blocked_family`` is
    tri-state on purpose — ``None`` means no manifest was supplied, which must
    not read as "DFInsta blocks nothing here".
    """

    literal: str
    family: str
    classes: int
    delivery_branch: str
    maps_to_blocked_family: bool | None
    blocked_by: tuple[str, ...]
    match_kind: str
    rides_with: tuple[str, ...]
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "literal": self.literal,
            "family": self.family,
            # A count, not the descriptors: obfuscated names are recycled, so a
            # count is comparable across versions and a name is a trap.
            "classes": self.classes,
            "delivery_branch": self.delivery_branch,
            "maps_to_blocked_family": self.maps_to_blocked_family,
            "blocked_by": list(self.blocked_by),
            "match_kind": self.match_kind,
            "rides_with": list(self.rides_with),
            "rationale": self.rationale,
        }


# --------------------------------------------------------------------------
# the diff
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SurfaceDiff:
    """Everything that changed between two versions on the layer that survives.

    JSON-serialisable through :meth:`to_dict`, which is what the human gate and
    the decision memory consume.
    """

    baseline: SurfaceSnapshot
    target: SurfaceSnapshot
    api_paths: CategoryDiff
    stable_types: CategoryDiff
    package_deltas: tuple[PackageDelta, ...]
    added_feature_types: tuple[str, ...]
    resources: Mapping[str, CategoryDiff]
    skipped_resource_types: Mapping[str, str]
    resource_id_stability: Mapping[str, float | None]
    colocation_changes: tuple[ColocationChange, ...]
    blocked: BlockedSurface | None = None

    @cached_property
    def candidates(self) -> tuple[Candidate, ...]:
        """Every added API-path literal, classified. The feature-candidate list."""
        return tuple(classify_candidate(literal, self) for literal in self.api_paths.added)

    @property
    def empty(self) -> bool:
        """True when the two versions are indistinguishable on this layer.

        A category that could not be compared makes this False. "Nothing
        changed" and "nothing changed among the things we could look at" are
        different claims, and only the first one is safe to skip a gate on.
        """
        return not (
            self.api_paths.changed
            or self.stable_types.changed
            or self.colocation_changes
            or self.skipped_resource_types
            or any(diff.changed for diff in self.resources.values())
        )

    def survival_rates(self) -> dict[str, float | None]:
        """Flat view of every measured rate, so the percentages stay recomputed."""
        rates: dict[str, float | None] = {
            "api_paths": self.api_paths.survival_rate,
            "stable_types": self.stable_types.survival_rate,
        }
        for resource_type, diff in self.resources.items():
            rates[f"resources.{resource_type}"] = diff.survival_rate
        return rates

    def candidates_by_family(self) -> dict[str, tuple[Candidate, ...]]:
        grouped: dict[str, list[Candidate]] = {}
        for candidate in self.candidates:
            grouped.setdefault(candidate.family, []).append(candidate)
        return {family: tuple(items) for family, items in sorted(grouped.items())}

    def branch_counts(self) -> dict[str, int]:
        counts = {BRANCH_ENDPOINT: 0, BRANCH_INLINE: 0, BRANCH_UNKNOWN: 0}
        for candidate in self.candidates:
            counts[candidate.delivery_branch] = counts.get(candidate.delivery_branch, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        rates = self.survival_rates()
        return {
            "kind": REPORT_KIND,
            "schema_version": SCHEMA_VERSION,
            "note": STABLE_LAYER_NOTE,
            "baseline": self.baseline.to_dict(),
            "target": self.target.to_dict(),
            "survival_rates": {
                key: None if value is None else round(value, 6) for key, value in rates.items()
            },
            "api_paths": self.api_paths.to_dict(),
            "stable_types": self.stable_types.to_dict(),
            "stable_type_packages": [delta.to_dict() for delta in self.package_deltas],
            "added_feature_types": list(self.added_feature_types),
            "resources": {
                resource_type: diff.to_dict() for resource_type, diff in self.resources.items()
            },
            "skipped_resource_types": dict(self.skipped_resource_types),
            "resource_id_stability": {
                key: None if value is None else round(value, 6)
                for key, value in self.resource_id_stability.items()
            },
            "colocation_changes": [change.to_dict() for change in self.colocation_changes],
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "branch_counts": self.branch_counts(),
            "blocked": None if self.blocked is None else self.blocked.to_dict(),
        }


def _as_snapshot(value: SurfaceSnapshot | Path | str) -> SurfaceSnapshot:
    """Accept a snapshot or an index directory; refuse anything that cannot iterate.

    A :class:`HookIndex` is rejected explicitly rather than duck-typed: it
    answers point queries and cannot enumerate a surface, so it would silently
    diff nothing at all.
    """
    if isinstance(value, SurfaceSnapshot):
        return value
    if isinstance(value, (str, Path)):
        return SurfaceSnapshot.load(value)
    if isinstance(value, HookIndex):
        raise TypeError(
            "HookIndex answers point queries and cannot enumerate a surface; "
            "pass the index directory or a SurfaceSnapshot"
        )
    raise TypeError(f"expected a SurfaceSnapshot or an index directory, got {type(value).__name__}")


def diff_surfaces(
    baseline_index: SurfaceSnapshot | Path | str,
    target_index: SurfaceSnapshot | Path | str,
    blocked: BlockedSurface | None = None,
) -> SurfaceDiff:
    """Diff two versions' API surfaces. Nothing here compares a class name.

    *baseline_index* and *target_index* are index directories (what the CLI has)
    or already-loaded snapshots (what tests and later stages have).
    """
    baseline = _as_snapshot(baseline_index)
    target = _as_snapshot(target_index)

    api_paths = CategoryDiff.between("api_paths", baseline.api_paths, target.api_paths)
    stable_types = CategoryDiff.between("stable_types", baseline.stable_types, target.stable_types)

    package_deltas = _package_deltas(baseline.stable_types, target.stable_types)
    added_feature_types = tuple(
        descriptor for descriptor in stable_types.added if looks_feature_bearing(descriptor)
    )

    resources: dict[str, CategoryDiff] = {}
    skipped: dict[str, str] = {}
    stability: dict[str, float | None] = {}
    for resource_type in sorted(set(baseline.resource_types) | set(target.resource_types)):
        in_baseline = resource_type in baseline.resource_types
        in_target = resource_type in target.resource_types
        if not (in_baseline and in_target):
            # Diffing an indexed side against an unindexed one would report
            # every name as added or removed. "Not indexed" is not "not
            # present", and at this scale the difference is a fabricated report.
            missing = baseline.label if not in_baseline else target.label
            skipped[resource_type] = (
                f"indexed in only one version (missing from {missing or 'the other index'}); "
                "an unindexed type is not an empty one"
            )
            continue
        resources[resource_type] = CategoryDiff.between(
            f"resources.{resource_type}",
            baseline.resource_names(resource_type),
            target.resource_names(resource_type),
        )
        stability[resource_type] = baseline.resource_id_stability(resource_type, target)

    colocation = _colocation_changes(baseline, target)

    return SurfaceDiff(
        baseline=baseline,
        target=target,
        api_paths=api_paths,
        stable_types=stable_types,
        package_deltas=package_deltas,
        added_feature_types=added_feature_types,
        resources=resources,
        skipped_resource_types=skipped,
        resource_id_stability=stability,
        colocation_changes=colocation,
        blocked=blocked,
    )


def _package_deltas(
    baseline_types: Iterable[str], target_types: Iterable[str]
) -> tuple[PackageDelta, ...]:
    before, after = set(baseline_types), set(target_types)
    prefixes: dict[str, dict[str, int]] = {}
    for descriptor in before | after:
        prefix = package_prefix(descriptor)
        bucket = prefixes.setdefault(
            prefix, {"baseline": 0, "target": 0, "added": 0, "removed": 0}
        )
        in_before = descriptor in before
        in_after = descriptor in after
        bucket["baseline"] += int(in_before)
        bucket["target"] += int(in_after)
        bucket["added"] += int(in_after and not in_before)
        bucket["removed"] += int(in_before and not in_after)
    deltas = [
        PackageDelta(
            prefix=prefix,
            baseline_count=bucket["baseline"],
            target_count=bucket["target"],
            added=bucket["added"],
            removed=bucket["removed"],
        )
        for prefix, bucket in prefixes.items()
        if bucket["added"] or bucket["removed"]
    ]
    # Biggest movers first: the report is read from the top and a package that
    # gained 40 types matters more than one that gained 1.
    deltas.sort(key=lambda delta: (-delta.churn, delta.prefix))
    return tuple(deltas)


def _colocation_changes(
    baseline: SurfaceSnapshot, target: SurfaceSnapshot
) -> tuple[ColocationChange, ...]:
    """Literals in both versions whose class COUNT moved.

    Counts, never descriptor sets. Obfuscated names churn every release, so
    comparing the sets would flag essentially every literal and report nothing.
    """
    changes = [
        ColocationChange(
            literal=literal,
            family=endpoint_family(literal),
            baseline_classes=baseline.class_count(literal),
            target_classes=target.class_count(literal),
        )
        for literal in set(baseline.api_paths) & set(target.api_paths)
        if baseline.class_count(literal) != target.class_count(literal)
    ]
    changes.sort(key=lambda change: (-abs(change.delta), change.literal))
    return tuple(changes)


def classify_candidate(literal: str, diff: SurfaceDiff) -> Candidate:
    """Describe one changed literal: family, spread, prior coverage, and cost to block.

    The cost is ``delivery_branch``:

    ``A_endpoint``
        Its own request path. One rule in ``throwIfBlocked`` and it is done.
    ``B_inline``
        Never appears in a class that does not already carry a blocked path, so
        it most likely rides inside another response — a fragile rewriter, not a
        URL rule.
    ``C_unknown``
        Neither could be shown. A human has to look.

    No verdict is produced. Whether the feature is worth blocking is a later
    stage's question and a human's decision.
    """
    family = endpoint_family(literal)
    target = diff.target
    hosts = target.descriptors_with_literal(literal)
    blocked = diff.blocked

    if blocked is None:
        maps: bool | None = None
        blocked_by: tuple[str, ...] = ()
        match_kind = ""
    else:
        match_kind = blocked.match_kind(literal)
        blocked_by = blocked.hooks_for(literal)
        maps = bool(blocked_by)

    branch, rides, why = _delivery_branch(literal, target, blocked, hosts)
    return Candidate(
        literal=literal,
        family=family,
        classes=len(hosts),
        delivery_branch=branch,
        maps_to_blocked_family=maps,
        blocked_by=blocked_by,
        match_kind=match_kind,
        rides_with=rides,
        rationale=why,
    )


def _delivery_branch(
    literal: str,
    target: SurfaceSnapshot,
    blocked: BlockedSurface | None,
    hosts: tuple[str, ...],
) -> tuple[str, tuple[str, ...], str]:
    if is_endpoint_path(literal):
        return (
            BRANCH_ENDPOINT,
            (),
            "its own API request path: one rule in throwIfBlocked blocks it",
        )
    if blocked is None:
        return (
            BRANCH_UNKNOWN,
            (),
            "not an API request path, and no manifest was supplied, so "
            "co-location with an already-blocked path could not be assessed",
        )
    if not hosts:
        return (
            BRANCH_UNKNOWN,
            (),
            "not an API request path and absent from the target version",
        )
    rides: set[str] = set()
    for host in hosts:
        # The literal cannot be its own evidence of riding inside something else.
        here = {
            other
            for other in target.literals_in(host)
            if other != literal and blocked.matches(other)
        }
        if not here:
            return (
                BRANCH_UNKNOWN,
                (),
                f"not an API request path; appears in {len(hosts)} class(es), at least "
                "one of which carries no already-blocked path",
            )
        rides |= here
    evidence = ", ".join(sorted(rides)[:3])
    return (
        BRANCH_INLINE,
        tuple(sorted(rides)),
        f"never appears without an already-blocked path in the same class "
        f"({evidence}): it most likely rides inside that response",
    )


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------


def _percent(rate: float | None) -> str:
    return f"{'n/a':>6s}" if rate is None else f"{rate * 100:5.1f}%"


def summary_lines(diff: SurfaceDiff, top: int = 12) -> list[str]:
    """Human-readable summary. Returned rather than printed so it is testable."""
    lines = [
        f"surface diff  {diff.baseline.label or '?'} -> {diff.target.label or '?'}",
        STABLE_LAYER_NOTE,
        "",
        f"  {'category':22s} {'baseline':>9s} {'target':>9s} {'added':>7s} {'removed':>8s}  survival",
    ]
    rows: list[tuple[str, CategoryDiff]] = [
        ("api paths", diff.api_paths),
        ("stable named types", diff.stable_types),
    ]
    rows.extend((f"resource {name}", value) for name, value in sorted(diff.resources.items()))
    for name, category in rows:
        lines.append(
            f"  {name:22s} {category.baseline_count:9d} {category.target_count:9d} "
            f"{len(category.added):+7d} {-len(category.removed):+8d}  "
            f"{_percent(category.survival_rate)}"
        )
    for resource_type, stability in sorted(diff.resource_id_stability.items()):
        lines.append(
            f"  resource {resource_type}: {_percent(stability)} of shared names kept their hex id "
            "-- which is why resources are diffed by NAME"
        )
    for resource_type, reason in sorted(diff.skipped_resource_types.items()):
        lines.append(f"  resource {resource_type}: SKIPPED -- {reason}")

    lines.append("")
    lines.append(f"  stable-type packages that moved: {len(diff.package_deltas)}")
    for delta in diff.package_deltas[:top]:
        lines.append(
            f"    {delta.prefix:44s} +{delta.added:<5d} -{delta.removed:<5d} "
            f"({delta.baseline_count} -> {delta.target_count})"
        )
    if diff.added_feature_types:
        lines.append(f"  added feature-bearing types under com/instagram/: {len(diff.added_feature_types)}")
        for descriptor in diff.added_feature_types[:top]:
            lines.append(f"    {descriptor}")

    lines.append("")
    lines.append(f"  literals whose co-location changed: {len(diff.colocation_changes)}")
    for change in diff.colocation_changes[:top]:
        lines.append(
            f"    {change.literal:56s} {change.baseline_classes} -> {change.target_classes} "
            f"{change.direction}"
        )

    lines.append("")
    branches = diff.branch_counts()
    lines.append(
        f"  candidates (added API-path literals): {len(diff.candidates)}  "
        + "  ".join(f"{branch}={count}" for branch, count in sorted(branches.items()))
    )
    if diff.blocked is None:
        lines.append(
            "  no manifest supplied: 'already blocked by DFInsta' was NOT assessed "
            "(that is not the same as 'nothing is blocked')"
        )
    for family, candidates in diff.candidates_by_family().items():
        lines.append(f"    [{family}] {len(candidates)}")
        for candidate in candidates[:top]:
            mark = "blocked-family" if candidate.maps_to_blocked_family else "new area"
            if candidate.maps_to_blocked_family is None:
                mark = "unassessed"
            lines.append(
                f"      {candidate.delivery_branch:11s} {candidate.literal:52s} "
                f"{candidate.classes:3d} class(es)  {mark}"
            )
    lines.append("")
    lines.append("  This is evidence, not a verdict. Addictiveness is decided at a human gate.")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Diff two Instagram versions on the stable-string layer (stage 3).",
        epilog=STABLE_LAYER_NOTE,
    )
    parser.add_argument("baseline", type=Path, help="baseline index directory")
    parser.add_argument("target", type=Path, help="target index directory")
    parser.add_argument(
        "--manifest",
        type=Path,
        help=f"hook manifest whose semantic_deps say what is already blocked "
        f"(default {DEFAULT_MANIFEST} when it exists)",
    )
    parser.add_argument("--json", type=Path, help="write the full report here")
    args = parser.parse_args(argv)

    manifest = args.manifest
    if manifest is None:
        # An explicitly typed path must never be silently ignored; the repo
        # default is a convenience and may legitimately be absent.
        manifest = DEFAULT_MANIFEST if DEFAULT_MANIFEST.is_file() else None
        if manifest is None:
            print(
                f"warning: {DEFAULT_MANIFEST} not found; 'already blocked' will not be assessed",
                file=sys.stderr,
            )
    try:
        blocked = None if manifest is None else BlockedSurface.from_manifest(manifest)
    except SurfaceDiffError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    try:
        diff = diff_surfaces(args.baseline, args.target, blocked)
    except IndexUnusable as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    for line in summary_lines(diff):
        print(line)

    if args.json:
        args.json.write_text(
            json.dumps(diff.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

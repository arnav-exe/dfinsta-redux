"""Read one version's Index, so host search never rescans 181,000 smali files.

``tools/indexer/build_index.py`` writes three files per decode; this is the
reader for them. It answers the three questions the Resolve stage asks:

    which classes contain this API-path literal?      api_surface.json
    where on disk does this descriptor live?          structural.jsonl
    what hex id does this drawable name have here?    api_surface.json

===============================================================================
  ONE INDEX BELONGS TO ONE DECODE.  NEVER CARRY A DESCRIPTOR ACROSS VERSIONS.
===============================================================================

Obfuscated names are recycled, not merely scrambled: ``LX/05t2;`` names a
1990-line Reels request builder in Instagram 430 and an unrelated 596-line class
in 439. A 430 descriptor looked up in a 439 index therefore returns a confident,
silently wrong answer rather than a miss. :meth:`HookIndex.for_decode` binds a
reader to the decode its header names and refuses any other, which turns that
mistake from a wrong answer into an exception.

**What the index is trusted for.** It is a *search accelerator*, not evidence.
Every descriptor it proposes is re-verified by reading that class out of the
decode and matching the anchor against it, so a stale structural index degrades
to "the host was not found" rather than "the wrong host was patched". The one
exception is :meth:`resource_id`: a resource lookup is not re-derived from the
decode by anything downstream, so it is the single answer here that a stale
index could get wrong undetected. Run ``build_index.py --check`` before trusting
one of those against a decode that may have moved.

Resource ids are also **per-version**. Of the 11,737 drawable names present in
both 430 and 439, only 103 keep their hex id — 99.1% are renumbered. Anchor on
the drawable *name* and re-resolve the id here for the target version. String
ids cannot be resolved at all under sparse resource encoding, so asking for an
unindexed resource type raises instead of returning ``None``: "not indexed" and
"not present" are different answers and must not be confused.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

HEADER_FILENAME = "header.json"
STRUCTURAL_FILENAME = "structural.jsonl"
API_SURFACE_FILENAME = "api_surface.json"

SCHEMA_VERSION = 1


class IndexError_(Exception):
    """Raised when an index is missing, malformed, or bound to another decode."""


# The name the rest of the package imports. Shadowing the builtin `IndexError`
# would be a trap for anyone catching it, so the class is defined under a
# private name and exported under an explicit one.
IndexUnusable = IndexError_


@dataclass(frozen=True)
class ClassRow:
    """One class as the structural index recorded it."""

    descriptor: str
    path: str
    tree: str
    super_descriptor: str | None
    interfaces: tuple[str, ...]
    methods: tuple[str, ...]
    obfuscated: bool

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ClassRow:
        return cls(
            descriptor=data["descriptor"],
            path=data["path"],
            tree=data["tree"],
            super_descriptor=data.get("super"),
            interfaces=tuple(data.get("interfaces", ())),
            methods=tuple(data.get("methods", ())),
            obfuscated=bool(data.get("obfuscated", False)),
        )


class HookIndex:
    """A read-only view over one decode's index.

    Construct with :meth:`load` when the decode is not at hand, or
    :meth:`for_decode` — which is what the Resolve stage uses — to bind the
    reader to a specific decode and reject a mismatched one up front.
    """

    def __init__(self, index_dir: Path, header: Mapping[str, Any], surface: Mapping[str, Any]):
        self._dir = Path(index_dir)
        self._header = dict(header)
        self._api_paths: Mapping[str, Sequence[str]] = surface.get("api_paths", {})
        self._resources: Mapping[str, Mapping[str, str]] = surface.get("resources", {})
        self._stable_types: Mapping[str, str] = surface.get("stable_types", {})
        self._resource_types = tuple(header.get("resource_types_indexed", ()))
        self._paths: dict[str, str] | None = None
        self._rows: dict[str, ClassRow] | None = None

    # ------------------------------------------------------------------ load

    @classmethod
    def load(cls, index_dir: Path | str) -> HookIndex:
        index_dir = Path(index_dir)
        try:
            header = json.loads((index_dir / HEADER_FILENAME).read_text(encoding="utf-8"))
            surface = json.loads(
                (index_dir / API_SURFACE_FILENAME).read_text(encoding="utf-8")
            )
        except (FileNotFoundError, NotADirectoryError) as error:
            # NotADirectoryError is the `--index path/to/a/file` slip. It has to
            # land as IndexUnusable too, or resolve.main()'s handler misses it and
            # the stage dies with a traceback instead of exit 2.
            raise IndexUnusable(f"index at {index_dir} is incomplete: {error}") from error
        except json.JSONDecodeError as error:
            raise IndexUnusable(f"index at {index_dir} is malformed: {error}") from error
        for name, document in ((HEADER_FILENAME, header), (API_SURFACE_FILENAME, surface)):
            if not isinstance(document, dict):
                # Valid JSON of the wrong shape is still malformed. Without this the
                # first `.get` raises AttributeError past every handler.
                raise IndexUnusable(
                    f"index at {index_dir}: {name} holds a "
                    f"{type(document).__name__}, expected a JSON object"
                )
        version = header.get("schema_version")
        # `1.0 == 1` is True, so a value comparison alone accepts a JSON float.
        if not isinstance(version, int) or isinstance(version, bool) or version != SCHEMA_VERSION:
            raise IndexUnusable(
                f"index at {index_dir} has schema_version {version!r}, "
                f"expected the integer {SCHEMA_VERSION}"
            )
        if not (index_dir / STRUCTURAL_FILENAME).is_file():
            raise IndexUnusable(f"index at {index_dir} has no {STRUCTURAL_FILENAME}")
        return cls(index_dir, header, surface)

    @classmethod
    def for_decode(cls, index_dir: Path | str, decode: Path | str) -> HookIndex:
        """Load, and refuse an index built from a different decode.

        This is the guard against the recycled-descriptor failure, so it is a
        hard error rather than a warning.
        """
        index = cls.load(index_dir)
        index.assert_matches(decode)
        return index

    def assert_matches(self, decode: Path | str) -> None:
        wanted = str(Path(decode).resolve())
        built_from = self._header.get("decode_path")
        if built_from != wanted:
            raise IndexUnusable(
                f"index at {self._dir} was built from {built_from!r} but is being used "
                f"against {wanted!r}. Obfuscated descriptors are recycled across "
                "Instagram versions, so a cross-decode lookup returns a wrong answer, "
                "not a miss."
            )

    # -------------------------------------------------------------- metadata

    @property
    def decode_path(self) -> str:
        # `.get(key, "")` returns the default only for a MISSING key, so an explicit
        # JSON null would otherwise hand a caller None from a `-> str` property.
        return self._header.get("decode_path") or ""

    @property
    def content_hash(self) -> str:
        return self._header.get("content_hash") or ""

    @property
    def header(self) -> Mapping[str, Any]:
        # Deep, not shallow: a shallow copy still shares the nested `counts` dict,
        # so a caller could mutate the reader's own header through it.
        return deepcopy(self._header)

    @property
    def resource_types(self) -> tuple[str, ...]:
        return self._resource_types

    # ------------------------------------------------------------- structure

    def _iter_rows(self):
        """Yield parsed structural rows, turning any parse failure into IndexUnusable.

        The structural file is streamed together from per-process shards with no
        atomic rename, so an interrupted build leaves a truncated last line. That
        must surface as IndexUnusable naming the line — the Resolve stage handles
        that and exits cleanly, whereas a raw JSONDecodeError escapes every
        handler and kills the run with a traceback.
        """
        path = self._dir / STRUCTURAL_FILENAME
        try:
            handle = open(path, "r", encoding="utf-8")
        except OSError as error:
            raise IndexUnusable(f"cannot read {path}: {error}") from error
        with handle:
            handle.readline()  # header line; never a class row
            for number, line in enumerate(handle, start=2):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as error:
                    raise IndexUnusable(
                        f"{path}:{number} is malformed: {error}. An index whose "
                        "structural file was truncated mid-write must be rebuilt."
                    ) from error
                if not isinstance(row, dict) or "descriptor" not in row or "path" not in row:
                    raise IndexUnusable(
                        f"{path}:{number} is not a class row: missing descriptor or path"
                    )
                yield row

    def _load_paths(self) -> dict[str, str]:
        """Descriptor -> decode-relative path, read once and cached.

        The structural index is ~63 MB of JSONL. Only the path is needed for
        host search, so the full rows are parsed separately and only on demand.
        """
        if self._paths is None:
            self._paths = {row["descriptor"]: row["path"] for row in self._iter_rows()}
        return self._paths

    def _load_rows(self) -> dict[str, ClassRow]:
        if self._rows is None:
            rows: dict[str, ClassRow] = {}
            for raw in self._iter_rows():
                row = ClassRow.from_dict(raw)
                rows[row.descriptor] = row
            self._rows = rows
            self._paths = {descriptor: row.path for descriptor, row in rows.items()}
        return self._rows

    def path_for(self, descriptor: str) -> str | None:
        """Decode-relative path of *descriptor*, or ``None`` if this version has no such class."""
        return self._load_paths().get(descriptor)

    def row_for(self, descriptor: str) -> ClassRow | None:
        return self._load_rows().get(descriptor)

    def has(self, descriptor: str) -> bool:
        return descriptor in self._load_paths()

    def class_count(self) -> int:
        return len(self._load_paths())

    # ------------------------------------------------------------- literals

    def descriptors_with_literal(self, literal: str) -> tuple[str, ...]:
        """Every class containing *literal* as an API-path string constant.

        Only literals that look like API paths are indexed — see
        ``looks_like_api_path`` in the builder — so an empty result can mean
        "no class has it" or "that string was never a candidate for indexing".
        Callers that need the difference must fall back to scanning the decode.
        """
        return tuple(self._api_paths.get(literal, ()))

    def descriptors_with_all_literals(self, literals: Iterable[str]) -> tuple[str, ...]:
        """Classes containing *every* literal in *literals*.

        Co-location is the fingerprint that disambiguates the Reels host. Each
        of ``clips/discover/``, ``clips/homecoming/`` and ``clips/discover/stream/``
        appears in 2-5 classes on its own — analytics maps and prefetch
        allowlists carry them too — but only the class that builds the outgoing
        request path carries all three. That was one class on 430 and one on 439.
        If a future version splits them the intersection empties and the caller
        escalates, which is the failure we want.
        """
        wanted = list(literals)
        if not wanted:
            return ()
        common: set[str] | None = None
        for literal in wanted:
            bucket = set(self._api_paths.get(literal, ()))
            common = bucket if common is None else (common & bucket)
            if not common:
                return ()
        return tuple(sorted(common or ()))

    def literal_is_indexed(self, literal: str) -> bool:
        return literal in self._api_paths

    @property
    def literal_count(self) -> int:
        return len(self._api_paths)

    # ------------------------------------------------------------- resources

    def resource_id(self, resource_type: str, name: str) -> str | None:
        """Hex id of a resource *name*, for THIS version only.

        Raises when *resource_type* was never indexed — notably ``string``,
        which sparse resource encoding makes unresolvable. Returning ``None``
        there would let "this index cannot answer" pass as "the app does not
        have it".
        """
        if resource_type not in self._resource_types:
            raise IndexUnusable(
                f"resource type {resource_type!r} is not indexed (indexed: "
                f"{', '.join(self._resource_types) or 'none'}). String ids in "
                "particular are unresolvable under sparse resource encoding, so "
                "there is no id to return and no answer to infer."
            )
        return self._resources.get(resource_type, {}).get(name)

    def stable_type_path(self, descriptor: str) -> str | None:
        """Path of a non-obfuscated class, without touching the structural index."""
        return self._stable_types.get(descriptor)

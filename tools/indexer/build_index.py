"""Stage 2 of the DFInsta port pipeline: build the per-version Index.

Turns one decoded APK into two lookup tables so later stages never rescan the
~181,000 smali files of a modern Instagram decode:

  A. Structural Index  (``structural.jsonl``)
     one row per class: descriptor, path relative to the decode, the
     ``smali_classesN`` tree it lives in, ``.super``, ``.implements`` and every
     method signature.

  B. API-Surface Index (``api_surface.json``)
     the things Instagram cannot easily scramble and which therefore survive a
     version bump verbatim: API-path string literals mapped to the descriptors
     that contain them, resource ids from ``res/values/public.xml`` by type,
     and every descriptor that is *not* obfuscated.

  Plus ``header.json`` -- decode path, content hash of exactly the inputs that
  were indexed, and the counts -- so a stale index is detectable
  (``--check`` recomputes the hash and compares).

===============================================================================
  THE INDEX IS PER-VERSION ONLY.  NEVER JOIN ON AN OBFUSCATED NAME ACROSS
  VERSIONS.
===============================================================================

Obfuscated names are not merely scrambled, they are *recycled*.  ``LX/05t2``
exists in both Instagram 430 and 439 and is a COMPLETELY DIFFERENT class in
each: 1990 lines carrying Reels endpoints in 430, 596 lines carrying none in
439.  Every obfuscated host moved between those two versions
(``LX/077K``->``LX/0DnT``, ``LX/06X7``->``LX/0Di2``, ``LX/05t2``->``LX/04tC``)
*and* the old names still resolve, to unrelated classes.  Looking a 430
descriptor up in a 439 index therefore returns a confident, silently wrong
answer.  Cross-version identity must be re-established from the API surface --
endpoint literals, resource names, stable ``Lcom/instagram/...`` types -- never
from a descriptor.  Each index carries the decode it was built from in its
header; refuse to use one against a different decode.

Second constraint, learned the same way: **string resource ids are not
resolvable** in these APKs.  Sparse resource encoding means the decode exposes
only ~555 of ~19,000 string entries, so an anchor pinned to a string id cannot
be trusted.  Drawable / id / layout ids *are* fully resolvable from
``res/values/public.xml`` and are what the API-surface index captures.  The
header records the per-type counts actually found in ``public.xml`` so the
shortfall stays visible rather than being rediscovered.

Usage
-----
    build_index.py <decode-dir> --out <index-dir> [--jobs N]
    build_index.py <decode-dir> --out <index-dir> --check

The decode directory is only ever read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

SCHEMA_VERSION = 1
GENERATOR = "tools/indexer/build_index.py"
HEADER_KIND = "dfinsta.index.header"
CLASS_KIND = "dfinsta.index.class"

PER_VERSION_WARNING = (
    "PER-VERSION INDEX. Obfuscated descriptors (LX/...) are recycled across "
    "Instagram versions and denote different classes in each; never use this "
    "index, or any descriptor from it, as a cross-version join key."
)
STRING_RESOURCE_NOTE = (
    "String resource ids are unresolvable under sparse resource encoding: "
    "public.xml exposes only a small fraction of the string table. Pin UI "
    "anchors by drawable id instead."
)

HEADER_FILENAME = "header.json"
STRUCTURAL_FILENAME = "structural.jsonl"
API_SURFACE_FILENAME = "api_surface.json"

DEFAULT_RESOURCE_TYPES = ("drawable", "id", "layout")
PUBLIC_XML_RELPATH = "res/values/public.xml"

#: Descriptors under this prefix are the obfuscated bucket (``smali*/X/...``).
OBFUSCATED_PREFIX = "LX/"

_DOT = 0x2E
_QUOTE = 0x22

# Characters an API-path literal may contain.  Deliberately excludes uppercase
# ASCII, which is what cheaply rejects type descriptors (``Ljava/lang/String;``)
# and method signatures (``getFoo()Ljava/lang/String;``) in a single pass.
_ALLOWED_PATH_BYTES = frozenset(b"abcdefghijklmnopqrstuvwxyz0123456789_-./%{}:$~")

# ``video/mp4`` is not an endpoint; ``video/refresh_resources/%s/`` is.  Only a
# bare two-segment ``type/subtype`` with a known top-level type is dropped.
_MIME_TOP_LEVEL = frozenset(
    (b"audio", b"video", b"image", b"application", b"text", b"font", b"model", b"multipart", b"message")
)

_MIN_PATH_LEN = 4
_MAX_PATH_LEN = 200
_MIN_SEGMENT_LEN = 3


# --------------------------------------------------------------------------
# decode layout
# --------------------------------------------------------------------------


def is_smali_tree(name: str) -> bool:
    """True for ``smali`` and ``smali_classes<N>`` directory names."""
    if name == "smali":
        return True
    if name.startswith("smali_classes"):
        suffix = name[len("smali_classes") :]
        return suffix.isdigit() and suffix != ""
    return False


def smali_tree_sort_key(name: str) -> tuple[int, int]:
    """Natural order: smali, smali_classes2, ..., smali_classes10, ... 20."""
    if name == "smali":
        return (0, 0)
    return (1, int(name[len("smali_classes") :]))


def list_smali_trees(decode: Path) -> list[str]:
    names = [entry.name for entry in os.scandir(decode) if entry.is_dir() and is_smali_tree(entry.name)]
    return sorted(names, key=smali_tree_sort_key)


def list_smali_files(decode: Path) -> list[str]:
    """Every ``*.smali`` path relative to *decode*, in deterministic order."""
    relpaths: list[str] = []
    for tree in list_smali_trees(decode):
        tree_root = decode / tree
        for dirpath, dirnames, filenames in os.walk(tree_root):
            dirnames.sort()
            base = os.path.relpath(dirpath, decode)
            for filename in sorted(filenames):
                if filename.endswith(".smali"):
                    relpaths.append(os.path.join(base, filename).replace(os.sep, "/"))
    return relpaths


def is_obfuscated(descriptor: str) -> bool:
    """``LX/04tC;`` -> True.  ``Lcom/instagram/api/tigon/TigonServiceLayer;`` -> False.

    Obfuscated descriptors are recycled between versions; stable ones are the
    only names that mean the same thing in two different decodes.
    """
    return descriptor.startswith(OBFUSCATED_PREFIX)


# --------------------------------------------------------------------------
# API-path literal classifier
# --------------------------------------------------------------------------


def looks_like_api_path(value: bytes | str) -> bool:
    """Heuristic: does this string literal look like an Instagram API path?

    Accepts ``clips/discover/``, ``feed/timeline/``,
    ``discover/topical_explore/``, ``video/refresh_resources/%s/``,
    ``text_feed/{post_id}/replies_in_ig/``.

    Rejects type descriptors, method signatures, anything with an uppercase
    letter, whitespace or an escape, and bare MIME types.
    """
    if isinstance(value, str):
        try:
            value = value.encode("utf-8")
        except UnicodeEncodeError:  # pragma: no cover - str is always encodable
            return False
    length = len(value)
    if length < _MIN_PATH_LEN or length > _MAX_PATH_LEN:
        return False
    if b"/" not in value:
        return False
    if not _ALLOWED_PATH_BYTES.issuperset(value):
        return False
    segments = value.split(b"/")
    if not any(len(segment) >= _MIN_SEGMENT_LEN for segment in segments):
        return False
    if len(segments) == 2 and segments[1] and segments[0] in _MIME_TOP_LEVEL:
        return False
    # A bare URI scheme ("https://", "content://", "ig://") is a prefix that
    # gets concatenated at runtime, not a path.  Anything with a real path
    # after the scheme has more than one non-empty segment and survives.
    non_empty = [segment for segment in segments if segment]
    if len(non_empty) == 1 and non_empty[0].endswith(b":"):
        return False
    return True


# --------------------------------------------------------------------------
# smali parsing
# --------------------------------------------------------------------------


def parse_smali(data: bytes, tolerant: bool = False) -> dict:
    """Single pass over one smali file.

    Returns ``{"descriptor", "super", "interfaces", "methods", "api_paths"}``.
    ``descriptor`` is ``None`` when the file carries no ``.class`` directive.

    The fast path assumes apktool's layout, where ``.class``/``.super``/
    ``.implements``/``.method``/``.field`` sit at column 0 and everything else
    is indented; that lets the hot loop dispatch on the first byte with no
    per-line strip.  ``tolerant=True`` re-runs with a strip per line and is used
    automatically as a fallback when the fast path found no ``.class``.
    """
    descriptor: str | None = None
    super_descriptor: str | None = None
    interfaces: list[str] = []
    methods: list[str] = []
    api_paths: set[str] = set()

    for line in data.split(b"\n"):
        if not line:
            continue
        if tolerant:
            line = line.strip()
            if not line:
                continue
        first = line[0]
        if first == _DOT:
            # Column-0 directive.  Dispatch on the second byte; ".end",
            # ".annotation", ".registers" and friends fall straight through.
            kind = line[1] if len(line) > 1 else 0
            if kind == 0x6D:  # m -> .method
                if line.startswith(b".method "):
                    parts = line.split()
                    if len(parts) > 1:
                        methods.append(parts[-1].decode("utf-8", "replace"))
                continue
            if kind == 0x66:  # f -> .field (may carry a string initialiser)
                pass  # fall through to the literal scan below
            elif kind == 0x73:  # s -> .super / .source
                if line.startswith(b".super "):
                    parts = line.split()
                    if len(parts) > 1:
                        super_descriptor = parts[-1].decode("utf-8", "replace")
                continue
            elif kind == 0x69:  # i -> .implements
                if line.startswith(b".implements "):
                    parts = line.split()
                    if len(parts) > 1:
                        interfaces.append(parts[-1].decode("utf-8", "replace"))
                continue
            elif kind == 0x63:  # c -> .class
                if line.startswith(b".class "):
                    parts = line.split()
                    if len(parts) > 1:
                        descriptor = parts[-1].decode("utf-8", "replace")
                continue
            else:
                continue
        open_quote = line.find(b'"')
        if open_quote == -1:
            continue
        close_quote = line.rfind(b'"')
        if close_quote <= open_quote:
            continue
        literal = line[open_quote + 1 : close_quote]
        if looks_like_api_path(literal):
            api_paths.add(literal.decode("ascii"))

    if descriptor is None and not tolerant:
        return parse_smali(data, tolerant=True)

    return {
        "descriptor": descriptor,
        "super": super_descriptor,
        "interfaces": interfaces,
        "methods": methods,
        "api_paths": api_paths,
    }


# --------------------------------------------------------------------------
# scanning
# --------------------------------------------------------------------------


def _scan_shard(job: tuple[str, list[str], str]) -> dict:
    """Scan a contiguous batch of files, writing their rows to a JSONL shard.

    Rows go straight to disk rather than back through IPC, so peak memory stays
    flat no matter how many classes the decode has.  Top-level so it can be
    sent to a ProcessPoolExecutor.
    """
    decode_str, relpaths, shard_path = job
    decode = Path(decode_str)

    digests: list[tuple[str, bytes]] = []
    api_paths: dict[str, set[str]] = {}
    stable_types: dict[str, str] = {}
    tree_counts: dict[str, int] = {}
    method_count = 0
    obfuscated_count = 0
    duplicate_descriptors: list[str] = []
    seen: set[str] = set()
    missing_class_directive: list[str] = []

    with open(shard_path, "w", encoding="utf-8") as handle:
        for relpath in relpaths:
            with open(decode / relpath, "rb") as source:
                data = source.read()
            digests.append((relpath, hashlib.sha256(data).digest()))

            parsed = parse_smali(data)
            descriptor = parsed["descriptor"]
            if descriptor is None:
                missing_class_directive.append(relpath)
                continue

            tree = relpath.split("/", 1)[0]
            tree_counts[tree] = tree_counts.get(tree, 0) + 1
            method_count += len(parsed["methods"])

            if descriptor in seen:
                duplicate_descriptors.append(descriptor)
            else:
                seen.add(descriptor)

            obfuscated = is_obfuscated(descriptor)
            if obfuscated:
                obfuscated_count += 1
            else:
                stable_types[descriptor] = relpath

            for path_literal in parsed["api_paths"]:
                bucket = api_paths.get(path_literal)
                if bucket is None:
                    api_paths[path_literal] = {descriptor}
                else:
                    bucket.add(descriptor)

            row = json.dumps(
                {
                    "kind": CLASS_KIND,
                    "descriptor": descriptor,
                    "path": relpath,
                    "tree": tree,
                    "super": parsed["super"],
                    "interfaces": parsed["interfaces"],
                    "methods": parsed["methods"],
                    "obfuscated": obfuscated,
                },
                separators=(",", ":"),
                ensure_ascii=False,
            )
            handle.write(row)
            handle.write("\n")

    return {
        "digests": digests,
        "api_paths": api_paths,
        "stable_types": stable_types,
        "tree_counts": tree_counts,
        "method_count": method_count,
        "obfuscated_count": obfuscated_count,
        "duplicate_descriptors": duplicate_descriptors,
        "missing_class_directive": missing_class_directive,
    }


def _batch(items: list[str], count: int) -> list[list[str]]:
    """Split *items* into at most *count* contiguous, near-equal batches."""
    if count <= 1 or not items:
        return [items]
    size = max(1, (len(items) + count - 1) // count)
    return [items[start : start + size] for start in range(0, len(items), size)]


# --------------------------------------------------------------------------
# resources
# --------------------------------------------------------------------------


def parse_public_xml(path: Path, types: tuple[str, ...]) -> dict:
    """Parse ``res/values/public.xml`` into ``type -> name -> hex id``.

    Also returns the entry count for *every* type present, which is what makes
    the sparse-string-encoding shortfall visible in the header.
    """
    resources: dict[str, dict[str, str]] = {resource_type: {} for resource_type in types}
    type_counts: dict[str, int] = {}
    duplicates: list[str] = []
    wanted = set(types)

    for _event, element in ElementTree.iterparse(str(path), events=("end",)):
        if element.tag != "public":
            continue
        resource_type = element.get("type")
        name = element.get("name")
        resource_id = element.get("id")
        if resource_type is None or name is None or resource_id is None:
            element.clear()
            continue
        type_counts[resource_type] = type_counts.get(resource_type, 0) + 1
        if resource_type in wanted:
            bucket = resources[resource_type]
            if name in bucket:
                duplicates.append(f"{resource_type}/{name}")
            else:
                bucket[name] = resource_id
        element.clear()

    return {
        "resources": resources,
        "type_counts": dict(sorted(type_counts.items())),
        "duplicates": duplicates,
    }


# --------------------------------------------------------------------------
# content hash
# --------------------------------------------------------------------------


def combine_digests(digests: list[tuple[str, bytes]]) -> str:
    """Order-independent, content-sensitive hash of the indexed inputs."""
    combined = hashlib.sha256()
    for relpath, digest in sorted(digests):
        combined.update(relpath.encode("utf-8"))
        combined.update(b"\0")
        combined.update(digest)
        combined.update(b"\n")
    return "sha256:" + combined.hexdigest()


def compute_content_hash(decode: Path) -> tuple[str, int, int]:
    """Recompute the hash of exactly the inputs the index is built from.

    Returns ``(content_hash, smali_file_count, resource_file_count)``.
    """
    digests: list[tuple[str, bytes]] = []
    for relpath in list_smali_files(decode):
        with open(decode / relpath, "rb") as source:
            digests.append((relpath, hashlib.sha256(source.read()).digest()))
    smali_count = len(digests)
    resource_count = 0
    public_xml = decode / PUBLIC_XML_RELPATH
    if public_xml.is_file():
        with open(public_xml, "rb") as source:
            digests.append((PUBLIC_XML_RELPATH, hashlib.sha256(source.read()).digest()))
        resource_count = 1
    return combine_digests(digests), smali_count, resource_count


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------


def build_index(
    decode: Path,
    out_dir: Path,
    jobs: int = 1,
    resource_types: tuple[str, ...] = DEFAULT_RESOURCE_TYPES,
    progress: bool = False,
) -> dict:
    """Build both indexes for *decode* into *out_dir*.  Returns the header.

    ``header.json`` holds the full header including run timings.  The copy
    embedded as the first line of ``structural.jsonl`` and under the ``header``
    key of ``api_surface.json`` is byte-identical to it apart from
    ``timings_seconds``/``jobs``, which describe the run rather than the index
    and cannot be known until the files have been written.  Staleness is
    decided only from ``schema_version``, ``decode_path`` and ``content_hash``,
    all of which appear in every copy.
    """
    decode = Path(decode).resolve()
    out_dir = Path(out_dir)
    if not decode.is_dir():
        raise NotADirectoryError(f"decode directory does not exist: {decode}")
    out_dir.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    trees = list_smali_trees(decode)
    relpaths = list_smali_files(decode)
    walk_seconds = time.perf_counter() - started
    if progress:
        print(
            f"[index] {len(relpaths)} smali files in {len(trees)} trees "
            f"(walk {walk_seconds:.1f}s)",
            file=sys.stderr,
            flush=True,
        )

    scan_started = time.perf_counter()
    structural_path = out_dir / STRUCTURAL_FILENAME
    shard_dir = out_dir / ".shards"
    shard_dir.mkdir(parents=True, exist_ok=True)

    batches = _batch(relpaths, jobs * 4) if jobs > 1 else [relpaths]
    shard_paths = [str(shard_dir / f"{index:05d}.jsonl") for index in range(len(batches))]
    job_args = [(str(decode), batch, shard) for batch, shard in zip(batches, shard_paths)]
    if jobs > 1 and len(batches) > 1:
        # ``map`` preserves submission order, so concatenating the shards in
        # order keeps structural.jsonl byte-identical to a single-job run.
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            results = list(pool.map(_scan_shard, job_args))
    else:
        results = [_scan_shard(job) for job in job_args]

    scan_seconds = time.perf_counter() - scan_started
    if progress:
        print(f"[index] scanned smali in {scan_seconds:.1f}s", file=sys.stderr, flush=True)

    digests: list[tuple[str, bytes]] = []
    api_paths: dict[str, set[str]] = {}
    stable_types: dict[str, str] = {}
    tree_counts: dict[str, int] = {}
    method_count = 0
    class_count = 0
    obfuscated_count = 0
    duplicate_descriptors: list[str] = []
    missing_class_directive: list[str] = []

    for result in results:
        digests.extend(result["digests"])
        for path_literal, descriptors in result["api_paths"].items():
            bucket = api_paths.get(path_literal)
            if bucket is None:
                api_paths[path_literal] = set(descriptors)
            else:
                bucket |= descriptors
        stable_types.update(result["stable_types"])
        for tree, count in result["tree_counts"].items():
            tree_counts[tree] = tree_counts.get(tree, 0) + count
            class_count += count
        method_count += result["method_count"]
        obfuscated_count += result["obfuscated_count"]
        duplicate_descriptors.extend(result["duplicate_descriptors"])
        missing_class_directive.extend(result["missing_class_directive"])

    public_xml = decode / PUBLIC_XML_RELPATH
    if public_xml.is_file():
        resource_data = parse_public_xml(public_xml, resource_types)
        with open(public_xml, "rb") as source:
            digests.append((PUBLIC_XML_RELPATH, hashlib.sha256(source.read()).digest()))
        resource_file_count = 1
    else:
        resource_data = {
            "resources": {resource_type: {} for resource_type in resource_types},
            "type_counts": {},
            "duplicates": [],
        }
        resource_file_count = 0

    resources = resource_data["resources"]
    names_by_id: dict[str, str] = {}
    for resource_type in sorted(resources):
        for name, resource_id in resources[resource_type].items():
            names_by_id.setdefault(resource_id, f"{resource_type}/{name}")

    content_hash = combine_digests(digests)

    header = {
        "kind": HEADER_KIND,
        "schema_version": SCHEMA_VERSION,
        "generator": GENERATOR,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "decode_path": str(decode),
        "decode_name": decode.name,
        "content_hash": content_hash,
        "content_hash_inputs": {
            "smali_files": len(relpaths),
            "resource_files": resource_file_count,
            "covers": ["smali trees", PUBLIC_XML_RELPATH],
        },
        "smali_trees": trees,
        "counts": {
            "classes": class_count,
            "methods": method_count,
            "smali_files": len(relpaths),
            "smali_trees": len(trees),
            "classes_by_tree": {tree: tree_counts.get(tree, 0) for tree in trees},
            "obfuscated_types": obfuscated_count,
            "stable_types": len(stable_types),
            "api_paths": len(api_paths),
            "api_path_class_pairs": sum(len(bucket) for bucket in api_paths.values()),
            "resources": {
                resource_type: len(resources[resource_type]) for resource_type in sorted(resources)
            },
        },
        "resource_types_indexed": list(resource_types),
        "public_xml_type_counts": resource_data["type_counts"],
        "anomalies": {
            "duplicate_descriptors": sorted(set(duplicate_descriptors))[:50],
            "duplicate_descriptor_count": len(set(duplicate_descriptors)),
            "smali_without_class_directive": sorted(missing_class_directive)[:50],
            "smali_without_class_directive_count": len(missing_class_directive),
            "duplicate_resource_names": sorted(set(resource_data["duplicates"]))[:50],
            "duplicate_resource_name_count": len(set(resource_data["duplicates"])),
        },
        "warning": PER_VERSION_WARNING,
        "string_resources": STRING_RESOURCE_NOTE,
    }

    write_started = time.perf_counter()
    header_line = json.dumps(header, separators=(",", ":"), ensure_ascii=False)
    with open(structural_path, "w", encoding="utf-8") as out:
        out.write(header_line)
        out.write("\n")
        for shard in shard_paths:
            with open(shard, "r", encoding="utf-8") as shard_handle:
                while True:
                    chunk = shard_handle.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
            os.remove(shard)
    try:
        shard_dir.rmdir()
    except OSError:  # pragma: no cover - shard dir not empty
        pass

    api_surface = {
        "header": header,
        "api_paths": {
            path_literal: sorted(api_paths[path_literal]) for path_literal in sorted(api_paths)
        },
        "resources": {
            resource_type: dict(sorted(resources[resource_type].items()))
            for resource_type in sorted(resources)
        },
        "resource_names_by_id": dict(sorted(names_by_id.items())),
        "stable_types": dict(sorted(stable_types.items())),
    }
    with open(out_dir / API_SURFACE_FILENAME, "w", encoding="utf-8") as out:
        json.dump(api_surface, out, separators=(",", ":"), ensure_ascii=False)
        out.write("\n")

    write_seconds = time.perf_counter() - write_started
    header = dict(header)
    header["jobs"] = jobs
    header["timings_seconds"] = {
        "walk": round(walk_seconds, 3),
        "scan": round(scan_seconds, 3),
        "write": round(write_seconds, 3),
        "total": round(time.perf_counter() - started, 3),
    }
    with open(out_dir / HEADER_FILENAME, "w", encoding="utf-8") as out:
        json.dump(header, out, indent=2, ensure_ascii=False)
        out.write("\n")

    if progress:
        print(
            f"[index] wrote {structural_path.name} and {API_SURFACE_FILENAME} "
            f"in {write_seconds:.1f}s "
            f"(total {header['timings_seconds']['total']:.1f}s)",
            file=sys.stderr,
            flush=True,
        )
    return header


def load_header(out_dir: Path) -> dict:
    with open(Path(out_dir) / HEADER_FILENAME, "r", encoding="utf-8") as handle:
        return json.load(handle)


def check_index(decode: Path, out_dir: Path) -> dict:
    """Is the index in *out_dir* still valid for *decode*?

    An index is stale when it was built from a different decode path, from a
    different schema, or when any indexed input changed.
    """
    decode = Path(decode).resolve()
    header = load_header(out_dir)
    content_hash, smali_count, resource_count = compute_content_hash(decode)

    reasons: list[str] = []
    if header.get("schema_version") != SCHEMA_VERSION:
        reasons.append(
            f"schema_version {header.get('schema_version')} != {SCHEMA_VERSION}"
        )
    if header.get("decode_path") != str(decode):
        reasons.append(f"built from {header.get('decode_path')}, checked against {decode}")
    if header.get("content_hash") != content_hash:
        reasons.append(
            f"content hash {header.get('content_hash')} != {content_hash}"
        )
    return {
        "fresh": not reasons,
        "reasons": reasons,
        "content_hash": content_hash,
        "smali_files": smali_count,
        "resource_files": resource_count,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the per-version Structural and API-Surface indexes for one decoded APK.",
        epilog=PER_VERSION_WARNING,
    )
    parser.add_argument("decode", type=Path, help="decoded APK directory (read-only)")
    parser.add_argument("--out", required=True, type=Path, help="output directory for the index")
    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="parallel scanner processes (default 1)",
    )
    parser.add_argument(
        "--resource-types",
        default=",".join(DEFAULT_RESOURCE_TYPES),
        help=f"comma-separated public.xml types to index (default {','.join(DEFAULT_RESOURCE_TYPES)})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not build; report whether the existing index is stale (exit 1 if it is)",
    )
    parser.add_argument("--quiet", action="store_true", help="suppress progress on stderr")
    args = parser.parse_args(argv)

    if args.check:
        result = check_index(args.decode, args.out)
        print(json.dumps(result, indent=2))
        return 0 if result["fresh"] else 1

    if args.jobs < 1:
        parser.error("--jobs must be >= 1")
    resource_types = tuple(part.strip() for part in args.resource_types.split(",") if part.strip())
    header = build_index(
        args.decode,
        args.out,
        jobs=args.jobs,
        resource_types=resource_types,
        progress=not args.quiet,
    )
    if not args.quiet:
        summary = {
            key: header[key]
            for key in ("decode_path", "content_hash", "counts", "timings_seconds")
        }
        print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

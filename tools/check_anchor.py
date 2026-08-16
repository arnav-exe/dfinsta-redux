"""Does this anchor pick out one class, on every version on disk?

    tools/check_anchor.py --anchor-file candidate.txt
    tools/check_anchor.py --hook replace_reels_discover_endpoint --form 1

An anchor is a claim about shape: *these instructions, in this order, appear in
exactly one place*. The claim is cheap to state, expensive to be wrong about, and
**arithmetic to check** — which is the whole reason this exists.

===============================================================================
  WHY A TOOL AND NOT A JUDGEMENT
===============================================================================

Instagram 442 moved a literal out of the class that builds the Reels request into
a shared string table, and the anchor pinning it stopped matching. The repair was
a second anchor keyed on `Lcom/instagram/clips/api/ClipsApiUtilHelper;` — a real
class name rather than an obfuscated one — and the only thing that made it
trustworthy was counting: it matches **exactly one site in the whole decode** on
439, 440, 441 and 442. Every rejected candidate was rejected by a count too: the
instruction shape around the stream literal matched 114 sites, the MobileConfig
gate before the homecoming literal matched about 1,400, and a promising-looking
`equals` disjunction matched 49.

None of those were distinguishable by reading. All of them were distinguishable
by counting in under a minute. So: propose by judgement, accept by arithmetic.

===============================================================================
  IT USES THE PIPELINE'S OWN MATCHER, DELIBERATELY
===============================================================================

The scan is `resolve.scan_for_anchor` and the pattern compiler is
`hook_manifest.compile_anchor` — the same code the Resolve stage runs. A checker
with its own regex would eventually disagree with the resolver about what matches,
and would do so silently, which is worse than having no checker: you would trust a
count that describes a different question from the one being asked.

===============================================================================
  WHAT THE VERDICTS MEAN
===============================================================================

**unique** — every decode matched exactly one class. What a `by_anchor` host
needs, because that fingerprint's whole claim is uniqueness over the decode.

**selective** — every decode matched at most one class, and at least one matched.
This is the shape of a *variant*: the 442 pooled-fetch anchor matches once on 442
and zero times on 439, 440 and 441, and that is correct rather than a weakness. It
is only usable as a `by_anchor` fingerprint scoped to its own form, which is what
`HostFingerprint.form` exists for.

**ambiguous** — some decode matched more than one class. Unusable as an identity;
usable only inside a host something else already picked.

**dead** — nothing matched anywhere. Reported as a failure and never as "no
problems found": an anchor that cannot match is exactly as quiet as one that
matches perfectly, and this project has shipped that confusion before.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY / "src"))

from dfinsta_pipeline.hook_manifest import (  # noqa: E402
    Hook,
    HostFingerprint,
    ManifestError,
    load_manifest,
)
from dfinsta_pipeline.resolve import scan_for_anchor  # noqa: E402

#: A decode directory's parent is named `<version><suffix>` — `441-port`,
#: `440-clean`. The leading digits are the version and the rest is which run
#: produced it, which matters because several runs of one version can be on disk
#: and they are not guaranteed to be the same tree.
_VERSION = re.compile(r"^(\d+)")

#: The marker of the throwaway hook built to carry a candidate anchor. It must
#: appear in the payload exactly once (`Hook` refuses otherwise) and must never
#: appear in a real decode, or `scan_for_anchor` would report classes as already
#: carrying this patch.
PROBE_MARKER = "# dfinsta_check_anchor_probe"

UNIQUE, SELECTIVE, AMBIGUOUS, DEAD = "unique", "selective", "ambiguous", "dead"

#: Exit codes. `dead` is louder than `ambiguous` on purpose: an ambiguous anchor
#: is a real thing that needs narrowing, whereas a dead one usually means the
#: pattern is malformed or was written against a version that is not here.
EXIT = {UNIQUE: 0, SELECTIVE: 0, AMBIGUOUS: 1, DEAD: 2}


@dataclass(frozen=True)
class Decode:
    """One decoded tree, and which port run produced it."""

    label: str
    version: str
    path: Path

    def __lt__(self, other: "Decode") -> bool:
        return (self.version, self.label) < (other.version, other.label)


@dataclass(frozen=True)
class Result:
    """What one anchor did in one decode."""

    decode: Decode
    matched: tuple[str, ...]
    scanned: int
    survivors: int
    prefilter: str


def decodes(root: Path) -> tuple[Decode, ...]:
    """Every decoded tree under `work/`, discovered rather than configured.

    Listed, not filtered to "the interesting ones": a version that was decoded
    and then left out of the check would be a version this tool silently did not
    answer for, and the answer it gives is a claim about *every* version on disk.
    """
    found: list[Decode] = []
    for path in sorted((root / "work").glob("*/analysis-decode")):
        if not path.is_dir():
            continue
        label = path.parent.name
        match = _VERSION.match(label)
        found.append(Decode(label, match.group(1) if match else "?", path))
    return tuple(sorted(found))


def probe_hook(anchor: tuple[str, ...]) -> Hook:
    """A throwaway hook carrying nothing but the anchor being checked.

    `scan_for_anchor` takes a `Hook` because that is what the Resolve stage has;
    everything else here is the minimum that `Hook.__post_init__` accepts. The
    payload is the marker alone, so it declares no captures the anchor has to
    bind and cannot fail validation for a reason that has nothing to do with the
    anchor under test.
    """
    return Hook(
        hook_id="check_anchor_probe",
        intent="check an anchor's selectivity",
        tier="ui",
        strategy="none",
        semantic_deps=(),
        hosts=(HostFingerprint("by_anchor", note="the anchor being checked"),),
        anchor=anchor,
        payload=(f"    {PROBE_MARKER}",),
        marker=PROBE_MARKER,
        expected_marker_count=1,
    )


def check(anchor: tuple[str, ...], root: Path, only: tuple[str, ...] = ()) -> list[Result]:
    hook = probe_hook(anchor)
    results: list[Result] = []
    for decode in decodes(root):
        if only and decode.label not in only and decode.version not in only:
            continue
        scan = scan_for_anchor(hook, decode.path)
        results.append(
            Result(decode, scan.matched, scan.scanned, scan.survivors, scan.prefilter)
        )
    return results


def verdict(results: list[Result]) -> str:
    counts = [len(result.matched) for result in results]
    if not counts or not any(counts):
        return DEAD
    if any(count > 1 for count in counts):
        return AMBIGUOUS
    return UNIQUE if all(count == 1 for count in counts) else SELECTIVE


def anchor_from_manifest(hook_id: str, form: int) -> tuple[str, ...]:
    for hook in load_manifest(REPOSITORY / "manifest" / "hooks.json"):
        if hook.hook_id == hook_id:
            if form >= len(hook.forms):
                raise SystemExit(
                    f"refusing: {hook_id} has {len(hook.forms)} form(s); asked for {form}"
                )
            return hook.forms[form].anchor
    raise SystemExit(f"refusing: no hook {hook_id!r} in the manifest")


def read_anchor(path: Path) -> tuple[str, ...]:
    """One pattern line per line. Blank lines are ignored; nothing else is.

    A `#` line is NOT treated as a comment: `significant()` drops comment lines
    from a class body, so an anchor line starting with `#` could never match, and
    silently discarding it here would hide that rather than let the scan report a
    dead anchor.
    """
    lines = tuple(
        line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    )
    if not lines:
        raise SystemExit(f"refusing: {path} holds no pattern lines")
    return lines


def render(anchor: tuple[str, ...], results: list[Result], found: str) -> str:
    out = [f"ANCHOR CHECK   {len(anchor)} line(s)", "=" * 72, ""]
    for line in anchor:
        out.append(f"    {line}")
    out.append("")
    if not results:
        out.append("  no decoded tree under work/ — nothing to check against")
        return "\n".join(out)
    prefilter = results[0].prefilter
    out.append(
        f"  prefilter   {prefilter!r}"
        if prefilter
        else "  prefilter   none long enough to grep; every class is matched in full"
    )
    out.append("")
    out.append(f"  {'decode':<22}{'scanned':>9}{'survived':>10}{'classes':>9}   where")
    for result in results:
        where = ", ".join(result.matched[:3]) or "—"
        if len(result.matched) > 3:
            where += f", … {len(result.matched) - 3} more"
        out.append(
            f"  {result.decode.label:<22}{result.scanned:>9}{result.survivors:>10}"
            f"{len(result.matched):>9}   {where}"
        )
    out.append("")
    out.append(f"  VERDICT  {found}")
    out.append("  " + {
        UNIQUE: "one class in every decode — usable as an identity, which is what "
                "a by_anchor host claims",
        SELECTIVE: "at most one class in every decode, and not present in all of them "
                   "— the shape of a variant. Usable as a by_anchor host SCOPED to "
                   "its own form; unscoped it would contribute nothing on the "
                   "versions it does not match and could not be told apart from a "
                   "dead one",
        AMBIGUOUS: "more than one class somewhere — this cannot identify a host. It "
                   "may still be a fine anchor INSIDE a class something else picked, "
                   "which is what the counts per decode are for",
        DEAD: "nothing matched anywhere. Not a pass: check the pattern compiles "
              "against a line you have actually read, and that the version you "
              "wrote it for is one of the decodes above",
    }[found])
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--anchor-file", type=Path, help="one pattern line per line")
    source.add_argument("--hook", help="check an anchor already in the manifest")
    parser.add_argument("--form", type=int, default=0, help="which form of --hook")
    parser.add_argument(
        "--decode", action="append", default=[],
        help="limit to a decode by label (441-port) or version (441); repeatable",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.anchor_file is not None:
        if not args.anchor_file.is_file():
            print(f"refusing: {args.anchor_file} is not a file", file=sys.stderr)
            return 2
        anchor = read_anchor(args.anchor_file)
    else:
        anchor = anchor_from_manifest(args.hook, args.form)

    try:
        results = check(anchor, REPOSITORY, tuple(args.decode))
    except ManifestError as error:
        # A pattern that does not compile is the commonest mistake by far, and the
        # message says which capture and why. Reported as a refusal rather than a
        # traceback because it is a thing the caller can fix.
        print(f"refusing: the anchor does not compile — {error}", file=sys.stderr)
        return 2

    found = verdict(results)
    if args.json:
        print(json.dumps({
            "anchor": list(anchor),
            "verdict": found,
            "decodes": [
                {
                    "label": result.decode.label,
                    "version": result.decode.version,
                    "matched": list(result.matched),
                    "scanned": result.scanned,
                    "survivors": result.survivors,
                }
                for result in results
            ],
        }, indent=2))
    else:
        print(render(anchor, results, found))
    return EXIT[found]


if __name__ == "__main__":
    raise SystemExit(main())

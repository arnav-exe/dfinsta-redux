"""Reduce a logcat capture to the lines the observation parser reads.

A raw capture is ~1.4 MB of the phone's whole log. It carries no account content
in the lines we use, but it does carry a lot of unrelated app telemetry, and none
of it belongs in a public repository. It is also gitignored, which leaves a worse
problem: `manifest/observations/<version>.jsonl` is committed evidence that
**nothing committed can be checked against**. A clone gets the counts and no way
to re-derive them.

So the capture is reduced to a few kinds of line and committed alongside the store:

  * `DFInstaObserve: !toggles +blocked …` — which blocks were active, as the build
    reported them, and what this build's instrumentation is able to state
  * `DFInstaObserve: /some/path/` — one per watched request
  * `DFInstaObserve: !blocked /some/path/` — one per request the guard refused,
    naming the literal that matched. This is the block signal; the two below are
    Instagram's own report of the same event, kept as corroboration
  * the block header `IgFunctionalErrorEvent: java.io.IOException: Blocked by …`
    and the category line immediately above it, which names the failing feature

Every `DFInstaObserve` line is kept whatever it says, so a directive this reducer
has not heard of survives into the committed capture rather than being filtered
out by a tool that predates it.

Everything else goes, including `IgFunctionalErrorEvent` lines about unrelated
failures, cold-start timings and module names.

**Losslessness is the property that matters** and it is checkable rather than
asserted: `observation.parse` over the redaction must equal `parse` over the
original. `--verify` does that comparison and refuses on any difference, so a
redaction that quietly dropped a line could never be committed as evidence.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

OBSERVE = re.compile(r"DFInstaObserve:")
BLOCK = re.compile(r"IgFunctionalErrorEvent: java\.io\.IOException: Blocked by DFInsta setting")
CATEGORY = re.compile(r"IgFunctionalErrorEvent: [A-Z_]+\s*$")


def redact(text: str) -> str:
    """The three kinds of line, in their original order."""
    lines = text.splitlines()
    keep: list[int] = []
    for index, line in enumerate(lines):
        if OBSERVE.search(line):
            keep.append(index)
        elif BLOCK.search(line):
            # The category sits on the line above and is what attributes the
            # block to a feature. Kept together or the pairing is lost.
            if index and CATEGORY.search(lines[index - 1]):
                keep.append(index - 1)
            keep.append(index)
    ordered = sorted(dict.fromkeys(keep))
    return "\n".join(lines[i] for i in ordered) + ("\n" if ordered else "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("capture", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--verify", action="store_true",
        help="parse both forms and refuse if they disagree. Use it: a redaction "
        "that dropped a line would otherwise become the committed evidence.",
    )
    args = parser.parse_args(argv)

    original = args.capture.read_text(encoding="utf-8", errors="replace")
    reduced = redact(original)

    if args.verify:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
        from dfinsta_pipeline.observation import parse  # noqa: PLC0415

        before, after = parse(original), parse(reduced)
        if before != after:
            print(
                f"refusing: the redaction changed what the parser sees.\n"
                f"  original: {before}\n  redacted: {after}",
                file=sys.stderr,
            )
            return 2

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(reduced, encoding="utf-8")
    print(
        f"{args.capture.name}: {len(original.splitlines())} lines -> "
        f"{len(reduced.splitlines())} ({args.out})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

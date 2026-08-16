"""Ask for a new anchor when a host resolved and its anchor did not.

    tools/reanchor.py --resolution work/442-port/resolution.json \\
                      --hook replace_reels_discover_endpoint
    tools/reanchor.py --resolution … --hook … --apply

Driven by the artefact the failure produces. When a version changes the shape of
a patch site, `resolution.json` says so in as many words — *"1 candidate host(s)
found, none matched the anchor"* — and carries both the class it found and the
decode it looked in. This reads that, asks k agents for a new pattern, and
**counts** every answer before believing any of it.

===============================================================================
  WHAT IT COSTS, AND WHAT IT WILL NOT DO
===============================================================================

Every run is agent invocations, which is the number this project measures. It
therefore does nothing on its own: it is invoked for one named hook, by a person
who has already seen a port stop.

It never writes to the manifest without `--apply`, and with `--apply` it appends
one variant to one hook and nothing else. The manifest is the 280 KB that is the
project; an agent's answer does not land in it because a script felt confident.

**No agent runtime is not a finding.** If `claude_agent_sdk` is missing this
refuses and says so, rather than reporting that no anchor could be found — those
two look identical in a results file and mean opposite things.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY / "src"))

from dfinsta_pipeline.hook_manifest import load_manifest  # noqa: E402
from dfinsta_pipeline.reanchor import (  # noqa: E402
    ReanchorRun,
    apply_variant,
    as_variant,
    collect,
)

sys.path.insert(0, str(REPOSITORY / "tools"))
from check_anchor import check as count_anchor  # noqa: E402
from check_anchor import decodes  # noqa: E402

MANIFEST = REPOSITORY / "manifest" / "hooks.json"


def host_from(report: dict, hook_id: str) -> tuple[str, Path]:
    """The class the resolver found, and the decode it looked in.

    The hook did not resolve — that is the premise — so `descriptor` is usually
    empty and the class is the single candidate its host search returned. More
    than one candidate is refused rather than picked from: this tool repairs an
    anchor, and a hook with two possible hosts has a different problem.
    """
    decode = Path(report["decode"])
    for item in report["resolutions"]:
        if item["hook_id"] != hook_id:
            continue
        if item.get("descriptor"):
            return item["descriptor"], decode
        candidates = sorted(
            {name for search in item.get("searches", ()) for name in search.get("candidates", ())}
        )
        if len(candidates) == 1:
            return candidates[0], decode
        raise SystemExit(
            f"refusing: {hook_id} has {len(candidates)} candidate host(s) "
            f"{candidates}. This repairs an anchor inside a known class; finding "
            "the class is what --discover-hosts is for"
        )
    raise SystemExit(f"refusing: {hook_id} is not in {report.get('decode', 'that report')}")


def declares(source: str, descriptor: str) -> bool:
    """Is this file's `.class` line this descriptor?

    Checked by the END of the line rather than by a fixed prefix: the modifiers
    in between vary, and `.class public final LX/8Ec;` is what 442's host really
    says. Matching `.class public {descriptor}` found nothing and reported it as
    the class being absent from the decode.
    """
    for line in source.splitlines():
        if line.startswith(".class"):
            return line.rstrip().endswith(descriptor)
    return False


def source_of(descriptor: str, decode: Path) -> str:
    name = descriptor.strip("L;").split("/")[-1]
    exact = [
        path
        for path in sorted(decode.glob(f"smali*/**/{name}.smali"))
        if declares(path.read_text(encoding="utf-8", errors="replace"), descriptor)
    ]
    if len(exact) != 1:
        raise SystemExit(
            f"refusing: {descriptor} matched {len(exact)} file(s) under {decode}"
        )
    return exact[0].read_text(encoding="utf-8", errors="replace")


def counts_for_anchor(anchor: tuple[str, ...]) -> dict[str, int]:
    """How many classes this anchor picks out, per decode on disk.

    `tools/check_anchor.py` again rather than a scan of its own: one counter, so
    a candidate accepted here is one that tool would also call selective.
    """
    return {
        result.decode.label: len(result.matched)
        for result in count_anchor(anchor, REPOSITORY)
    }


def render(run: ReanchorRun) -> str:
    out = [f"REANCHOR  {run.hook_id}   host {run.host}", "=" * 72, ""]
    if run.failures:
        out.append("  proposers that failed to answer:")
        out += [f"    {item}" for item in run.failures]
        out.append("")
    for item in run.checked:
        mark = "ACCEPTED" if item.accepted else item.outcome
        out.append(f"  [{mark}] {item.candidate.proposer}   {len(item.candidate.anchor)} line(s)")
        out.append(f"      {item.reason}")
        if item.counts:
            out.append("      per decode: " + ", ".join(
                f"{label}={value}" for label, value in sorted(item.counts.items())
            ))
        for line in item.candidate.anchor:
            out.append(f"        {line}")
        out.append("")
    winner = run.winner
    if winner is None:
        out.append("  nothing was accepted. Every answer is above with why it failed —")
        out.append("  read them before running this again: a prompt that produced four")
        out.append("  near-misses needs different wording, and four nonsense answers")
        out.append("  usually mean there is no stable landmark near this site.")
    else:
        out.append(f"  WINNER  {winner.candidate.proposer}, {len(winner.candidate.anchor)} lines")
        out.append("  Re-run with --apply to write it into the manifest as a variant,")
        out.append("  then re-run the port: the build and its verifier are the next check.")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--resolution", type=Path, required=True)
    parser.add_argument("--hook", required=True)
    parser.add_argument("-k", type=int, default=3, help="proposers to ask")
    parser.add_argument("--model", default=None)
    parser.add_argument("--apply", action="store_true",
                        help="write the winning variant into manifest/hooks.json")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if not args.resolution.is_file():
        print(f"refusing: {args.resolution} is not a file", file=sys.stderr)
        return 2
    if args.k < 1:
        print("refusing: -k must be at least 1", file=sys.stderr)
        return 2
    if not decodes(REPOSITORY):
        print("refusing: no decoded tree under work/, so no answer could be counted",
              file=sys.stderr)
        return 2

    report = json.loads(args.resolution.read_text(encoding="utf-8"))
    host, decode = host_from(report, args.hook)
    hook = next((item for item in load_manifest(MANIFEST) if item.hook_id == args.hook), None)
    if hook is None:
        print(f"refusing: no hook {args.hook!r} in the manifest", file=sys.stderr)
        return 2

    # Imported here so that everything above — reading the report, finding the
    # class, loading the hook — works on a machine with no agent runtime, and the
    # refusal names the runtime rather than arriving as an import error.
    from dfinsta_pipeline.agent_runner import AgentUnavailable, build_claude_runner

    try:
        proposers = {
            f"proposer-{index}": build_claude_runner(decode, model=args.model)
            for index in range(args.k)
        }
    except AgentUnavailable as error:
        print(f"refusing: {error}", file=sys.stderr)
        return 3

    version = str(report.get("version") or decode.parent.name)
    run = collect(
        hook, host, source_of(host, decode), version, proposers, counts_for_anchor
    )

    print(json.dumps(run.to_dict(), indent=2) if args.json else render(run))

    winner = run.winner
    if not args.apply:
        return 0 if winner else 1
    if winner is None:
        print("refusing to apply: nothing was accepted", file=sys.stderr)
        return 1
    note = (
        f"Proposed by {winner.candidate.proposer} for {version} and accepted by counting: "
        f"once inside {host}, and never more than one class in any decode on disk. "
        "The port's build and its verifier are what check it next."
    )
    apply_variant(MANIFEST, args.hook, as_variant(winner, note))
    print(f"\n  written into {MANIFEST} as a variant of {args.hook}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

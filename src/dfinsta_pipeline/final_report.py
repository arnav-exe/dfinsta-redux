"""Stage 11: what a port can be *shown* to have achieved, and what it cannot.

    python -m dfinsta_pipeline.final_report --version 440 \
        --evidence work/440-attributed/evidence.jsonl \
        --evidence manifest/runtime_evidence/440.jsonl \
        --evidence manifest/differentials/439-440.jsonl

The last stage of `pipeline_flowchart.md` and the only one never started. It was
not started because until 2026-08-06 it could not have said anything true:

* **`static_verified` had no producer.** One of the three kinds required after a
  build was declared in `evidence.py` and emitted by nothing, so every hook on
  every version escalated on it and release readiness was unsatisfiable by
  construction rather than by any hook's fault.
* **A differential had nowhere durable to live.** It was computed, printed and
  thrown away each time, so the second required kind was permanently absent too.
* **A claim could not be joined to anything.** No version, no build hash, and
  `recorded_at` empty on all thirty committed claims. A report could not order
  claims in time, date a port, or say which APK a device measurement was taken
  against.

All three are closed, so this reads a run rather than guessing at one.

===============================================================================
  WHAT THIS REFUSES TO DO
===============================================================================

**It computes nothing about a hook.** Every verdict here is `EvidenceLedger`'s,
reached through the same `report(POST_BUILD)` the driver prints and the release
gate would consult. A reporter that re-derived readiness would be a second
opinion on the one question the ledger exists to answer, and the two would agree
until one was edited.

**It will not describe a hook it has no subject for.** A `Subject` decides which
evidence a hook owes, so one has to exist before anything can be said.
`--provenance HOOK=KIND` supplies it, and an agent-resolved hook is
`HOOK=agent:PROPOSER-ID` because `Subject` refuses an agent hook that does not
name its proposer -- evidence produced *by* the proposer is a schema error. A key
naming a hook with no claims is refused rather than ignored.

**But provenance changes no verdict here, and saying otherwise would be the
overstatement worth avoiding.** Measured: `mechanical`, `agent` and
`already_applied` require *exactly the same three kinds* after a build. Everything
provenance decides -- an agent hook's `adversarial_verified` and
`proposer_agreement`, an already-applied hook's exemption from `registers_safe` --
lives in PRE_APPLY, which this stage does not report. Reporting the full set was
tried and reverted: a port's pre-apply claims live in its run directory under
gitignored `work/`, so a durable report assembled from `manifest/` would escalate
every hook for want of files nobody keeps. That half is separately gated anyway --
the driver refuses to build when it fails, so a build existing is that gate having
passed. Do not read "reported as agent" here as "the agent-specific checks were
consulted".

**An unversioned claim answers to whatever version is asked for**, because absent
`version` means "recorded before attribution existed" and not "belongs to no
port". So a report over 440's files asking for 439 is not refused -- the
unattributed device probes are inherited, the attributed claims are excluded and
listed, and the header says how many were inherited. Nothing is certified, but it
is not the refusal a reader might expect; check `inherited` before quoting a
number from an old corpus.

**It will not silently join evidence about two different APKs.** Claims now carry
`build_sha256`, and a report over claims naming more than one build says so in
its own header. That is not pedantry: the first time these files were combined,
the `static_verified` claims named a build the device evidence had never been
taken against, and the two APKs turned out to differ by exactly one ZIP timestamp
on `classes21.dex` -- byte-identical in every entry. The right answer was to
*make that argument*, not to skip the check.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from .evidence import (
    POST_BUILD,
    PRE_APPLY,
    EvidenceClaim,
    EvidenceError,
    EvidenceKind,
    EvidenceLedger,
    Subject,
    Verdict,
)

__all__ = ["ReportError", "PortReport", "build_report", "render", "main"]


class ReportError(RuntimeError):
    """Raised when a report cannot honestly be produced."""


@dataclass(frozen=True)
class PortReport:
    """One port, as far as its evidence goes."""

    version: str
    hooks: tuple[str, ...]
    ready: tuple[str, ...]
    escalations: tuple[dict[str, Any], ...]
    builds: tuple[str, ...]
    recorded_span: tuple[str, str] | None
    claim_counts: dict[str, int]
    #: Claims whose `version` is not this report's. Reported rather than dropped.
    foreign: tuple[str, ...]
    #: How many claims name an APK at all. See `render` — one digest in a header
    #: reads as though every claim were about it.
    claims_naming_a_build: int = 0

    @property
    def complete(self) -> bool:
        return not self.escalations

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "version": self.version,
            "complete": self.complete,
            "hooks": list(self.hooks),
            "release_ready": list(self.ready),
            "escalations": [dict(item) for item in self.escalations],
            "builds": list(self.builds),
            "recorded_span": list(self.recorded_span) if self.recorded_span else None,
            "claim_counts": dict(self.claim_counts),
            "claims_naming_a_build": self.claims_naming_a_build,
            "foreign_versions": list(self.foreign),
        }


def read_claims(paths: Sequence[Path]) -> list[EvidenceClaim]:
    """Every claim in every file, in the order given.

    A missing file is an error rather than an empty list. A report assembled from
    three sources of which one silently contributed nothing is the shape that
    reads as "this hook has no runtime evidence" when the truth is "nobody looked
    in the right place".
    """

    claims: list[EvidenceClaim] = []
    empty: list[Path] = []
    for path in paths:
        if not path.is_file():
            raise ReportError(f"no evidence at {path}")
        before = len(claims)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                claims.append(EvidenceClaim.from_dict(json.loads(line)))
            # `ValueError` and not just `EvidenceError`: `from_dict` calls
            # `EvidenceKind(...)` and `Verdict(...)`, and an unknown member raises
            # a plain `ValueError` — which `EvidenceError` subclasses but is not.
            # An evidence kind written by a newer pipeline therefore escaped as a
            # traceback and exited 1, and a release script reads exit 1 as "this
            # port is not ready" rather than "I could not read the file".
            # `EvidenceLedger.load` already catches the wider tuple over the same
            # call; this is that rule, in the second place that makes it.
            except (json.JSONDecodeError, KeyError, ValueError) as error:
                raise ReportError(f"{path}:{number}: {error}") from error
        if len(claims) == before:
            empty.append(path)
    if not claims:
        raise ReportError("no claims in any evidence file")
    if empty:
        # Reported, not refused. A differential file legitimately holds nothing
        # for the first version of a line, so refusing would block a real case —
        # but an empty runtime file among full ones makes a report say "no
        # runtime evidence for any hook" when the truth is "that file is empty",
        # which is the exact confusion this function exists to prevent.
        print(
            "note: no claims in " + ", ".join(str(path) for path in empty),
            file=sys.stderr,
        )
    return claims


def build_report(
    version: str,
    claims: Iterable[EvidenceClaim],
    *,
    provenance: dict[str, str] | None = None,
    default_provenance: str = "mechanical",
) -> PortReport:
    """Readiness for one version, from claims that may span several files.

    ``provenance`` maps a hook to how it was resolved, and an agent-resolved one
    must also name its proposer -- ``{"hook": "agent:claude-x"}``. That is not a
    formality: `Subject` refuses an agent hook with no proposer, and evidence
    produced *by* the proposer is a schema error rather than a judgement call.
    Without the proposer this function could only ever register such a hook as
    `mechanical`, scoring it under the **smaller** requirement set — the opposite
    of what naming it `agent` was meant to do.
    """

    claims = list(claims)
    provenance = dict(provenance or {})
    mine = [claim for claim in claims if claim.version in (None, version)]
    foreign = sorted({
        claim.version for claim in claims if claim.version not in (None, version)
    } - {None})

    hooks = sorted({claim.hook_id for claim in mine})
    if not hooks:
        raise ReportError(f"no claims for version {version}")

    # A key naming a hook with no claims used to be dropped by `dict.get`, so a
    # misspelt id changed nothing and said nothing while the report looked as
    # though the flag had been honoured. Silence that under-requires is the one
    # failure this whole stage is supposed to make impossible.
    unknown = sorted(set(provenance) - set(hooks))
    if unknown:
        raise ReportError(
            f"--provenance names {', '.join(unknown)}, which has no claim in this "
            f"report. Hooks present: {', '.join(hooks)}"
        )

    ledger = EvidenceLedger()
    for hook in hooks:
        kind, _, proposer = provenance.get(hook, default_provenance).partition(":")
        ledger.register(Subject(hook, kind, proposed_by=proposer))
    for claim in mine:
        ledger.record(claim)

    # POST_BUILD, not the whole requirement set, and the reason is the corpus
    # rather than the question. Reporting every kind was tried and reverted: the
    # pre-apply claims of a port live in its run directory under gitignored
    # `work/`, so a durable report assembled from `manifest/` would escalate
    # every hook for want of files nobody keeps — incomplete for a reason of file
    # location rather than of evidence. The pre-apply half is also already gated:
    # the driver refuses to build when it fails, so a build existing at all is
    # that gate having passed.
    #
    # **Measured consequence, worth knowing before reading a verdict here:**
    # `mechanical`, `agent` and `already_applied` require exactly the same three
    # kinds after a build. Everything provenance decides -- an agent hook's
    # `adversarial_verified` and `proposer_agreement`, an already-applied hook's
    # exemption from `registers_safe` -- lives in PRE_APPLY. So `--provenance`
    # changes no verdict in this report. It is still needed to *construct* the
    # subject, and an agent hook still cannot be registered without naming its
    # proposer, but a reader must not take "reported as agent" here to mean the
    # agent-specific checks were consulted. They were not.
    report = ledger.report(POST_BUILD)
    escalated = {item["hook_id"] for item in report["escalations"]}

    stamps = sorted(claim.recorded_at for claim in mine if claim.recorded_at)
    builds = sorted({claim.build_sha256 for claim in mine if claim.build_sha256})
    return PortReport(
        version=version,
        hooks=tuple(hooks),
        ready=tuple(hook for hook in hooks if hook not in escalated),
        escalations=tuple(report["escalations"]),
        builds=tuple(builds),
        recorded_span=(stamps[0], stamps[-1]) if stamps else None,
        claim_counts=dict(Counter(claim.kind.value for claim in mine)),
        foreign=tuple(foreign),
        claims_naming_a_build=sum(1 for claim in mine if claim.build_sha256),
    )


def render(report: PortReport) -> str:
    lines = [
        f"DFInsta port {report.version}",
        "=" * (13 + len(report.version)),
        "",
        f"  hooks              {len(report.hooks)}",
        f"  RELEASE-READY      {len(report.ready)} of {len(report.hooks)}"
        "   (post-build evidence)",
    ]
    if report.recorded_span:
        first, last = report.recorded_span
        lines.append(f"  evidence recorded  {first}" + (f" .. {last}" if last != first else ""))
    else:
        # Said, not omitted. Undated evidence was the norm until 2026-08-06 and a
        # blank line here would read as a report that simply did not mention it.
        lines.append("  evidence recorded  (undated — claims predate attribution)")

    # How MANY claims name a build, not just which builds appear. One digest on
    # the header would read as "all of this is about that APK" when in practice
    # only `static_verified` carries one: the device evidence for 440 names a
    # serial and no artifact, and a differential deliberately names none because
    # it spans two. Reporting the coverage keeps "measured against this APK" and
    # "measured, somewhere" apart.
    named = report.claims_naming_a_build
    total = sum(report.claim_counts.values())
    if not report.builds:
        lines.append("  build              (no claim names an APK)")
    elif len(report.builds) == 1:
        lines.append(f"  build              {report.builds[0]}")
        lines.append(f"                     named by {named} of {total} claims")
        if named < total:
            lines.append(
                "                     the rest are not tied to any artifact — a device "
                "probe names a serial, a differential spans two builds"
            )
    else:
        lines.append(f"  build              *** {len(report.builds)} DIFFERENT APKs ***")
        for digest in report.builds:
            lines.append(f"                       {digest}")
        lines.append(
            "                     Claims about different artifacts are combined here. "
            "Show the builds equivalent or re-measure."
        )
    if report.foreign:
        lines.append(f"  other versions     {', '.join(report.foreign)} (excluded)")

    lines.append("")
    lines.append("  evidence")
    for kind, count in sorted(report.claim_counts.items()):
        lines.append(f"    {kind:<24} {count}")

    lines.append("")
    if report.ready:
        lines.append("  Release-ready — every required kind passed:")
        for hook in report.ready:
            lines.append(f"    ✓ {hook}")
    else:
        lines.append("  No hook has complete post-build evidence.")

    if report.escalations:
        lines.append("")
        lines.append(f"  Needs a human ({len(report.escalations)}):")
        for item in report.escalations:
            lines.append(f"    ✗ {item['hook_id']}")
            for reason in item.get("reasons", ()):
                lines.append(f"        {reason}")

    lines.append("")
    lines.append(
        "  This says what the evidence shows. It is not a statement that the app "
        "works: every inert patch this project has shipped passed everything up to "
        "the runtime probe."
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", required=True, help="the port to report on, e.g. 440")
    parser.add_argument(
        "--evidence",
        type=Path,
        action="append",
        required=True,
        dest="evidence",
        metavar="JSONL",
        help="an evidence file; repeatable. A port's claims are spread across the "
        "run directory, manifest/runtime_evidence and manifest/differentials",
    )
    parser.add_argument(
        "--provenance",
        action="append",
        default=[],
        metavar="HOOK=KIND",
        help="how a hook was resolved: mechanical, already_applied, or "
        "agent:PROPOSER-ID. It decides which evidence that hook owes, and an "
        "agent-resolved hook must name its proposer because evidence produced by "
        "the proposer is a schema error",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", type=Path, help="write the rendered report here too")
    args = parser.parse_args(argv)

    overrides: dict[str, str] = {}
    for item in args.provenance:
        hook, separator, kind = item.partition("=")
        if not separator:
            print(f"refused: --provenance wants HOOK=KIND, got {item!r}", file=sys.stderr)
            return 2
        overrides[hook] = kind

    try:
        report = build_report(
            args.version, read_claims(args.evidence), provenance=overrides
        )
    except (ReportError, EvidenceError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2

    text = json.dumps(report.to_dict(), indent=2) if args.json else render(report)
    print(text)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    # Exit 1 when incomplete, so a release script can gate on it without parsing
    # prose -- the same shape `verify_build` and `rulings --audit` already use.
    return 0 if report.complete else 1


if __name__ == "__main__":
    raise SystemExit(main())

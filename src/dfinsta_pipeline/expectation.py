"""What this port owes the last one, and what it must not quietly lose.

    python -m dfinsta_pipeline.expectation --version 441

`final_report` says how many hooks a port can show to be release-ready.
`history` prints that number next to the previous versions'. Neither *fails* when
it falls, and a count that only ever gets printed is a count nobody notices
moving: 441 reads 4 of 7, and the whole of that sentence a reader keeps is "4".
If 442 reads 3 of 7 the report is equally calm about it.

So this is the assertion the other two deliberately refuse to make —

    every hook that was release-ready on N-1 is release-ready on N.

===============================================================================
  DERIVED, NEVER DECLARED
===============================================================================

There is no expected count in this file, in a config file, or on the command
line, and adding one would undo the point. A declared expectation has exactly one
repair when it fails, it takes one character, and it is indistinguishable in a
diff from a legitimate change: edit 4 to 3. The expectation is instead recomputed
from the previous version's committed evidence every time it is asked for, which
leaves exactly one way to lower it: make the hook pass again.

**There is exactly one escape hatch and it is a recorded human decision.** A
ratchet with no release is a trap: when Instagram genuinely removes a surface,
the hook that patched it can never pass again and would fail this check for ever.
So `retirement` subtracts a hook from the expectation — but only through an
append-only row naming who ruled and why, with `effective_from` derived rather
than supplied so it cannot be backdated onto the port that exposed the drop, and
with `ruled_by: agent` refused. Un-retirement is another row, never an edit, so a
surface Instagram brings back is expected again without the record losing the
fact that it was once doubted. What is still refused is the thing this file
exists to prevent: a bar lowered by editing one number in one line of JSON.

**A set, not a number.** `4 -> 3` says a port got worse. `set_app_context is no
longer release-ready` says which thing to go and look at, and the two are not the
same message. It also survives the hook set changing size, and a set is the only form that can
say *which* hook without a reader going to look it up.

(An earlier draft of this argued from the corpus: "439 carries 10 hook ids and
440 carries 7". That was false, and false in an instructive way -- 439's extra
three were `install_probe_long_click`, `replace_probe_endpoint` and
`set_probe_context`, fixture hooks that `tests/test_claim_attribution` had
written into the committed `manifest/static_evidence/439.jsonl` because the
driver had no way to point its evidence writes anywhere else. The real sets are
both 7. The argument for a set stands on its own; the evidence for it did not.)

===============================================================================
  WHAT THIS REFUSES TO DO
===============================================================================

**It computes no readiness of its own.** Both sides come from
`final_report.build_report`, which gets them from `EvidenceLedger`. A second
opinion on readiness would agree with the first until one of them was edited.

**It will not treat a rise as good news yet.** A hook that starts passing cannot
become release-ready in the port that fixes it -- `differential` is one of the
three required kinds and it needs a passing baseline to regress from, so a
newly-working hook reads `inconclusive / baseline_not_a_pass` for one version and
only lands the version after. **The count can fall in one port and can only rise
after two.** A gain here is therefore reported as unconfirmed, and the reader is
told what would confirm it.

**It distinguishes a hook that failed from a hook that vanished.** A hook in the
expectation with no claim at all on N is not an escalation -- `build_report`
never sees it, so it has no reasons to give. That is the loudest case, not the
quietest one: it is what removing a hook from the manifest looks like from here.

**It will not compare across a gap.** The previous version is the one immediately
before N *in the series*, not the newest one that happens to have files. Skipping
439 to compare 441 against 440 is right; skipping 440 to compare 441 against 439
would silently forgive whatever 440 lost.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .final_report import PortReport, ReportError, build_report, read_claims
from .retirement import RetirementError, retired_at
from .history import BASELINE_VERSION, HistoryError, _NUMERIC

__all__ = [
    "ExpectationError",
    "Standing",
    "Verdict",
    "Comparison",
    "evidence_files",
    "port_report",
    "standings",
    "versions_with_evidence",
    "compare",
    "sweep",
    "render",
    "render_sweep",
    "main",
]


class ExpectationError(RuntimeError):
    """Raised when an expectation cannot honestly be derived or checked."""


#: Exit codes. `1` is deliberately NOT used: `final_report` already exits 1 for
#: "incomplete", and incomplete is this project's *normal* state -- three hooks
#: have never passed a runtime probe on any version, so 441's honest best is 4 of
#: 7. A drop must not share an exit code with the condition that is true on every
#: successful port, or the gate that matters is invisible inside the one that
#: always fires.
EXIT_MET = 0
EXIT_REFUSED = 2
EXIT_DROPPED = 3


def evidence_files(
    root: Path | str, version: str, previous: str | None
) -> list[Path]:
    """The conventional durable evidence for one version.

    By convention and not by argument, because the argument is how the check gets
    quietly weakened: a caller that omits `runtime_evidence` gets a report in
    which no hook is release-ready, and comparing that to a full one manufactures
    a drop in every hook at once.
    """

    root = Path(root)
    files = [
        root / "manifest" / "static_evidence" / f"{version}.jsonl",
        root / "manifest" / "runtime_evidence" / f"{version}.jsonl",
    ]
    if previous is not None:
        pair = root / "manifest" / "differentials" / f"{previous}-{version}.jsonl"
        if pair.is_file():
            files.append(pair)
    return files


def versions_with_evidence(
    root: Path | str = ".", *, baseline: str = BASELINE_VERSION
) -> list[str]:
    """Versions from `baseline` forward that have any durable evidence, in order.

    The union of the static and runtime directories rather than either alone: a
    version part-way through a port has one and not the other, and it must still
    appear in the series so that the version after it is compared against the
    right predecessor.
    """

    root = Path(root)
    if not _NUMERIC.fullmatch(baseline):
        raise ExpectationError(f"baseline {baseline!r} is not a version number")
    found: set[str] = set()
    for name in ("static_evidence", "runtime_evidence"):
        directory = root / "manifest" / name
        if directory.is_dir():
            found |= {
                path.stem
                for path in directory.glob("*.jsonl")
                if _NUMERIC.fullmatch(path.stem)
            }
    # `key=int`: these are release numbers, and sorted as strings a series
    # containing 1000 would order it first and give every later version the wrong
    # predecessor. The same trap `history.series` documents.
    return sorted((v for v in found if int(v) >= int(baseline)), key=int)


def port_report(root: Path | str, version: str, previous: str | None) -> PortReport:
    """`final_report`'s answer for one version, from committed files only."""

    try:
        return build_report(version, read_claims(evidence_files(root, version, previous)))
    except ReportError as error:
        raise ExpectationError(f"{version}: {error}") from error


@dataclass(frozen=True)
class Standing:
    """One hook's release-readiness across the whole series.

    The record a human needs in order to tell a regression from a dormancy. It
    lived in `retirement` until that module was deleted, and it is here because
    every line of it is assembled from this module's own readers — `roster` is
    the consumer, and the per-hook view of readiness is the same question
    `compare` asks between two versions rather than a separate one.
    """

    hook_id: str
    #: Versions where every required post-build kind passed, in release order.
    release_ready_on: tuple[str, ...]
    #: Versions whose evidence could be read at all, in release order. A version
    #: absent from BOTH tuples was never computable — 439 has no static evidence,
    #: so no hook has a standing there, and reading that as a failure would make
    #: every hook look permanently broken.
    assessed_on: tuple[str, ...]

    @property
    def never_release_ready(self) -> bool:
        return not self.release_ready_on

    @property
    def last_release_ready(self) -> str | None:
        return self.release_ready_on[-1] if self.release_ready_on else None

    def dropped_at(self) -> str | None:
        """The first assessed version after the last good one, or None.

        `None` covers both "still passing" and "never passed", which are opposite
        situations — read `never_release_ready` alongside it rather than treating
        a null here as good news.
        """

        last = self.last_release_ready
        if last is None:
            return None
        after = [v for v in self.assessed_on if int(v) > int(last)]
        return after[0] if after else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook_id,
            "release_ready_on": list(self.release_ready_on),
            "assessed_on": list(self.assessed_on),
            "never_release_ready": self.never_release_ready,
            "last_release_ready": self.last_release_ready,
            "dropped_at": self.dropped_at(),
        }


def standings(
    root: Path | str = ".",
    *,
    baseline: str = BASELINE_VERSION,
) -> dict[str, Standing]:
    """Every hook's release-readiness across every assessable version.

    Assembled from `port_report`, which is `final_report`, which is the
    `EvidenceLedger` — the same answer the release gate reads, reached the same
    way. A second derivation of readiness here would agree with the first until
    one of them was edited.

    There used to be a `ceiling` beside `baseline`, capping the series so that a
    *retirement case* built at version N could still be re-derived after N+1 was
    ported. `retirement` is deleted, nothing else ever passed it, and a parameter
    with no caller is a branch no test can reach — so it went with its reason
    rather than being kept in case somebody wants it. `baseline` remains and is
    used: `roster` passes it.
    """

    root = Path(root)
    series = versions_with_evidence(root, baseline=baseline)
    ready_by_version: dict[str, set[str]] = {}
    seen_by_version: dict[str, set[str]] = {}
    for index, version in enumerate(series):
        previous = series[index - 1] if index else None
        # Absent and unreadable are different facts, and conflating them is how a
        # corrupt corpus reads as a quiet one. A version whose evidence file does
        # not exist is skipped: 439 has runtime evidence and no static evidence,
        # because `static_verified` had no producer until 440, so its readiness is
        # unknowable rather than zero — recording it as zero would make every hook
        # look like it had been failing since the start of the series.
        #
        # A file that exists and cannot be read is a REFUSAL. This was found the
        # only way it could be: a test corpus with the wrong `producer` on every
        # runtime claim was rejected by the ledger, every version was skipped, and
        # the result was the cheerful "every assessed hook is release-ready".
        missing = [
            path for path in evidence_files(root, version, previous) if not path.is_file()
        ]
        try:
            report = port_report(root, version, previous)
        except ExpectationError:
            if missing:
                continue
            raise
        ready_by_version[version] = set(report.ready)
        seen_by_version[version] = set(report.hooks)

    out: dict[str, Standing] = {}
    for hook in sorted({h for hooks in seen_by_version.values() for h in hooks}):
        out[hook] = Standing(
            hook_id=hook,
            release_ready_on=tuple(
                v for v in series if hook in ready_by_version.get(v, ())
            ),
            assessed_on=tuple(v for v in series if hook in seen_by_version.get(v, ())),
        )
    return out


@dataclass(frozen=True)
class Verdict:
    """One hook's fate between two ports."""

    hook_id: str
    #: `held`, `dropped`, `gained` or `retired`. `retired` is the only one that is
    #: a decision rather than a measurement: a human recorded that the project
    #: stops expecting this hook. Without it a retired hook that still passes reads
    #: as `gained` and the report congratulates the port on a hook it gave up on.
    state: str
    #: Why the ledger escalated it on this version, when it did. Empty for a hook
    #: that has no claim at all -- and that emptiness is itself the finding.
    reasons: tuple[str, ...] = ()

    @property
    def vanished(self) -> bool:
        """Dropped with no claim on this version at all."""

        return self.state == "dropped" and not self.reasons

    def to_dict(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook_id,
            "state": self.state,
            "reasons": list(self.reasons),
            "vanished": self.vanished,
        }


@dataclass(frozen=True)
class Comparison:
    """What version `version` owed version `previous`, and what it delivered."""

    version: str
    previous: str
    expected: tuple[str, ...]
    actual: tuple[str, ...]
    verdicts: tuple[Verdict, ...]

    def _in(self, *states: str) -> tuple[str, ...]:
        return tuple(v.hook_id for v in self.verdicts if v.state in states)

    @property
    def dropped(self) -> tuple[str, ...]:
        return self._in("dropped")

    @property
    def held(self) -> tuple[str, ...]:
        return self._in("held")

    @property
    def gained(self) -> tuple[str, ...]:
        return self._in("gained")

    @property
    def met(self) -> bool:
        return not self.dropped

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "version": self.version,
            "previous": self.previous,
            "expected": list(self.expected),
            "actual": list(self.actual),
            "met": self.met,
            "dropped": list(self.dropped),
            "held": list(self.held),
            "gained": list(self.gained),
            "verdicts": [v.to_dict() for v in self.verdicts],
        }


def compare(
    root: Path | str = ".",
    *,
    version: str,
    previous: str | None = None,
    baseline: str = BASELINE_VERSION,
) -> Comparison:
    """Derive `version`'s expectation from its predecessor and check it."""

    root = Path(root)
    if not _NUMERIC.fullmatch(version):
        raise ExpectationError(f"{version!r} is not a version number")

    if previous is None:
        series = versions_with_evidence(root, baseline=baseline)
        earlier = [v for v in series if int(v) < int(version)]
        if not earlier:
            raise ExpectationError(
                f"{version} has no predecessor at or after {baseline}, so there is "
                "nothing to expect of it. The first version of a series establishes "
                "the bar; it cannot be measured against one"
            )
        previous = earlier[-1]
    elif not _NUMERIC.fullmatch(previous):
        raise ExpectationError(f"{previous!r} is not a version number")
    elif int(previous) >= int(version):
        raise ExpectationError(
            f"{previous} does not precede {version}. Comparing a port against a "
            "later one inverts the check: every hook the later one fixed reads as "
            "a drop"
        )

    before = port_report(root, previous, _predecessor(root, previous, baseline))
    # N's OWN evidence is assembled from N's TRUE predecessor, never from an
    # overridden one. A differential file is named for the pair it spans, so
    # `--version 442 --previous 440` used to look for `differentials/440-442`,
    # find nothing, and report 442 as having lost every hook — with the reason
    # `differential: no claim recorded`, which is true of a file that was never
    # supposed to exist and tells the reader nothing. The flag chooses whose bar
    # to measure against; it does not choose what this port measured.
    now = port_report(root, version, _predecessor(root, version, baseline))

    # Every hook that was ready, minus the ones a human has recorded a retirement
    # for. That subtraction is the ONLY way the bar comes down, and `retirement`
    # is where the rules that make it safe live: `effective_from` is derived so a
    # retirement cannot be backdated onto the port that exposed the drop, and an
    # agent may not rule. Un-retirement is another row, so a hook Instagram brings
    # back is expected again without the retirement being erased.
    expected = set(before.ready) - set(retired_at(version, root))
    actual = set(now.ready)
    reasons = {
        item["hook_id"]: tuple(item.get("reasons", ()))
        for item in now.escalations
    }

    retired = set(retired_at(version, root))
    verdicts: list[Verdict] = []
    for hook in sorted(set(before.ready) | actual):
        if hook in retired and hook in before.ready:
            # Retired FIRST, or a hook that was ready before and is still ready
            # falls through to `gained` and gets announced as "newly
            # release-ready" on the very port that stopped expecting it. It is
            # neither held nor gained: the project gave it up, and saying so is
            # the whole reason the state exists.
            verdicts.append(Verdict(hook, "retired"))
        elif hook in expected and hook in actual:
            verdicts.append(Verdict(hook, "held"))
        elif hook in expected:
            verdicts.append(Verdict(hook, "dropped", reasons.get(hook, ())))
        else:
            verdicts.append(Verdict(hook, "gained"))

    return Comparison(
        version=version,
        previous=previous,
        expected=tuple(sorted(expected)),
        actual=tuple(sorted(actual)),
        verdicts=tuple(verdicts),
    )


def _predecessor(root: Path, version: str, baseline: str) -> str | None:
    """The version before `version` in the series, or None at the baseline.

    Needed to assemble the *previous* port's own evidence: its differential file
    is named for the pair, so reading 440 means knowing 439 came before it.
    """

    earlier = [
        v for v in versions_with_evidence(root, baseline=baseline) if int(v) < int(version)
    ]
    return earlier[-1] if earlier else None


def sweep(
    root: Path | str = ".", *, baseline: str = BASELINE_VERSION
) -> tuple[list[Comparison], list[tuple[str, str]]]:
    """Check every consecutive pair that can be checked; say what was skipped.

    Returns the comparisons and the pairs that could not be made, each with a
    reason. **Both halves are the result.** A sweep that returned only what it
    managed to check would pass an empty corpus, and a check that cannot fail is
    the shape this project has shipped more than once -- see
    `absence-assertions-need-positive-controls`. A caller asserting the
    comparisons should also assert it got some.
    """

    root = Path(root)
    series = versions_with_evidence(root, baseline=baseline)
    comparisons: list[Comparison] = []
    skipped: list[tuple[str, str]] = []
    for previous, version in zip(series, series[1:]):
        try:
            comparisons.append(
                compare(
                    root,
                    version=version,
                    previous=previous,
                    baseline=baseline,
                )
            )
        except ExpectationError as error:
            # A port mid-flight is the ordinary case: the driver publishes static
            # evidence at build time and the device session adds runtime evidence
            # hours later, so between the two there is a version with half a
            # corpus. Skipping it is right; skipping it silently is not.
            skipped.append((f"{previous} -> {version}", str(error)))
    return comparisons, skipped


def render(comparison: Comparison) -> str:
    lines = [
        f"EXPECTATION  {comparison.previous} → {comparison.version}",
        "=" * 60,
        "",
        f"  expected release-ready   {len(comparison.expected)}"
        f"   (derived from {comparison.previous}, not declared)",
        f"  actually release-ready   {len(comparison.actual)}",
        "",
    ]

    if comparison.dropped:
        lines.append(f"  *** {len(comparison.dropped)} HOOK(S) DROPPED ***")
        lines.append("")
        for verdict in comparison.verdicts:
            if verdict.state != "dropped":
                continue
            lines.append(f"    ✗ {verdict.hook_id}")
            if verdict.vanished:
                # The loudest case gets the longest sentence. A hook with no claim
                # on this version was not measured and found wanting; it was not
                # measured. From here that is indistinguishable from someone
                # having deleted it, which is exactly why it must not read as a
                # quieter failure than a regression.
                lines.append(
                    f"        NO CLAIM AT ALL on {comparison.version}. It was "
                    f"release-ready on {comparison.previous} and this port has no "
                    "evidence about it whatsoever — it was removed from the "
                    "manifest, or its evidence was never published."
                )
            else:
                for reason in verdict.reasons:
                    lines.append(f"        {reason}")
        lines.append("")
        lines.append(
            "  Read the reasons before the count. A `differential` verdict of "
            "failed/regressed is a real"
        )
        lines.append(
            "  regression in this port; inconclusive/no_current means the hook was "
            "not measured, and the"
        )
        lines.append("  thing to fix is the device session, not the hook.")
        lines.append("")
        lines.append(
            "  The bar comes down ONLY through a recorded retirement. It is otherwise "
            "derived from the previous port's"
        )
        lines.append(
            "  own evidence every time it is asked for, so the only thing that "
            "clears this is the hook"
        )
        lines.append("  passing again.")
    else:
        lines.append(
            f"  Expectation met — all {len(comparison.expected)} hook(s) that were "
            f"release-ready on {comparison.previous} still are."
        )
        for hook in comparison.held:
            lines.append(f"    ✓ {hook}")

    if comparison.gained:
        lines.append("")
        lines.append(f"  Newly release-ready ({len(comparison.gained)}) — UNCONFIRMED:")
        for hook in comparison.gained:
            lines.append(f"    + {hook}")
        # Stated every time, not only when it looks surprising. A hook that starts
        # working cannot become release-ready in the port that fixes it, so the
        # first version a gain appears is the one where it is least verified.
        lines.append(
            "    A hook cannot become release-ready in the port that fixes it: "
            "`differential` needs a"
        )
        lines.append(
            "    passing baseline, so the fixing port reads "
            "inconclusive/baseline_not_a_pass and the gain"
        )
        lines.append(
            f"    lands the version after. These become the expectation for the "
            f"port after {comparison.version};"
        )
        lines.append("    that port is what confirms them.")

    return "\n".join(lines)


def render_sweep(
    comparisons: Iterable[Comparison], skipped: Iterable[tuple[str, str]]
) -> str:
    comparisons = list(comparisons)
    skipped = list(skipped)
    blocks = [render(item) for item in comparisons]
    if skipped:
        lines = ["", "NOT CHECKED", "=" * 60, ""]
        for pair, why in skipped:
            lines.append(f"  {pair}: {why}")
        lines.append("")
        lines.append(
            "  A version with half a corpus is the ordinary mid-port state — the "
            "driver publishes static"
        )
        lines.append(
            "  evidence at build time and the device session lands hours later. "
            "Listed so that a pair"
        )
        lines.append("  nobody checked is never mistaken for a pair that passed.")
        blocks.append("\n".join(lines))
    if not comparisons:
        blocks.append(
            "\nNo pair could be compared. That is not a pass — it is the absence of "
            "a check."
        )
    return "\n\n".join(blocks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument(
        "--version",
        help="the port to check. Omit to sweep every consecutive pair in the series",
    )
    parser.add_argument(
        "--previous",
        help="whose bar to measure against, defaulting to the version immediately "
        "before this one in the series. Rarely right: skipping one forgives "
        "whatever it lost. It does NOT change which evidence this port is read "
        "from — that is always this port's own",
    )
    parser.add_argument("--baseline", default=BASELINE_VERSION)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.previous and not args.version:
        print(
            "refused: --previous needs --version. A sweep compares consecutive "
            "pairs and has no single predecessor to override",
            file=sys.stderr,
        )
        return EXIT_REFUSED

    try:
        if args.version:
            comparisons = [
                compare(
                    args.root,
                    version=args.version,
                    previous=args.previous,
                    baseline=args.baseline,
                )
            ]
            skipped: list[tuple[str, str]] = []
        else:
            comparisons, skipped = sweep(args.root, baseline=args.baseline)
    # `ValueError` and `OSError` beside the module's own error, and the reason is
    # NOT the obvious one: `--baseline nope` never reaches `int()`, because
    # `versions_with_evidence` guards it and raises `ExpectationError` first. The
    # live path is that `\d+` matches a number of ANY length while CPython
    # refuses to parse an int past 4300 digits, so `--version <4301 digits>`
    # passes the regex and raises `ValueError` from the comparison. `OSError` is
    # an unreadable manifest directory, from the glob. Both left as tracebacks;
    # see `a-refusal-channel-is-only-a-channel-if-everything-uses-it`.
    except (ExpectationError, HistoryError, RetirementError, ValueError, OSError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return EXIT_REFUSED

    if args.json:
        # An OBJECT, not the bare list this used to print. `sweep` returns the
        # skipped pairs precisely so that "a pair nobody checked is never
        # mistaken for a pair that passed", and `render_sweep` prints a NOT
        # CHECKED block — but the machine-readable form, which is the one a
        # release script would actually gate on, had no such field. A mid-port
        # 442 with static evidence and no runtime file printed one comparison and
        # exit 0, and nothing in the output mentioned 442 at all. The human form
        # was honest and the automatable form was not, which is the wrong way
        # round.
        print(json.dumps(
            {
                "schema_version": 1,
                "comparisons": [item.to_dict() for item in comparisons],
                "not_checked": [
                    {"pair": pair, "reason": why} for pair, why in skipped
                ],
            },
            indent=2,
        ))
    else:
        print(render_sweep(comparisons, skipped))

    if not comparisons:
        print(
            "refused: no pair could be compared, so nothing was checked",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    return EXIT_DROPPED if any(not item.met for item in comparisons) else EXIT_MET


if __name__ == "__main__":
    raise SystemExit(main())

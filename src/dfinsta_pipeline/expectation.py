"""What this port owes the last one, and what it must not quietly lose.

    python -m dfinsta_pipeline.expectation --version 441

`final_report` says how many hooks a port can show to be release-ready.
`history` prints that number next to the previous versions'. Neither *fails* when
it falls, and a count that only ever gets printed is a count nobody notices
moving: 441 reads 4 of 7, and the whole of that sentence a reader keeps is "4".
If 442 reads 3 of 7 the report is equally calm about it.

So this is the assertion the other two deliberately refuse to make —

    every hook that was release-ready on N-1 is release-ready on N,
    unless a human recorded a decision to retire it.

===============================================================================
  DERIVED, NEVER DECLARED
===============================================================================

There is no expected count in this file, in a config file, or on the command
line, and adding one would undo the point. A declared expectation has exactly one
repair when it fails, it takes one character, and it is indistinguishable in a
diff from a legitimate change: edit 4 to 3. The expectation is instead recomputed
from the previous version's committed evidence every time it is asked for, which
leaves precisely two ways to lower it — make a hook pass again, or record a
retirement that names a human and a reason.

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
from .history import BASELINE_VERSION, HistoryError, _NUMERIC

__all__ = [
    "ExpectationError",
    "Retirement",
    "Verdict",
    "Comparison",
    "RETIREMENTS",
    "read_retirements",
    "retirements_in_force",
    "retirements_on_record",
    "evidence_files",
    "port_report",
    "versions_with_evidence",
    "retired_by",
    "compare",
    "sweep",
    "render",
    "render_sweep",
    "main",
]


class ExpectationError(RuntimeError):
    """Raised when an expectation cannot honestly be derived or checked."""


#: Where a recorded retirement lives. Append-only; see the directory README.
RETIREMENTS = Path("manifest") / "retirements.jsonl"

#: Exit codes. `1` is deliberately NOT used: `final_report` already exits 1 for
#: "incomplete", and incomplete is this project's *normal* state -- three hooks
#: have never passed a runtime probe on any version, so 441's honest best is 4 of
#: 7. A drop must not share an exit code with the condition that is true on every
#: successful port, or the gate that matters is invisible inside the one that
#: always fires.
EXIT_MET = 0
EXIT_REFUSED = 2
EXIT_DROPPED = 3


def _blank(value: Any) -> bool:
    """Absent, null, or whitespace — and NOT merely falsy.

    `not (value or "")` reads a JSON `0` as missing. No field is numeric today,
    but `effective_from` is a version number that a producer could reasonably
    write unquoted, and "your retirement is missing effective_from" would be a
    lie about a row that has one.
    """

    return value is None or not str(value).strip()


@dataclass(frozen=True)
class Retirement:
    """A human's recorded decision to stop expecting a hook.

    Every field is required, and that is the mechanism rather than the paperwork.
    The failure this whole module exists to prevent is a quiet edit that lowers
    the bar, so the one escape hatch is made expensive: retiring a hook costs a
    permanent row naming who ruled, which decision it was, and why.
    """

    hook_id: str
    effective_from: str
    decision_id: str
    ruled_by: str
    rationale: str
    recorded_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "hook_id": self.hook_id,
            "effective_from": self.effective_from,
            "decision_id": self.decision_id,
            "ruled_by": self.ruled_by,
            "rationale": self.rationale,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Retirement":
        if data.get("schema_version") != 1:
            raise ExpectationError(
                f"unsupported retirement schema {data.get('schema_version')!r}"
            )
        missing = [
            key
            for key in ("hook_id", "effective_from", "decision_id", "ruled_by", "rationale")
            if _blank(data.get(key))
        ]
        if missing:
            raise ExpectationError(
                f"retirement is missing {', '.join(missing)}. A retirement that does "
                "not say who ruled and why is a lowered bar with no author"
            )
        # Only a human retires a hook. An agent may investigate and draft one --
        # that is the whole design of the decision gate -- but if a proposer could
        # also *sign* it, the cheapest way past a red build would be for the thing
        # being measured to rule that the measurement no longer applies.
        if str(data["ruled_by"]).strip().lower() == "agent":
            raise ExpectationError(
                f"{data['hook_id']}: ruled_by is 'agent'. A hook is retired by a "
                "human; an agent drafts the case for it"
            )
        if not _NUMERIC.fullmatch(str(data["effective_from"])):
            raise ExpectationError(
                f"{data['hook_id']}: effective_from "
                f"{data['effective_from']!r} is not a version number"
            )
        return cls(
            hook_id=str(data["hook_id"]),
            effective_from=str(data["effective_from"]),
            decision_id=str(data["decision_id"]),
            ruled_by=str(data["ruled_by"]),
            rationale=str(data["rationale"]),
            recorded_at=str(data.get("recorded_at") or ""),
        )


def read_retirements(
    root: Path | str = ".", *, path: Path | str | None = None
) -> dict[str, Retirement]:
    """Every recorded retirement, keyed by hook.

    A missing file means none have been recorded, which is the state today and is
    not an error. A file that exists and cannot be parsed **is** an error: the one
    thing worse than no retirement record is one that is silently skipped, because
    then a malformed row reads as "this hook was never retired" and the port fails
    for a reason nobody can act on.
    """

    location = Path(path) if path is not None else Path(root) / RETIREMENTS
    if not location.is_file():
        return {}
    out: dict[str, Retirement] = {}
    for number, line in enumerate(
        location.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ExpectationError(f"{location}:{number}: {error}") from error
        # Type-checked before `.get`. `json.loads("null")`, `"3"` and `"[1, 2]"`
        # all parse, so the line above cannot refuse them, and `.get` on the
        # result raised a bare `AttributeError` — which `sweep` does not treat as
        # a skip and `main` does not catch, so the tool left as a traceback. Two
        # ways for a row to be malformed and only one of them findable. `history`
        # closed this exact gap in `_rows`/`_field`; this is the third module to
        # ship it.
        if not isinstance(row, dict):
            raise ExpectationError(
                f"{location}:{number}: expected a JSON object, got "
                f"{type(row).__name__}"
            )
        record = row.get("record", row)
        if not isinstance(record, dict):
            raise ExpectationError(
                f"{location}:{number}: 'record' is {type(record).__name__}, not an "
                "object"
            )
        try:
            retirement = Retirement.from_dict(record)
        except ExpectationError as error:
            raise ExpectationError(f"{location}:{number}: {error}") from error
        seen = out.get(retirement.hook_id)
        # EARLIEST wins, not latest. The ledger's usual rule is "a later claim
        # supersedes", and it is wrong here: appending a second row for the same
        # hook with a later `effective_from` would *un-retire* it for the versions
        # in between and turn a permanent record into an editable one.
        if seen is None or int(retirement.effective_from) < int(seen.effective_from):
            out[retirement.hook_id] = retirement
    return out


def retired_by(
    version: str,
    retirements: dict[str, Retirement],
    *,
    withdrawn: dict[tuple[str, str], Any] | None = None,
) -> dict[str, Retirement]:
    """Those in force when reporting on `version`.

    Keyed on `effective_from`, so a retirement ruled for 442 does not reach back
    and excuse a hook that had already stopped passing on 441.

    `withdrawn` maps `(original_decision_id, hook_id)` to a recorded reversal, and
    removes the retirement from force. It is a **parameter rather than a read**
    because whether a withdrawal applies is itself version-dependent, and this
    function is handed a version. Callers should not assemble it by hand: use
    `retirements_in_force`, which is the one place that reads both files.
    """

    if not _NUMERIC.fullmatch(version):
        raise ExpectationError(f"{version!r} is not a version number")
    withdrawn = withdrawn or {}
    return {
        hook: item
        for hook, item in retirements.items()
        if int(item.effective_from) <= int(version)
        and (item.decision_id, hook) not in withdrawn
    }


def retirements_on_record(root: Path | str = ".") -> dict[str, Retirement]:
    """Retirements that have not been withdrawn, at ANY version.

    **A different question from `retirements_in_force`, and conflating them was a
    real regression.** That one asks "was this hook retired *as of version N*" and
    is what the expectation needs. This one asks "is there an outstanding
    retirement decision for this hook at all", which is what `candidates`,
    `build_case` and `publish` need: they are deciding whether to ask a human
    again, not what a port owed.

    Using the version-scoped reader for those broke them silently. A retirement
    built from a case at version V always takes effect at V+1, so
    `retirements_in_force(V)` can never contain it — and a hook that had just been
    retired was offered for retirement all over again.
    """

    withdrawals = _withdrawn_retirements(root)
    return {
        hook: item
        for hook, item in read_retirements(root).items()
        if (item.decision_id, hook) not in withdrawals
    }


def _withdrawn_retirements(root: Path | str) -> dict[tuple[str, str], Any]:
    from .reversal import withdrawn  # noqa: PLC0415

    return withdrawn("retirement", root)


def retirements_in_force(
    version: str, root: Path | str = "."
) -> dict[str, Retirement]:
    """Retirements that actually apply at `version`, withdrawals honoured.

    **One function, because two files decide this.** A caller that read
    `read_retirements` alone would honour a retirement a human had already
    withdrawn — and it would do so silently, which is the direction that keeps a
    hook un-expected forever. Every consumer of "is this hook retired" goes
    through here.
    """

    from .reversal import withdrawn_at  # noqa: PLC0415  (reversal imports nothing here)

    return retired_by(
        version,
        read_retirements(root),
        withdrawn=withdrawn_at(version, "retirement", root),
    )


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
class Verdict:
    """One hook's fate between two ports."""

    hook_id: str
    #: `held`, `dropped`, `gained`, `retired` or `retired_still_passing`.
    state: str
    #: Why the ledger escalated it on this version, when it did. Empty for a hook
    #: that has no claim at all -- and that emptiness is itself the finding.
    reasons: tuple[str, ...] = ()
    retirement: Retirement | None = None

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
            "retirement": self.retirement.to_dict() if self.retirement else None,
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
    retirements: dict[str, Retirement] | None = None,
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

    if retirements is None:
        # `retirements_in_force`, not `read_retirements`: a withdrawn retirement
        # must stop excusing the hook, and that is version-dependent.
        in_force = retirements_in_force(version, root)
    else:
        in_force = retired_by(version, retirements)

    before = port_report(root, previous, _predecessor(root, previous, baseline))
    # N's OWN evidence is assembled from N's TRUE predecessor, never from an
    # overridden one. A differential file is named for the pair it spans, so
    # `--version 442 --previous 440` used to look for `differentials/440-442`,
    # find nothing, and report 442 as having lost every hook — with the reason
    # `differential: no claim recorded`, which is true of a file that was never
    # supposed to exist and tells the reader nothing. The flag chooses whose bar
    # to measure against; it does not choose what this port measured.
    now = port_report(root, version, _predecessor(root, version, baseline))

    expected = set(before.ready) - set(in_force)
    actual = set(now.ready)
    reasons = {
        item["hook_id"]: tuple(item.get("reasons", ()))
        for item in now.escalations
    }

    verdicts: list[Verdict] = []
    for hook in sorted(set(before.ready) | actual):
        retirement = in_force.get(hook)
        if retirement is not None:
            # Reported either way. A hook retired while it is still passing is not
            # an error, but it is worth a human seeing: the case for retiring it
            # was probably made when it was not.
            state = "retired_still_passing" if hook in actual else "retired"
            verdicts.append(Verdict(hook, state, reasons.get(hook, ()), retirement))
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
    # Parsed once, up front, and the result deliberately discarded. The *values*
    # must not be hoisted — passing them to `compare` sends it down the branch
    # that skips withdrawals, which made `expectation --version 446` and the
    # default sweep give opposite answers from the same three files. But the
    # *parse* must happen out here: inside the loop, a malformed retirements file
    # raises `ExpectationError`, which the per-pair handler below treats as "this
    # pair is mid-port, skip it", and a corrupt store reads as "nothing to check".
    # That is `a-skip-for-absent-swallowed-unreadable` a second time, in the
    # module where the lesson was written.
    read_retirements(root)
    _withdrawn_retirements(root)
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
            "  To lower the bar legitimately, record a retirement in "
            f"{RETIREMENTS} — naming a human,"
        )
        lines.append(
            "  a decision and a reason. There is deliberately no other way; the "
            "expectation is derived."
        )
    else:
        lines.append(
            f"  Expectation met — all {len(comparison.expected)} hook(s) that were "
            f"release-ready on {comparison.previous} still are."
        )
        for hook in comparison.held:
            lines.append(f"    ✓ {hook}")

    retired = [v for v in comparison.verdicts if v.state.startswith("retired")]
    if retired:
        lines.append("")
        lines.append(f"  Retired, so not expected ({len(retired)}):")
        for verdict in retired:
            item = verdict.retirement
            assert item is not None
            note = " — STILL PASSING" if verdict.state == "retired_still_passing" else ""
            lines.append(f"    · {verdict.hook_id}{note}")
            lines.append(
                f"        {item.ruled_by} ruled at {item.effective_from} "
                f"({item.decision_id}): {item.rationale}"
            )

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
    except (ExpectationError, HistoryError, ValueError, OSError) as error:
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

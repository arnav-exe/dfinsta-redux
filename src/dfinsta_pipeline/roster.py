"""Every hook, and everything known about it. The per-hook view.

    python -m dfinsta_pipeline.roster

`history` reads the project **per version** — agent invocations, hooks passed,
selectivity margins, one column per port. `expectation` reports what a port
*lost*. `final_report` says what one port can be shown to have achieved. Nothing
answered the question a human actually asks first:

    what is this hook, and when did it last do anything?

So a hook that has quietly not executed for three versions is visible only to
somebody who already suspected it, and the answer to "should we still be carrying
this?" had to be assembled by hand from four directories.

===============================================================================
  A VIEW, NOT AN ALARM
===============================================================================

This is deliberately complete and deliberately silent. It prints every hook every
time, including the healthy ones and including the ones whose fate was already
decided — because a table you read is cheap to make exhaustive, while an alert
that fires every port is one you learn to skip, and a red check nobody reads has
stopped working.

That completeness is what lets it stay quiet about settled questions. Three hooks
have never passed a runtime probe on any version and **the owner decided on
2026-08-01 to keep all three**; they appear here with their status and their
recorded decision beside them, as state rather than as an accusation. Nothing is
re-litigated because nothing is being asked.

===============================================================================
  WHAT IT REFUSES TO DO
===============================================================================

**It computes no verdicts of its own.** Release-readiness comes from
`expectation.standings`, which is `final_report`, which is the `EvidenceLedger` —
the same answer the release gate reads, reached the same way. A second opinion
here would agree with the first until one was edited.

**It separates "did not run" from "was not measured".** A hook with no runtime
claim on a version is `—`; a hook measured and silent is `·`; a hook that ran is
`✓`. Collapsing the first two is how "we never looked" comes to read as "it is
broken", which is the mistake that made the first differential compare 2 of 7.

**It shows a recorded decision where one exists, and its absence where one does
not.** Only one hook carries a written `DECISION` note today
(`replace_reels_discover_endpoint.probe.note`, which also carries a REVISIT
trigger that fires by itself). The other two dormant hooks were decided in
conversation and the decision was never written into the manifest. That gap is
worth seeing, so this prints it rather than smoothing over it.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from .expectation import (
    ExpectationError,
    standings,
    versions_with_evidence,
)
from .history import BASELINE_VERSION

__all__ = [
    "RosterError",
    "HookLife",
    "roster",
    "render",
    "main",
]


class RosterError(RuntimeError):
    """Raised when the roster cannot honestly be assembled."""

#: Printed per hook per version. `—` and `·` are different facts and the legend
#: says so on every run, because the distinction is the one readers collapse.
RAN = "✓"
SILENT = "·"
UNMEASURED = "—"


@dataclass(frozen=True)
class HookLife:
    """One hook, across every version with committed evidence."""

    hook_id: str
    intent: str
    tier: str
    status: str
    #: Versions whose `runtime_probe` for this hook passed — it executed.
    ran_on: tuple[str, ...]
    #: Versions that carry any `runtime_probe` for it, passed or not.
    measured_on: tuple[str, ...]
    #: From the evidence ledger, via `standings`.
    release_ready_on: tuple[str, ...]
    assessed_on: tuple[str, ...]
    #: A written decision found in the manifest, if any.
    note: str = ""

    @property
    def last_ran(self) -> str | None:
        return self.ran_on[-1] if self.ran_on else None

    @property
    def never_ran(self) -> bool:
        return not self.ran_on

    @property
    def dormant_and_undecided(self) -> bool:
        """Never executed, and nothing written down about why we keep it.

        The one thing this view is opinionated about. A hook that has never run is
        not a defect — it can be dormant by server config, which is exactly why
        three of them are deliberately kept. A hook that has never run *and* has
        no recorded reason is a decision nobody can find.

        A third exemption used to sit here: a hook with a recorded *retirement*
        was decided by definition. That record no longer exists — a hook is
        active or it is not in the manifest — so `status` and `note` are all
        there is, and a dormant hook has to carry its reasoning in writing.
        """

        return self.never_ran and not self.note

    def to_dict(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook_id,
            "intent": self.intent,
            "tier": self.tier,
            "status": self.status,
            "ran_on": list(self.ran_on),
            "measured_on": list(self.measured_on),
            "release_ready_on": list(self.release_ready_on),
            "assessed_on": list(self.assessed_on),
            "last_ran": self.last_ran,
            "note": self.note,
            "dormant_and_undecided": self.dormant_and_undecided,
        }


def _decision_note(entry: dict[str, Any]) -> str:
    """A written decision anywhere in a hook's manifest entry.

    Searched rather than read from a named field, because there is no named
    field: the one decision recorded today lives in `probe.note`, and the next
    one will land wherever its author finds natural. Surfacing prose by pattern is
    a poor substitute for a schema — and printing it is what makes the absence of
    a schema visible instead of comfortable.
    """

    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str) and "DECISION" in value:
            found.append(value)

    walk(entry)
    return found[0] if found else ""


def _runtime(root: Path, versions: Sequence[str]) -> dict[str, dict[str, bool]]:
    """`hook -> {version: it ran}` from the committed runtime evidence.

    A hook absent from a version's file is absent from its inner dict, which is
    how `—` (never measured) stays distinguishable from `·` (measured, silent).
    """

    out: dict[str, dict[str, bool]] = {}
    for version in versions:
        path = root / "manifest" / "runtime_evidence" / f"{version}.jsonl"
        if not path.is_file():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise RosterError(f"{path}:{number}: {error}") from error
            if not isinstance(row, dict):
                raise RosterError(
                    f"{path}:{number}: expected a JSON object, got {type(row).__name__}"
                )
            record = row.get("record", row)
            if not isinstance(record, dict):
                raise RosterError(
                    f"{path}:{number}: 'record' is {type(record).__name__}, not an object"
                )
            try:
                hook, verdict = record["hook_id"], record["verdict"]
            except KeyError as error:
                raise RosterError(f"{path}:{number}: claim has no hook_id/verdict") from error
            # `runtime_probe` ONLY. This read every row in the file, so a
            # `differential` claim with `verdict: "passed"` would have counted as
            # the hook executing — `presence-is-not-execution` with the evidence
            # kind ignored. Latent today because these files carry one kind, and
            # one clause away from not being.
            if record.get("kind") != "runtime_probe":
                continue
            # `or` and not `=`: a hook measured twice on one version ran if ANY
            # claim passed. The ledger's retry guard is what judges whether
            # re-measuring until green was legitimate; that is not this view's
            # job, and overwriting would make the answer depend on file order.
            slot = out.setdefault(hook, {})
            slot[version] = slot.get(version, False) or verdict == "passed"
    return out


def roster(
    root: Path | str = ".", *, baseline: str = BASELINE_VERSION
) -> tuple[list[HookLife], list[str]]:
    """Every hook in the manifest, and the versions the view spans."""

    root = Path(root)
    manifest_path = root / "manifest" / "hooks.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RosterError(f"{manifest_path}: {error}") from error

    versions = versions_with_evidence(root, baseline=baseline)
    if not versions:
        raise RosterError(f"no committed evidence at or after {baseline}")

    try:
        standing = standings(root, baseline=baseline)
    except ExpectationError as error:
        raise RosterError(str(error)) from error
    ran = _runtime(root, versions)

    hooks = manifest.get("hooks") if isinstance(manifest, dict) else None
    if not isinstance(hooks, list):
        # Checked rather than indexed: a manifest with no `hooks` produced a bare
        # `KeyError` and exit 1 where the contract is `refused:` and exit 2.
        raise RosterError(f"{manifest_path} has no 'hooks' array")

    lives: list[HookLife] = []
    for entry in hooks:
        if not isinstance(entry, dict) or "hook_id" not in entry:
            raise RosterError(f"{manifest_path}: a hook entry has no hook_id")
        hook = entry["hook_id"]
        measured = ran.get(hook, {})
        found = standing.get(hook)
        lives.append(
            HookLife(
                hook_id=hook,
                intent=str(entry.get("intent", "")),
                tier=str(entry.get("tier", "")),
                status=str(entry.get("status", "active")),
                ran_on=tuple(v for v in versions if measured.get(v)),
                measured_on=tuple(v for v in versions if v in measured),
                release_ready_on=found.release_ready_on if found else (),
                assessed_on=found.assessed_on if found else (),
                note=_decision_note(entry),
            )
        )
    return lives, versions


def render(lives: Iterable[HookLife], versions: Sequence[str]) -> str:
    lives = list(lives)
    if not lives:
        # Refused upstream rather than rendered empty; this is the caller-filtered
        # case, and a printed "no hooks" for a manifest holding seven would read
        # as a fact about the project.
        raise RosterError("no hooks to render")

    versions = list(versions)
    if not versions:
        # `render` is in `__all__`, so this is reachable from outside `main`,
        # where `roster()` refuses first. `versions[0]` below would be an
        # IndexError, which is outside every caught tuple.
        raise RosterError("no versions to render")
    width = max(len(life.hook_id) for life in lives) + 2
    lines = [
        f"HOOK ROSTER   {versions[0]} → {versions[-1]}",
        "=" * 74,
        "",
        f"  {'hook':<{width}}" + "".join(f"{v:>6}" for v in versions)
        + "   last ran   release-ready",
    ]
    for life in lives:
        cells = ""
        for version in versions:
            if version in life.ran_on:
                cells += f"{RAN:>6}"
            elif version in life.measured_on:
                cells += f"{SILENT:>6}"
            else:
                cells += f"{UNMEASURED:>6}"
        ready = ", ".join(life.release_ready_on) or "—"
        lines.append(
            f"  {life.hook_id:<{width}}{cells}   {(life.last_ran or 'never'):>8}   {ready}"
        )

    lines += [
        "",
        f"  {RAN} ran   {SILENT} measured, did not run   {UNMEASURED} no evidence",
        "",
        "  Measured-and-silent is not broken. Instagram decides much of this "
        "server-side, so a hook",
        "  can be correct and simply never selected — which is why "
        "\"never executed on my device\"",
        "  is not \"dead\".",
        "",
        "  what each hook does",
    ]
    for life in lives:
        lines.append(f"    {life.hook_id}  [{life.tier}]")
        lines.append(f"        {life.intent}")

    inactive = [life for life in lives if life.status != "active"]
    if inactive:
        # From the manifest's own `status`, which is the only remaining word for
        # a hook the project has stopped carrying. Printed rather than filtered:
        # a hook nobody expects any more is exactly the row a reader needs to see
        # beside its evidence, not one hidden from the table.
        lines += ["", "  not active"]
        for life in inactive:
            lines.append(f"    {life.hook_id} — status {life.status}")

    decided = [life for life in lives if life.note]
    if decided:
        lines += ["", "  written decisions in the manifest"]
        for life in decided:
            # From the word DECISION onward, not the note's first sentence. The
            # note is a long technical narrative and its opening line is about
            # measurement method; printing that summarised the decision as
            # "Block-counting CANNOT measure this hook", which is true and is not
            # the decision.
            start = life.note.find("DECISION")
            excerpt = life.note[start:] if start >= 0 else life.note
            excerpt = " ".join(excerpt.split())
            lines.append(f"    {life.hook_id}")
            lines.append(f"        {excerpt[:180]}…")
            if "REVISIT" in life.note:
                lines.append(
                    "        Carries a REVISIT trigger that fires by itself — the "
                    "shape worth copying."
                )

    undecided = [life for life in lives if life.dormant_and_undecided]
    if undecided:
        lines += [
            "",
            f"  never executed, and nothing written down ({len(undecided)})",
        ]
        for life in undecided:
            lines.append(f"    {life.hook_id}")
        lines += [
            "    Not a defect and not a question — a hook can be dormant by server "
            "config, and these",
            "    were kept deliberately. But the reasoning lives outside the "
            "repository, so the next",
            "    reader cannot find it. The one hook that does carry a note also "
            "carries a REVISIT",
            "    trigger that fires by itself, which is the shape worth copying.",
        ]

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--baseline", default=BASELINE_VERSION)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        lives, versions = roster(args.root, baseline=args.baseline)
        if args.json:
            # An OBJECT carrying `versions`, and `render` called first so the JSON
            # path refuses whatever the human path refuses. Two defects in one
            # line before: an empty roster printed `[]` and exited 0 while the
            # table refused with exit 2, and dropping `versions` left a consumer
            # unable to tell `—` (never measured) from `·` (measured, silent) —
            # the exact three-way distinction this module exists for, reduced to
            # two in the form a script gates on. Third time this project has
            # shipped a machine-readable view quieter than its human one.
            render(lives, versions)
            text = json.dumps(
                {
                    "schema_version": 1,
                    "versions": list(versions),
                    "hooks": [life.to_dict() for life in lives],
                },
                indent=2,
            )
        else:
            text = render(lives, versions)
    except (RosterError, ExpectationError, ValueError, OSError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

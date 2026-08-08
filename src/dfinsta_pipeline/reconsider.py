"""Decisions that have started to look wrong, and the evidence that says so.

    python -m dfinsta_pipeline.reconsider --version 441

`reversal.py` gives a recorded decision a way back. Nothing tells a human they
should take it. So a block that turns out to be inert, or a retirement whose hook
comes back to life, stays in force until somebody happens to think about it —
which for a pipeline meant to run with minimal human effort is never.

This is the detection half. It **proposes nothing and decides nothing**: it reads
committed evidence and reports which recorded decisions no longer match it. A
human decides, and `reversal` records. An automatic reversal would let the system
quietly undo its own protections.

===============================================================================
  THE RULES, AND THE ONE DELIBERATELY LEFT OUT
===============================================================================

**`block_inert`** — the app enforces the block and the enforcing hook has never
executed. The rule exists because DFInsta has shipped four patches that were
applied, verified and never reached; a block nothing runs is a rule that only
looks like protection.

**`block_endpoint_absent`** — the endpoint no longer appears anywhere in the
app. Needs an index for the version, so it is skipped rather than guessed when
one is not supplied — and the skip is reported, because a rule that quietly did
not run is indistinguishable from a rule that found nothing.

**`retirement_returned`** — a retired hook has passed a runtime probe at or after
the version its retirement took effect. The hook is working and the project has
stopped expecting it.

**Not a rule: "declared blocked and not yet enforced".** Five of the six
endpoints ruled on 2026-08-08 are in exactly that state, and they are not suspect
— they are *outstanding implementation work*, which `rulings --audit` already
owns and already exits 1 for. Firing here as well would mean the gate's first
run proposed reconsidering decisions taken the day before, and a gate whose
opening move is a false alarm is one nobody answers twice. (The sixth,
`feed/timeline_stream/`, gained its guard the same day and so is a live subject
of `block_inert` — it stays out of the report because its hook executes, which
is the rule working rather than skipping.)

===============================================================================
  WHY IT NAMES THE DECISION, NOT JUST THE SUBJECT
===============================================================================

Every reconsideration carries the `original_decision_id` it questions, because
that is the key `reversal` withdraws by — paired with the subject, since one gate
decision covers every candidate in its docket. A report that named only the
endpoint would leave a human to find the decision themselves, and the pairing is
exactly the part that is easy to get wrong.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from .expectation import ExpectationError, retirements_on_record
from .reversal import ReversalError
from .history import BASELINE_VERSION, _NUMERIC
from .roster import HookLife, RosterError, roster

__all__ = [
    "ReconsiderError",
    "TRIGGERS",
    "Reconsideration",
    "reconsiderations",
    "render",
    "main",
]


class ReconsiderError(RuntimeError):
    """Raised when the evidence cannot honestly be read."""


#: Closed on purpose. A trigger nothing consumes would put a question in front of
#: a human that no recorded decision can answer.
TRIGGERS = ("block_inert", "block_endpoint_absent", "retirement_returned")


@dataclass(frozen=True)
class Reconsideration:
    """One recorded decision that no longer matches the evidence."""

    #: What kind of decision is being questioned: `block` or `retirement`. Matches
    #: `reversal.KINDS`, because this is what a withdrawal would name.
    kind: str
    subject: str
    original_decision_id: str
    trigger: str
    summary: str
    evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.trigger not in TRIGGERS:
            raise ReconsiderError(f"unknown trigger {self.trigger!r}")
        if not self.subject.strip():
            raise ReconsiderError("a reconsideration must name its subject")
        if not self.original_decision_id.strip():
            raise ReconsiderError(
                "a reconsideration must name the decision it questions — that is the "
                "key a withdrawal is recorded against"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "subject": self.subject,
            "original_decision_id": self.original_decision_id,
            "trigger": self.trigger,
            "summary": self.summary,
            "evidence": list(self.evidence),
        }


def _blocking_hook(lives: Sequence[HookLife], manifest: dict[str, Any], endpoint: str) -> str:
    """Which hook declares this endpoint, by the app's own spelling rule."""

    from .assessment import normalise  # noqa: PLC0415

    target = normalise(endpoint)
    for entry in manifest["hooks"]:
        for dep in entry.get("semantic_deps") or ():
            if normalise(dep) == target:
                return entry["hook_id"]
    return ""


def reconsiderations(
    root: Path | str = ".",
    *,
    version: str,
    baseline: str = BASELINE_VERSION,
    index_dir: Path | str | None = None,
) -> tuple[list[Reconsideration], list[str]]:
    """What no longer matches, and which rules could not be run.

    Returns `(found, not_run)`. **Both halves are the result.** A rule that needs
    an index and did not get one found nothing for a reason that has nothing to do
    with the evidence, and reporting only the findings would make "no index" read
    as "nothing wrong" — the same shape as a sweep that skips a pair silently.
    """

    root = Path(root)
    if not _NUMERIC.fullmatch(version):
        raise ReconsiderError(f"{version!r} is not a version number")

    try:
        lives, _ = roster(root, baseline=baseline)
    # `ReversalError` too: `retirements_on_record` reaches `reversal.read_reversals`,
    # so a corrupt `reversals.jsonl` left a traceback from both CLIs while the
    # parallel `retirements.jsonl` path refused cleanly.
    except (RosterError, ExpectationError, ReversalError) as error:
        raise ReconsiderError(str(error)) from error
    by_id = {life.hook_id: life for life in lives}

    manifest = json.loads((root / "manifest" / "hooks.json").read_text(encoding="utf-8"))

    from .rulings import read_store, unenforced_endpoints  # noqa: PLC0415

    from .rulings import BLOCKING_VERDICTS  # noqa: PLC0415

    rulings = read_store(root / "manifest" / "rulings.jsonl")
    # `BLOCKING_VERDICTS`, not `== "block"`. `offer_toggle` also writes the
    # endpoint into `semantic_deps` and also needs a `throwIfBlocked` guard, so an
    # inert `offer_toggle` block was invisible here while an identical `block`
    # reported. The feature policy makes `offer_toggle` the expected shape for
    # anything addictive, so that was the case about to become common.
    blocked_by_ruling = {
        ruling.candidate_id.split(":", 1)[-1]: ruling
        for ruling in rulings
        if ruling.verdict in BLOCKING_VERDICTS
    }

    found: list[Reconsideration] = []
    not_run: list[str] = []

    # ---- block_inert -----------------------------------------------------
    #
    # Only for endpoints the app ACTUALLY enforces. An endpoint still awaiting its
    # `throwIfBlocked` guard is outstanding work, not a suspect decision, and
    # `rulings --audit` already reports it.
    # BOTH paths under `root`. `unenforced_endpoints` defaults its source to
    # `dfinsta_source_439/...` relative to the process CWD, so passing only the
    # manifest made a `--root /elsewhere` run read that root's manifest against
    # *this repository's* app source — half-scoped, which is worse than unscoped
    # because it looks right. The same defect was found in `reversal`'s CLI.
    from .rulings import DEFAULT_SOURCE_PATH  # noqa: PLC0415

    source = root / DEFAULT_SOURCE_PATH
    try:
        unenforced = set(unenforced_endpoints(root / "manifest" / "hooks.json", source))
    except Exception as error:  # noqa: BLE001 - the app source may be absent
        # Not silently empty. An unreadable source means every block looks
        # enforced, so `block_inert` would judge endpoints whose enforcement
        # nobody checked — the rule still runs, and says what it could not see.
        unenforced = set()
        not_run.append(
            f"block_inert: could not read the app source at {source} ({error}); "
            "every declared block was treated as enforced"
        )

    for endpoint, ruling in sorted(blocked_by_ruling.items()):
        if endpoint in unenforced:
            continue
        hook_id = _blocking_hook(lives, manifest, endpoint)
        life = by_id.get(hook_id)
        if life is None or not life.never_ran:
            continue
        found.append(
            Reconsideration(
                kind="block",
                subject=endpoint,
                original_decision_id=ruling.decision_id,
                trigger="block_inert",
                summary=(
                    f"{endpoint} is blocked and enforced, but {hook_id} has never "
                    "executed on any measured version — the block cannot be doing "
                    "anything"
                ),
                evidence=(
                    f"ruled block on {ruling.recorded_at or 'an unrecorded date'}",
                    f"enforced by {hook_id}, which ran on: none",
                    f"measured on: {', '.join(life.measured_on) or 'no version'}",
                ),
            )
        )

    # ---- block_endpoint_absent -------------------------------------------
    if index_dir is None:
        not_run.append(
            "block_endpoint_absent: no --index given, so whether these endpoints "
            "still exist in the app was not checked"
        )
    else:
        from .hook_index import HookIndex, IndexUnusable  # noqa: PLC0415

        index_dir = Path(index_dir)
        # `HookIndex.load`, not the constructor. Building it directly skipped
        # every shape check `load` exists for — `hook_index` says so in as many
        # words: "Valid JSON of the wrong shape is still malformed. Without this
        # the first `.get` raises AttributeError past every handler."
        try:
            index = HookIndex.load(index_dir)
        except (OSError, json.JSONDecodeError, KeyError, IndexUnusable, ValueError) as error:
            raise ReconsiderError(f"{index_dir}: {error}") from error
        # Which decode this index was built from, carried into the evidence. The
        # rule cannot verify the index matches `version` — nothing here holds the
        # decode to compare against — and obfuscated descriptors are recycled
        # between versions, so a mismatched index would let this rule state
        # confidently that a live surface is gone. Naming the decode is what lets
        # a human catch that; claiming to have checked it would be worse.
        # Carried into each finding's evidence rather than into `rules_not_run`:
        # the rule DID run, and a caveat is not a skip. Mixing them made supplying
        # an index fail to remove the skip it was supplied to remove.
        built_from = index.header.get("decode_path", "an unrecorded decode")
        for endpoint, ruling in sorted(blocked_by_ruling.items()):
            # Every slash spelling, because the manifest normalises a leading
            # slash that the index keeps — reading only one spelling is how an
            # entire grouping went invisible on 440.
            spellings = {endpoint, f"/{endpoint}", endpoint.rstrip("/"), f"/{endpoint.rstrip('/')}"}
            if any(index.descriptors_with_literal(s) for s in spellings):
                continue

            found.append(
                Reconsideration(
                    kind="block",
                    subject=endpoint,
                    original_decision_id=ruling.decision_id,
                    trigger="block_endpoint_absent",
                    summary=(
                        f"{endpoint} appears in no class on {version} — the surface "
                        "it blocks is gone"
                    ),
                    evidence=(
                        f"ruled block on {ruling.recorded_at or 'an unrecorded date'}",
                        f"searched {index_dir} for: {', '.join(sorted(spellings))}",
                        # Both caveats, on every finding, because neither can be
                        # resolved from the index alone.
                        f"that index was built from {built_from}; nothing here proves "
                        f"it is {version}'s decode, and obfuscated descriptors are "
                        "recycled between versions",
                        "an empty index result is AMBIGUOUS — it can mean no class "
                        "holds the literal, or that the literal was never a candidate "
                        "for indexing. `hook_index` says the difference needs a scan "
                        "of the decode. CONFIRM AGAINST THE DECODE before withdrawing",
                    ),
                )
            )

    # ---- retirement_returned ---------------------------------------------
    for hook_id, retirement in sorted(retirements_on_record(root).items()):
        life = by_id.get(hook_id)
        if life is None:
            continue
        back = [v for v in life.ran_on if int(v) >= int(retirement.effective_from)]
        if not back:
            continue
        found.append(
            Reconsideration(
                kind="retirement",
                subject=hook_id,
                original_decision_id=retirement.decision_id,
                trigger="retirement_returned",
                summary=(
                    f"{hook_id} was retired from {retirement.effective_from} and has "
                    f"executed since, on {', '.join(back)}"
                ),
                evidence=(
                    f"retired by {retirement.ruled_by}: {retirement.rationale}",
                    f"ran on: {', '.join(life.ran_on)}",
                ),
            )
        )

    return found, not_run


def render(found: Iterable[Reconsideration], not_run: Iterable[str], version: str) -> str:
    found = list(found)
    not_run = list(not_run)
    lines = [f"RECONSIDER  at {version}", "=" * 68, ""]
    if not found:
        lines.append("  Nothing recorded has stopped matching the evidence.")
    else:
        lines.append(f"  {len(found)} recorded decision(s) no longer match the evidence:")
        lines.append("")
        for item in found:
            lines.append(f"    [{item.trigger}] {item.kind}: {item.subject}")
            lines.append(f"        {item.summary}")
            for line in item.evidence:
                lines.append(f"          · {line}")
            lines.append(f"        withdraws {item.original_decision_id}")
            lines.append("")
    if not_run:
        lines += ["  RULES NOT RUN", ""]
        for line in not_run:
            lines.append(f"    {line}")
        lines += [
            "",
            "    A rule that did not run found nothing for a reason that has nothing "
            "to do with the",
            "    evidence. Listed so it is never mistaken for a clean result.",
            "",
        ]
    lines += [
        "  This proposes; it does not decide. A withdrawal is a recorded human "
        "decision —",
        "  see manifest/REVERSALS.md. Nothing here changes anything.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--version", required=True)
    parser.add_argument("--baseline", default=BASELINE_VERSION)
    parser.add_argument(
        "--index",
        type=Path,
        help="a decode index for this version. Without it the "
        "`block_endpoint_absent` rule is skipped and says so",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        found, not_run = reconsiderations(
            args.root, version=args.version, baseline=args.baseline, index_dir=args.index
        )
    except (ReconsiderError, ExpectationError, ReversalError, ValueError, OSError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(
            {
                "schema_version": 1,
                "version": args.version,
                "reconsiderations": [item.to_dict() for item in found],
                "rules_not_run": not_run,
            },
            indent=2,
        ))
    else:
        print(render(found, not_run, args.version))
    # Exit 0 whatever it finds. This is a proposal, not a gate: a non-zero exit
    # would make a port fail because a human has not yet answered a question,
    # which is the "approve your way past a red build" pressure the whole design
    # avoids.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

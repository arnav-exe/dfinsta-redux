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

**`block_never_observed`** — the app was *watching* this endpoint on a real
device and never once saw it requested. Stage 4 judges an endpoint from its name
in a class of names, and on 2026-08-08 that produced one ruling on a path that
fires zero times and one on `delivery/background_prefetch`, which is not a
request path at all but a no-op logger's marker name. Both looked exactly like
the four good rulings beside them. `observation.never_observed` is the
measurement that tells them apart, and it refuses rather than answering when no
session is evidence — so this rule reports itself skipped instead of reading a
missing measurement as a finding.

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

**And `block_never_observed` must not re-create it.** The two are one word apart
in English and opposite in what they rest on: the omitted rule fires on the
*absence of an implementation*, this one on *evidence of non-occurrence*. The
difference is enforced rather than described — this rule skips exactly the
endpoints `unenforced_endpoints` names, the same set `block_inert` skips, so an
endpoint with no guard cannot reach it however its watch list was written. That
matters because a watch list is a claim by the build about what it was watching,
and `delivery/background_prefetch` inside one would otherwise put the day-old
decision the omission protects straight back in front of a human.

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
from .observation import ObservationError
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
TRIGGERS = (
    "block_inert",
    "block_endpoint_absent",
    "block_never_observed",
    "retirement_returned",
)


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

    from .assessment import normalise  # noqa: PLC0415
    from .reversal import withdrawn  # noqa: PLC0415

    try:
        lives, _ = roster(root, baseline=baseline)
        # Compared through `normalise`, because a human types `--endpoint` and
        # `reversal` stores what they typed: a block withdrawn as
        # `/feed/timeline_stream/` must silence a ruling recorded as
        # `feed/timeline_stream/`. Two spellings of one rule is how an entire
        # grouping went invisible on 440.
        withdrawn_blocks = {
            (decision_id, normalise(subject))
            for decision_id, subject in withdrawn("block", root)
        }
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
        unenforced_unknown = False
    except Exception as error:  # noqa: BLE001 - the app source may be absent
        # Not silently empty. An unreadable source means every block looks
        # enforced, so `block_inert` would judge endpoints whose enforcement
        # nobody checked — the rule still runs, and says what it could not see.
        unenforced = set()
        # Carried as its own fact rather than inferred from `not unenforced`:
        # "the source said nothing is unenforced" and "the source could not be
        # read" produce the same empty set and are opposite states, and
        # `block_never_observed` below has to tell them apart.
        unenforced_unknown = True
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
            # A withdrawn block is not a decision to question. This rule was the
            # last of the three to consult `withdrawn_blocks`: `retirement_returned`
            # reads through `retirements_on_record`, and `block_inert` is silenced
            # only *indirectly*, because `apply_unblock` removes the dep and
            # `_blocking_hook` then returns "". Neither of those protects this
            # rule, so withdrawing a block and later watching its endpoint vanish
            # would propose withdrawing it a second time — a question a human has
            # already answered for ever, which is what a reversal is.
            if (ruling.decision_id, normalise(endpoint)) in withdrawn_blocks:
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

    # ---- block_never_observed --------------------------------------------
    #
    # The only rule here built on a measurement of the app's own traffic rather
    # than on the shape of the code. Read the module docstring for why it is not
    # the omitted "declared and not enforced" rule wearing a new name: it skips
    # the same `unenforced` set `block_inert` skips, so an endpoint with no guard
    # cannot reach it whatever a watch list claims.
    from .observation import evidential, never_observed, read as read_observations  # noqa: PLC0415

    try:
        unseen = set(never_observed(version, root))
        sessions = evidential(read_observations(version, root))
    except ObservationError as error:
        # Skipped, and named. `never_observed` refuses when nothing measured is
        # evidence — no store, or every session vacuous — and that refusal is the
        # whole point: an empty answer would be the same answer it gives when
        # every watched path was seen.
        unseen, sessions = set(), ()
        not_run.append(f"block_never_observed: {error}")
    if unenforced_unknown and sessions:
        # The rule ran, with a blind spot, and says so under its own name. With
        # no app source nothing can be shown to be unenforced, so the structural
        # guarantee above — that this cannot fire on an unenforced endpoint —
        # does not hold on this run, and `delivery/background_prefetch` in a
        # watch list would reach a human as a question about a day-old decision.
        not_run.append(
            f"block_never_observed: could not read the app source at {source}, so an "
            "endpoint with no guard was not excluded from this rule"
        )

    if sessions:
        surfaces = sorted({item.surface for item in sessions})
        totals: dict[str, int] = {}
        for item in sessions:
            for literal, count in item.counts.items():
                totals[literal] = totals.get(literal, 0) + count
        # Keyed by the app's own spelling rule, like every other join here: the
        # watch list carries the literal as the smali does (`/feed/timeline/`)
        # and a ruling carries the candidate's (`feed/timeline/`). Built from a
        # sorted iteration so that two watched spellings of one rule — both
        # unobserved, so either is a truthful evidence line — always yield the
        # same one rather than whichever the set happened to hand over.
        by_rule = {normalise(literal): literal for literal in sorted(unseen, reverse=True)}

        for endpoint, ruling in sorted(blocked_by_ruling.items()):
            # Declared AND enforced, in that order. `unenforced_endpoints` names
            # what is *declared and unguarded*, so an endpoint that no hook
            # declares at all is in neither set and would sail past a check on
            # enforcement alone — which is the docstring's structural guarantee
            # failing on the one example it names. Today every ruled endpoint is
            # declared, so this line changes nothing; it becomes load-bearing the
            # moment a dep leaves `hooks.json` while `rulings.jsonl` keeps its
            # row, which is exactly what `apply_unblock` does. `block_inert`
            # reaches the same answer through `by_id.get(hook_id)` being None.
            if not _blocking_hook(lives, manifest, endpoint):
                continue
            if endpoint in unenforced:
                continue
            if (ruling.decision_id, normalise(endpoint)) in withdrawn_blocks:
                continue
            literal = by_rule.get(normalise(endpoint))
            if literal is None:
                continue
            found.append(
                Reconsideration(
                    kind="block",
                    subject=endpoint,
                    original_decision_id=ruling.decision_id,
                    trigger="block_never_observed",
                    summary=(
                        f"{endpoint} is blocked and the app was watching it on {version}, "
                        f"and across {len(sessions)} session(s) that observed "
                        f"{sum(totals.values())} request(s) it was never requested once"
                    ),
                    evidence=(
                        f"ruled block on {ruling.recorded_at or 'an unrecorded date'}",
                        f"watched as {literal}; observed 0 times",
                        # `totals` cannot be empty here: every session in
                        # `sessions` is non-vacuous, which is what makes it
                        # evidence at all. No "or nothing" fallback, because a
                        # branch that cannot be reached is a safety net nobody
                        # can test.
                        "the same sessions observed: "
                        + ", ".join(
                            f"{key} x{value}"
                            for key, value in sorted(
                                totals.items(), key=lambda pair: (-pair[1], pair[0])
                            )
                        ),
                        f"measured on: {', '.join(surfaces)}",
                        # The bound, on every finding. It cannot be resolved from
                        # the store, and it is the reading a human is most likely
                        # to make without being told not to.
                        "NEVER OBSERVED IS BOUNDED BY THE SURFACES WALKED. A path only "
                        "one screen requests is not observed by a session that never "
                        "went there, and server-side configuration can suppress a "
                        "request the app would otherwise make — a MobileConfig flag is "
                        "why a statically perfect 430 settings hook was dead at "
                        "runtime. CONFIRM THE SURFACE THAT WOULD EXERCISE IT WAS "
                        "VISITED before withdrawing",
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
    except (
        ReconsiderError,
        ExpectationError,
        ObservationError,
        ReversalError,
        ValueError,
        OSError,
    ) as error:
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

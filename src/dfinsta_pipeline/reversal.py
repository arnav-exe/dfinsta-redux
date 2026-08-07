"""Withdrawing a decision that was already recorded — the only way back.

    python -m dfinsta_pipeline.reversal list
    python -m dfinsta_pipeline.reversal withdraw-block --endpoint feed/timeline_stream/ \
        --original-decision-id decision-29d4… --ruled-by arnav \
        --rationale "…" --recorded-at 2026-08-09T10:00:00Z --confirm
    python -m dfinsta_pipeline.reversal withdraw-retirement --hook <id> --from-version 443 …

This project had **two one-way doors** and no way back through either.

* **A block.** A human rules `block` on an endpoint, `rulings.apply` adds it to
  `semantic_deps`, and `feature_gate.VERDICTS` — `block`, `offer_toggle`,
  `ignore`, `defer` — contains nothing that means "stop blocking". Worse than a
  missing button: candidates are computed as *consumption surfaces not already in
  `semantic_deps`*, so blocking one removes it from the population the gate draws
  from. **The question can never be raised again.**
* **A retirement.** `read_retirements` takes the *earliest* `effective_from` per
  hook, deliberately, so appending cannot un-retire. Also correct, and also a
  door with no handle.

The escape in both cases was a human editing a tracked file, or reverting a
commit — which is precisely the unreviewed edit the rest of this design exists to
prevent, and which **erases** the decision rather than recording its reversal.
This project's discipline is that decisions are permanent and reversals are new
decisions. That is what this module implements.

===============================================================================
  WHAT A REVERSAL IS, AND IS NOT
===============================================================================

**It is a new recorded decision, appended.** Both rows survive: blocked on the
8th for reason A, unblocked on the 9th for reason B. Nothing is deleted, and the
history reads as a history.

**It names the decision it withdraws.** You cannot withdraw something that was
never decided, and the pairing is `(original_decision_id, subject)` rather than
the decision id alone — one gate decision covers every candidate in its docket,
so withdrawing by id would silently unblock all six at once.

**Only a human may sign one.** `ruled_by: agent` is refused, as everywhere else:
if the thing being measured could withdraw the measurement, the cheapest route
past a red build would be to undo the rule.

**The pipeline may propose one; it may never make one.** An automatic reversal
would let the system quietly undo its own protections. The signals worth raising
a gate on already exist — a block whose hook never executes, a device contrast
that shows the app broken with it on, an endpoint that has vanished from the app
— and each of those is evidence for a human, not a trigger for a machine.

===============================================================================
  WHY THE TWO KINDS DIFFER IN ONE PLACE
===============================================================================

`effective_from` applies to a **retirement** withdrawal and not to a block one,
and that asymmetry is real rather than an oversight.

`expectation` asks "was this hook retired *as of version N*", so restoring it has
to name a version — and it is derived as the one *after* the port it was ruled
from, exactly as a retirement is, so a hook cannot be restored retroactively into
a port that has already been assessed. A block lives in `manifest/hooks.json`,
which is applied to whatever version is being built and has no per-version
semantics at all; a withdrawal takes effect at the next build and a version field
would be a number nothing reads.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from .contracts import canonical_sha256
from .history import _NUMERIC

__all__ = [
    "ReversalError",
    "REVERSALS",
    "KINDS",
    "Reversal",
    "read_reversals",
    "withdrawn",
    "withdrawn_at",
    "UnblockPlan",
    "reversal_id",
    "append",
    "plan_unblock",
    "apply_unblock",
    "render",
    "main",
]


class ReversalError(RuntimeError):
    """Raised when a reversal cannot honestly be recorded or applied."""


#: Append-only, tracked. See `manifest/REVERSALS.md`.
REVERSALS = Path("manifest") / "reversals.jsonl"

#: What may be withdrawn. Deliberately closed: a reversal whose kind nothing
#: consumes would record an intention and change nothing, which is the shape this
#: project has shipped at one end or the other four times.
KINDS = ("block", "retirement")


@dataclass(frozen=True)
class Reversal:
    """A human's recorded decision to withdraw an earlier one."""

    schema_version: int
    #: `block` or `retirement`.
    withdraws: str
    #: The endpoint path, or the hook id. Whatever the original decision was about.
    subject: str
    #: The decision being withdrawn. Together with `subject` this identifies one
    #: ruling out of a docket that shared a decision id.
    original_decision_id: str
    decision_id: str
    ruled_by: str
    rationale: str
    recorded_at: str
    #: Retirement withdrawals only: the first version that expects the hook again.
    effective_from: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ReversalError(f"unsupported reversal schema {self.schema_version!r}")
        if self.withdraws not in KINDS:
            raise ReversalError(
                f"unknown reversal kind {self.withdraws!r}; expected one of "
                f"{', '.join(KINDS)}"
            )
        for value, label in (
            (self.subject, "subject"),
            (self.original_decision_id, "original_decision_id"),
            (self.decision_id, "decision_id"),
            (self.ruled_by, "ruled_by"),
            (self.rationale, "rationale"),
        ):
            if not str(value).strip():
                raise ReversalError(
                    f"a reversal is missing {label}. Withdrawing a decision with no "
                    "author, no reason, or no decision to withdraw is an edit wearing "
                    "a record's clothes"
                )
        if self.ruled_by.strip().lower() == "agent":
            raise ReversalError(
                "ruled_by is 'agent'. A human withdraws a decision; an agent may "
                "assemble the case for withdrawing it"
            )
        if self.withdraws == "retirement":
            if not _NUMERIC.fullmatch(self.effective_from):
                raise ReversalError(
                    "withdrawing a retirement needs effective_from: `expectation` asks "
                    "whether a hook was retired as of a version, so restoring it has to "
                    "name one"
                )
        elif self.effective_from:
            raise ReversalError(
                "a block withdrawal must not carry effective_from. `manifest/hooks.json` "
                "is applied to whatever version is being built and has no per-version "
                "semantics; the field would be a number nothing reads"
            )

    def to_dict(self) -> dict[str, Any]:
        row = {
            "schema_version": self.schema_version,
            "withdraws": self.withdraws,
            "subject": self.subject,
            "original_decision_id": self.original_decision_id,
            "decision_id": self.decision_id,
            "ruled_by": self.ruled_by,
            "rationale": self.rationale,
            "recorded_at": self.recorded_at,
        }
        # Omitted rather than written empty, following `EvidenceClaim.to_dict`: a
        # block withdrawal that carried `"effective_from": ""` would invite a
        # reader to treat the empty string as a version.
        if self.effective_from:
            row["effective_from"] = self.effective_from
        return row

    @classmethod
    def from_dict(cls, data: Any) -> "Reversal":
        if not isinstance(data, dict):
            raise ReversalError(
                f"a reversal must be a JSON object, got {type(data).__name__}"
            )
        allowed = {
            "schema_version",
            "withdraws",
            "subject",
            "original_decision_id",
            "decision_id",
            "ruled_by",
            "rationale",
            "recorded_at",
            "effective_from",
        }
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise ReversalError(f"reversal has unknown keys: {', '.join(unknown)}")
        return cls(
            schema_version=data.get("schema_version"),
            withdraws=str(data.get("withdraws", "")),
            subject=str(data.get("subject", "")),
            original_decision_id=str(data.get("original_decision_id", "")),
            decision_id=str(data.get("decision_id", "")),
            ruled_by=str(data.get("ruled_by", "")),
            rationale=str(data.get("rationale", "")),
            recorded_at=str(data.get("recorded_at", "")),
            effective_from=str(data.get("effective_from", "")),
        )


def reversal_id(
    *, withdraws: str, subject: str, original_decision_id: str, ruled_by: str,
    rationale: str, recorded_at: str, effective_from: str = "",
) -> str:
    """A reversal's identity, derived from its content.

    Same rule as a ruling and a retirement: identical answers deduplicate and
    different ones cannot collide, so a retry cannot mint a second reversal of
    the same decision.
    """

    digest = canonical_sha256(
        {
            "effective_from": effective_from,
            "original_decision_id": original_decision_id,
            "rationale": rationale,
            "recorded_at": recorded_at,
            "ruled_by": ruled_by,
            "subject": subject,
            "withdraws": withdraws,
        }
    )
    return f"withdraw-{withdraws}-{digest[:12]}"


def read_reversals(
    root: Path | str = ".", *, path: Path | str | None = None
) -> list[Reversal]:
    """Every recorded reversal, in file order.

    A missing file means none, which is the ordinary state. A file that exists
    and cannot be read is a refusal: a malformed row silently skipped would read
    as "this decision was never withdrawn", which is the direction that keeps a
    withdrawn block in force.
    """

    location = Path(path) if path is not None else Path(root) / REVERSALS
    if not location.is_file():
        return []
    out: list[Reversal] = []
    for number, line in enumerate(location.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ReversalError(f"{location}:{number}: {error}") from error
        if not isinstance(row, dict):
            raise ReversalError(
                f"{location}:{number}: expected a JSON object, got {type(row).__name__}"
            )
        record = row.get("record", row)
        try:
            out.append(Reversal.from_dict(record))
        except ReversalError as error:
            raise ReversalError(f"{location}:{number}: {error}") from error
    return out


def withdrawn(
    kind: str, root: Path | str = ".", *, path: Path | str | None = None
) -> dict[tuple[str, str], Reversal]:
    """`(original_decision_id, subject) -> Reversal` for one kind.

    Keyed on the pair and not on the decision id, because one gate decision
    covers every candidate in its docket — keying on the id alone would withdraw
    six rulings when a human withdrew one.
    """

    if kind not in KINDS:
        raise ReversalError(f"unknown reversal kind {kind!r}")
    return {
        (item.original_decision_id, item.subject): item
        for item in read_reversals(root, path=path)
        if item.withdraws == kind
    }


def withdrawn_at(
    version: str, kind: str, root: Path | str = ".", *, path: Path | str | None = None
) -> dict[tuple[str, str], Reversal]:
    """Those in force when reporting on `version`. Retirement withdrawals only.

    A withdrawal ruled for 443 must not reach back and restore a hook into 442's
    expectation, for the same reason a retirement ruled for 442 must not excuse
    441 — a decision that can change what an already-assessed port owed is a
    decision that can rewrite history.
    """

    if not _NUMERIC.fullmatch(version):
        raise ReversalError(f"{version!r} is not a version number")
    return {
        key: item
        for key, item in withdrawn(kind, root, path=path).items()
        if item.effective_from and int(item.effective_from) <= int(version)
    }


def append(
    reversal: Reversal, *, root: Path | str = ".", path: Path | str | None = None
) -> Path:
    """Append one reversal. Refuses a duplicate withdrawal of the same decision."""

    location = Path(path) if path is not None else Path(root) / REVERSALS
    existing = read_reversals(root, path=location)
    key = (reversal.original_decision_id, reversal.subject)
    for item in existing:
        if (item.original_decision_id, item.subject) == key and item.withdraws == reversal.withdraws:
            raise ReversalError(
                f"{reversal.subject} was already withdrawn from {reversal.original_decision_id} "
                f"by {item.ruled_by} ({item.decision_id}). To block it again, rule on it "
                "at the gate — that is a new decision, not an un-withdrawal"
            )
    location.parent.mkdir(parents=True, exist_ok=True)
    with open(location, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(reversal.to_dict(), sort_keys=True) + "\n")
    return location


# --------------------------------------------------------------- unblocking


@dataclass(frozen=True)
class UnblockPlan:
    """What withdrawing a block would change, computed before anything is written."""

    reversal: Reversal
    manifest_path: Path
    document_before: str
    document_after: str
    #: Tuples, because the loop below removes the endpoint from EVERY hook whose
    #: `semantic_deps` normalise to it. Scalars here reported only the last match
    #: while two entries had been edited — latent in today's manifest, one edit
    #: away from being a lie in the output.
    hook_ids: tuple[str, ...]
    removed: tuple[str, ...]

    @property
    def changes_manifest(self) -> bool:
        return self.document_before != self.document_after


def plan_unblock(
    reversal: Reversal, *, manifest_path: Path | str
) -> UnblockPlan:
    """Remove `reversal.subject` from the url_block hook's `semantic_deps`.

    Matches the entry the way the app does — `assessment.normalise`, which strips
    a leading slash — so a decision recorded as `feed/timeline_stream/` withdraws
    the manifest's `/feed/timeline_stream/` and vice versa. Two spellings of one
    rule is how an entire grouping went invisible on 440.
    """

    from .assessment import normalise  # noqa: PLC0415  (cycle: assessment reads the manifest)
    from .manifest_patch import serialise  # noqa: PLC0415

    if reversal.withdraws != "block":
        raise ReversalError("plan_unblock needs a block withdrawal")
    manifest_path = Path(manifest_path)
    before = manifest_path.read_text(encoding="utf-8")
    document = json.loads(before)
    if serialise(document) != before:
        raise ReversalError(
            f"{manifest_path} is not in canonical form; writing it would reformat "
            "lines nobody reviewed"
        )

    hooks = document.get("hooks")
    if not isinstance(hooks, list):
        # `ManifestError` is a `ValueError` and is caught by `main`, which is what
        # made this gap look accidental: a manifest missing `hooks` entirely
        # produced a bare `KeyError` and exit 1 instead of `refused:` and exit 2.
        raise ReversalError(f"{manifest_path} has no 'hooks' array")
    target = normalise(reversal.subject)
    hook_ids: list[str] = []
    removed: list[str] = []
    for entry in hooks:
        if not isinstance(entry, dict) or "hook_id" not in entry:
            raise ReversalError(f"{manifest_path}: a hook entry has no hook_id")
        deps = entry.get("semantic_deps") or []
        keep = [dep for dep in deps if normalise(dep) != target]
        if len(keep) != len(deps):
            hook_ids.append(entry["hook_id"])
            removed.extend(dep for dep in deps if normalise(dep) == target)
            entry["semantic_deps"] = keep
    if not hook_ids:
        raise ReversalError(
            f"{reversal.subject} is not in any hook's semantic_deps, so there is "
            "nothing to withdraw. A reversal that changes nothing would record an "
            "intention and leave the block in place"
        )
    return UnblockPlan(
        reversal=reversal,
        manifest_path=manifest_path,
        document_before=before,
        document_after=serialise(document),
        hook_ids=tuple(hook_ids),
        removed=tuple(removed),
    )


def apply_unblock(
    plan: UnblockPlan,
    *,
    confirm: bool,
    root: Path | str = ".",
    reversals_path: Path | str | None = None,
) -> Path:
    """Record the reversal, then write the manifest. In that order.

    The record first, as `rulings.apply` does: if the manifest write then fails, a
    human's decision is still on disk. The opposite order could unblock an
    endpoint with no record of who decided to.
    """

    if not confirm:
        raise ReversalError(
            "withdrawing a block changes what the app blocks; pass confirm"
        )
    current = plan.manifest_path.read_text(encoding="utf-8")
    if current != plan.document_before:
        raise ReversalError(
            f"{plan.manifest_path} changed since this plan was made; re-plan rather "
            "than overwrite an edit nobody reviewed"
        )

    from .manifest_patch import write_manifest_atomically  # noqa: PLC0415

    written = append(plan.reversal, root=root, path=reversals_path)
    write_manifest_atomically(plan.manifest_path, plan.document_after)
    return written


def render(reversals: Iterable[Reversal]) -> str:
    reversals = list(reversals)
    if not reversals:
        return (
            "No decision has been withdrawn.\n\n"
            "That is the ordinary state. A reversal is a recorded human decision to "
            "undo an earlier\none — see manifest/REVERSALS.md."
        )
    lines = [f"RECORDED REVERSALS ({len(reversals)})", "=" * 60, ""]
    for item in reversals:
        when = f" from {item.effective_from}" if item.effective_from else ""
        lines.append(f"  {item.withdraws}: {item.subject}{when}")
        lines.append(f"      withdraws {item.original_decision_id}")
        lines.append(f"      {item.ruled_by} at {item.recorded_at} — {item.rationale}")
        lines.append("")
    lines.append(
        "  Both rows survive: the original decision and its withdrawal. Nothing here "
        "deletes history."
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--reversals", type=Path, help=f"default <root>/{REVERSALS}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="every recorded reversal")

    def human(p: argparse.ArgumentParser) -> None:
        p.add_argument("--original-decision-id", required=True)
        p.add_argument("--ruled-by", required=True)
        p.add_argument("--rationale", required=True)
        p.add_argument(
            "--recorded-at",
            required=True,
            help="ISO 8601. Supplied, never read from the clock here",
        )
        p.add_argument("--confirm", action="store_true")

    unblock = sub.add_parser("withdraw-block", help="stop blocking an endpoint")
    unblock.add_argument("--endpoint", required=True)
    # Defaulted from --root after parsing, not here: a literal default is relative
    # to the process CWD, so `--root /repo` recorded the reversal inside the repo
    # and read the manifest outside it. Half-scoped is worse than unscoped.
    unblock.add_argument("--manifest", type=Path, default=None)
    human(unblock)

    unretire = sub.add_parser("withdraw-retirement", help="expect a hook again")
    unretire.add_argument("--hook", required=True)
    unretire.add_argument(
        "--from-version",
        required=True,
        help="the first version that expects the hook again. Must be later than the "
        "port the withdrawal was decided from, so it cannot restore a hook into an "
        "already-assessed port",
    )
    human(unretire)

    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            print(render(read_reversals(args.root, path=args.reversals)))
            return 0

        kind = "block" if args.command == "withdraw-block" else "retirement"
        subject = args.endpoint if kind == "block" else args.hook
        effective = "" if kind == "block" else args.from_version
        reversal = Reversal(
            schema_version=1,
            withdraws=kind,
            subject=subject,
            original_decision_id=args.original_decision_id,
            decision_id=reversal_id(
                withdraws=kind,
                subject=subject,
                original_decision_id=args.original_decision_id,
                ruled_by=args.ruled_by,
                rationale=args.rationale,
                recorded_at=args.recorded_at,
                effective_from=effective,
            ),
            ruled_by=args.ruled_by,
            rationale=args.rationale,
            recorded_at=args.recorded_at,
            effective_from=effective,
        )

        if kind == "block":
            manifest = args.manifest or Path(args.root) / "manifest" / "hooks.json"
            plan = plan_unblock(reversal, manifest_path=manifest)
            written = apply_unblock(
                plan, confirm=args.confirm, root=args.root, reversals_path=args.reversals
            )
            for hook_id, entry in zip(plan.hook_ids, plan.removed):
                print(f"withdrew {entry} from {hook_id}.semantic_deps")
            print(f"recorded in {written}; manifest written: {plan.manifest_path}")
        else:
            if not args.confirm:
                raise ReversalError(
                    "restoring a hook to the expectation can fail a future port; pass confirm"
                )
            written = append(reversal, root=args.root, path=args.reversals)
            print(
                f"{reversal.subject} is expected again from {reversal.effective_from}; "
                f"recorded in {written}"
            )
        print(
            "Commit it: the expectation and the manifest read the committed files, and "
            "an uncommitted row works here and vanishes on clone."
        )
        return 0
    except (ReversalError, KeyError, ValueError, OSError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

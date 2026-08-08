"""The one way to stop expecting a hook, and the one way to start again.

    python -m dfinsta_pipeline.retirement show
    python -m dfinsta_pipeline.retirement retire   --hook <id> --version 441 \
        --ruled-by arnav --rationale "..." --recorded-at 2026-08-09T10:00:00+00:00
    python -m dfinsta_pipeline.retirement unretire --hook <id> --version 443 \
        --ruled-by arnav --rationale "..." --recorded-at ...

`expectation` is a ratchet: every hook release-ready on N-1 must be release-ready
on N. That is what stops a port quietly losing a hook. A ratchet with no release
is a trap, though — when Instagram genuinely removes a surface, the hook that
patched it can never pass again and would fail the expectation for ever. Shopping
already did this once: it lost its dedicated tab and dissolved across other
endpoints, and DFInsta 1.4.1's three Shopping substitutions are permanently
ineffective as a result.

This is that release, and nothing else. There was a much larger version of this
until 2026-08-08 — a durable Workflow, a signed docket, a validator/authority
split, a starter with a retry loop, committed History fixtures — and it recorded
**zero retirements in its entire life**. The apparatus was deleted; the two rules
that were actually load-bearing were kept.

===============================================================================
  THE TWO RULES WORTH KEEPING
===============================================================================

**`effective_from` is derived from the tree, never supplied.** It is the version
after the newest one with committed evidence — `versions_with_evidence(root)[-1] + 1`.
Nothing on the command line names it, and nothing names the version it is computed
from either.

That second half is the part this module got wrong on its first day. It used to
derive `effective_from` from a `--version` argument, which made the rule
syntactic: standing at a red 441 build you could type `--version 440`, get
`effective_from 441`, and clear the very port that exposed the drop. Exit 0, no
warning, and every other rule still satisfied — the derivation checked out, the
ruler was a human, the rationale was there. A relation between two fields the
same person supplies in the same command protects nobody. **Ask what the operator
controls, not whether the arithmetic is right.**

The same reasoning is why this is not a gate inside a port and cannot unblock
one. If a red build could be turned green by approving a retirement, approving a
retirement becomes the cheapest thing a tired person can do at the end of a long
port — and it would reliably be approved exactly when the evidence for it is
weakest. Landing a version late costs nothing real: a hook that should be retired
is not urgent, because the thing it patched is already not working.

**An agent may assemble every fact and still not rule.** `ruled_by` refuses
`agent`, because the thing being measured must not get to rule that the
measurement no longer applies.

===============================================================================
  UN-RETIREMENT IS A ROW, NOT AN EDIT
===============================================================================

Instagram brings surfaces back. So a retirement must be reversible — and the
reversal must not erase the retirement, or the record would say a hook had never
been doubted. Both decisions are rows in one append-only file, and "is this hook
retired at version V?" is a **fold** over them rather than a field:

    retire   settings_hook  effective 442  "IgActionBar removed on 441"
    unretire settings_hook  effective 445  "the action bar is back on 444"

`history()` returns that sequence for a hook, so the fact that it was retired and
later un-retired survives in full. Editing or deleting a row is never correct; a
`_TRIGGER`-free JSONL file cannot enforce that, so the CLI simply offers no way to
do it and `read()` refuses a file it cannot parse rather than skipping the line.

===============================================================================
  NOTICING THAT ONE CAME BACK
===============================================================================

:func:`returned` names retired hooks that have since passed a runtime probe at or
after their effective version. It is a **report, never a gate** — it proposes that
someone look, and un-retiring stays a human act. Nothing here decides.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

__all__ = [
    "KINDS",
    "RETIREMENTS",
    "Retirement",
    "RetirementError",
    "append",
    "history",
    "latest_ported",
    "read",
    "retired_at",
    "returned",
]


class RetirementError(Exception):
    """Raised rather than recording, or reading, a decision that is not sound."""


#: `retire` stops the expectation demanding a hook; `unretire` starts it again.
#: Both are rows. There is no third kind and no delete.
KINDS = ("retire", "unretire")

RETIREMENTS = Path("manifest") / "retirements.jsonl"

_NUMERIC = re.compile(r"\d+")
_SCHEMA = 1


def _version(value: str, field: str) -> str:
    if not _NUMERIC.fullmatch(str(value).strip()):
        raise RetirementError(f"{field} {value!r} is not a version number")
    return str(value).strip()


def latest_ported(root: Path | str = ".") -> str:
    """The newest version with committed evidence — what a decision is taken AT.

    Read from the tree so no operator supplies it. Lazy import because
    `expectation` imports this module back to subtract retirements.
    """
    from .expectation import ExpectationError, versions_with_evidence  # noqa: PLC0415

    try:
        series = versions_with_evidence(root)
    except ExpectationError as error:
        raise RetirementError(f"cannot tell which version this is being decided at: {error}") from error
    if not series:
        raise RetirementError(
            f"{Path(root)} has no committed evidence, so there is no version to decide at. "
            "A retirement is always taken at the newest port and takes effect after it."
        )
    return series[-1]


@dataclass(frozen=True)
class Retirement:
    """One decision about one hook, at one version."""

    kind: str
    hook_id: str
    #: The version this takes effect at. ALWAYS `version + 1`; see the module
    #: docstring. Stored rather than recomputed on read so the file states what
    #: was decided, and checked on read so a hand-edited row is refused.
    effective_from: str
    #: The version the decision was taken at, kept so `effective_from` can be
    #: re-derived and disagreement caught.
    decided_at: str
    ruled_by: str
    rationale: str
    recorded_at: str

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise RetirementError(f"unknown kind {self.kind!r}; expected one of {', '.join(KINDS)}")
        if not self.hook_id.strip():
            raise RetirementError("a retirement must name a hook")
        ruled_by = self.ruled_by.strip()
        if not ruled_by:
            raise RetirementError("a retirement must name who ruled")
        if ruled_by.lower() == "agent":
            raise RetirementError(
                "ruled_by 'agent' is refused. An agent may assemble every fact in the "
                "case and still not close it: the thing being measured must not get to "
                "rule that the measurement no longer applies."
            )
        if not self.rationale.strip():
            raise RetirementError("a retirement must say why; the record is read by people")
        if not self.recorded_at.strip():
            raise RetirementError("a retirement must carry the timestamp it was recorded at")
        decided = _version(self.decided_at, "decided_at")
        effective = _version(self.effective_from, "effective_from")
        expected = str(int(decided) + 1)
        if effective != expected:
            raise RetirementError(
                f"effective_from {effective} does not follow decided_at {decided}. It is "
                f"always the next version ({expected}) and is derived, never supplied — a "
                "decision that could name its own effective version could be backdated "
                "onto the port that exposed the drop."
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA,
            "kind": self.kind,
            "hook_id": self.hook_id,
            "effective_from": self.effective_from,
            "decided_at": self.decided_at,
            "ruled_by": self.ruled_by,
            "rationale": self.rationale,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Retirement":
        if not isinstance(data, dict):
            raise RetirementError(f"a retirement row must be an object, not {type(data).__name__}")
        missing = [
            key
            for key in ("kind", "hook_id", "effective_from", "decided_at", "ruled_by",
                        "rationale", "recorded_at")
            if key not in data
        ]
        if missing:
            raise RetirementError(f"retirement row is missing {', '.join(missing)}")
        return cls(
            kind=str(data["kind"]),
            hook_id=str(data["hook_id"]),
            effective_from=str(data["effective_from"]),
            decided_at=str(data["decided_at"]),
            ruled_by=str(data["ruled_by"]),
            rationale=str(data["rationale"]),
            recorded_at=str(data["recorded_at"]),
        )


def _path(root: Path | str) -> Path:
    return Path(root) / RETIREMENTS


def read(root: Path | str = ".") -> tuple[Retirement, ...]:
    """Every decision on record, in the order it was written.

    An absent file is an empty record — no hook has ever been retired, which is
    a coherent state and the one this project has been in for its whole life. An
    **unreadable** file is not: it is refused, because a corrupt store that reads
    as "nothing retired" would silently restore the ratchet's trap and every hook
    would look expected again. Absent and unreadable have been conflated in four
    modules here; this is not the fifth.
    """
    path = _path(root)
    if not path.exists():
        return ()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise RetirementError(f"{path}: {error}") from error
    out: list[Retirement] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            out.append(Retirement.from_dict(json.loads(line)))
        except (json.JSONDecodeError, RetirementError) as error:
            raise RetirementError(f"{path} line {number}: {error}") from error
    return tuple(out)


def append(record: Retirement, root: Path | str = ".") -> Path:
    """Add one decision. Never rewrites, never removes.

    Refuses a decision that does not change anything — retiring a hook that is
    already retired, or un-retiring one that is not. Not tidiness: a no-op row
    would make `history()` read as though somebody had changed their mind twice,
    and the history is the whole reason both kinds are rows.
    """
    existing = read(root)
    already = record.hook_id in retired_at(record.effective_from, root, records=existing)
    if record.kind == "retire" and already:
        raise RetirementError(
            f"{record.hook_id} is already retired as of {record.effective_from}; "
            "recording it twice would read as a change of mind"
        )
    if record.kind == "unretire" and not already:
        raise RetirementError(
            f"{record.hook_id} is not retired as of {record.effective_from}, so there "
            "is nothing to un-retire"
        )
    path = _path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record.to_dict(), sort_keys=True) + "\n")
    return path


def history(hook_id: str, root: Path | str = ".") -> tuple[Retirement, ...]:
    """Every decision ever taken about one hook, oldest first.

    This is what makes un-retirement safe to record: the retirement is still
    here afterwards. A reader can see that a hook was doubted, when, by whom and
    why, and that it later came back.
    """
    return tuple(item for item in read(root) if item.hook_id == hook_id)


def retired_at(
    version: str, root: Path | str = ".", *, records: Iterable[Retirement] | None = None
) -> frozenset[str]:
    """Which hooks are retired as of `version` — a fold, not a field.

    Rows are applied in file order, and only those whose `effective_from` is at or
    before `version`. So a hook retired effective 442 and un-retired effective 445
    is retired at 442, 443 and 444, and expected again at 445.
    """
    version = _version(version, "version")
    out: set[str] = set()
    # Sorted by when each decision takes effect, not by where it sits in the
    # file. `read` re-checks the derivation because hand-editing is the threat
    # model, and it does not check ordering — two individually valid rows written
    # out of order would otherwise fold to the wrong answer. Stable, so file
    # order still breaks ties.
    ordered = sorted(
        read(root) if records is None else records, key=lambda item: int(item.effective_from)
    )
    for item in ordered:
        if int(item.effective_from) > int(version):
            continue
        if item.kind == "retire":
            out.add(item.hook_id)
        else:
            out.discard(item.hook_id)
    return frozenset(out)


def returned(version: str, root: Path | str = ".") -> tuple[str, ...]:
    """Retired hooks that have passed a runtime probe at or after taking effect.

    A report, never a gate. Instagram brings surfaces back, and a hook that is
    working again while the project has stopped expecting it is worth a look —
    but un-retiring is a human decision and nothing here takes it.
    """
    retired = retired_at(version, root)
    if not retired:
        return ()
    # The runtime evidence file is read directly rather than through
    # `expectation.port_report`, which would import this module back. It is one
    # JSONL of claims and the shape needed here is two fields.
    path = Path(root) / "manifest" / "runtime_evidence" / f"{version}.jsonl"
    if not path.exists():
        # No evidence for this version is not "nothing came back" — nobody
        # looked. Refusing keeps that distinct, the same way `read` refuses an
        # unreadable store.
        raise RetirementError(
            f"{path} does not exist, so whether a retired hook is working again was "
            f"never measured at {version}. That is not the same as none having come back."
        )
    back: set[str] = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            claim = json.loads(line)
        except json.JSONDecodeError as error:
            raise RetirementError(f"{path} line {number}: {error}") from error
        if claim.get("kind") == "runtime_probe" and claim.get("verdict") == "passed":
            if claim.get("hook_id") in retired:
                back.add(str(claim["hook_id"]))
    return tuple(sorted(back))


def render(version: str, root: Path | str = ".") -> str:
    records = read(root)
    retired = retired_at(version, root, records=records)
    lines = [f"RETIREMENTS  as of {version}", "=" * 68, ""]
    if not records:
        lines.append("  nothing has ever been retired")
        return "\n".join(lines)
    lines.append(f"  retired now ({len(retired)}): {', '.join(sorted(retired)) or 'none'}")
    lines.append("")
    lines.append("  the whole record, oldest first:")
    for item in records:
        verb = "RETIRED  " if item.kind == "retire" else "UN-RETIRED"
        lines.append(
            f"    {verb} {item.hook_id}  effective {item.effective_from}  "
            f"decided at {item.decided_at}  by {item.ruled_by}  "
            f"on {item.recorded_at}  ({item.rationale})"
        )
    try:
        came_back = returned(version, root)
    except RetirementError as error:
        # The record is the thing this command exists to print. `returned` refuses
        # when nothing measured whether a retired hook is working again, and that
        # refusal is right — but letting it out of here threw away the whole
        # history and printed only the error, which made `show` useless from the
        # first retirement onward.
        return "\n".join(lines + ["", f"  could not check for returns: {error}"])
    if came_back:
        lines += [
            "",
            f"  RETIRED AND WORKING AGAIN ({len(came_back)}): {', '.join(came_back)}",
            "    These passed a runtime probe at or after they were retired. Instagram",
            "    brings surfaces back. This proposes; un-retiring is a human decision.",
        ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path("."))
    sub = parser.add_subparsers(dest="command", required=True)

    show = sub.add_parser("show", help="the whole record, and what is retired now")
    show.add_argument(
        "--version", help="which version to report as of; defaults to the newest ported"
    )

    for kind in KINDS:
        rule = sub.add_parser(kind, help=f"record a {kind} decision")
        rule.add_argument("--hook", required=True)
        # No --version, and deliberately none. It used to be here, and it made the
        # backdating rule a formality: typing the previous version cleared the port
        # in front of you. The version is read from the tree.
        rule.add_argument("--ruled-by", required=True, help="a person; 'agent' is refused")
        rule.add_argument("--rationale", required=True)
        rule.add_argument("--recorded-at", required=True, help="ISO 8601; never read from a clock")

    args = parser.parse_args(argv)
    try:
        if args.command == "show":
            print(render(args.version or latest_ported(args.root), args.root))
            return 0
        decided = latest_ported(args.root)
        record = Retirement(
            kind=args.command,
            hook_id=args.hook,
            effective_from=str(int(decided) + 1),
            decided_at=decided,
            ruled_by=args.ruled_by,
            rationale=args.rationale,
            recorded_at=args.recorded_at,
        )
        path = append(record, args.root)
        print(
            f"{args.command}d {record.hook_id}, effective {record.effective_from} "
            f"(decided at {decided}) -> {path}"
        )
        return 0
    except (RetirementError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

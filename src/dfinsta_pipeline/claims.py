"""Read a stuck operation claim, and — deliberately, explicitly — release one.

A worker killed mid-stage delivers no cancellation. It leaves `operation_claims`
holding a `pending` row owned by a token whose process is gone, and the next
attempt refuses with *"Operation is already claimed"*, which is non-retryable, so
the run fails. **The run is wedged, not burned**: the row is `pending`, and
`Ledger.release_pending_operation` blanks the owner so a later attempt takes it
over at `begin_operation`, with every already-completed stage adopted.

Until this module existed nothing in the repo performed that release. Recovery
meant hand-written SQL against an append-only ledger, at exactly the moment
someone is under pressure — which is the situation most likely to turn a wedged
run into a real incident.

===============================================================================
  WHAT THIS WILL NOT DO
===============================================================================

**It will not release a claim whose owner might still be running.** A release
lets a second attempt work the same key, and while the ledger survives that —
`record_effect` is owner-fenced and `quarantine_operation` silently no-ops for a
non-owner — "survives" is not "was asked for". So the release is refused unless
the caller states the owner token they intend to release, and it must match the
row exactly. The token is printed by `show`; typing it back is the same
confirmation discipline the submission client uses for a gate subject.

**It will not touch a `quarantined` row.** Quarantine is terminal by design and
un-quarantining is not a recovery, it is a way to run something a fail-closed
check already refused.

**It will not touch `effect` or `completed`.** Those rows are the adoption path
working correctly: a later attempt is *supposed* to pick up their recorded
output, and blanking the owner would gain nothing.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from .ledger import Ledger

__all__ = ["ClaimError", "Claim", "read_claim", "release_claim", "main"]


class ClaimError(RuntimeError):
    """Raised when a claim cannot be read, or must not be released."""


#: The only status a release may act on. `quarantined` is terminal; `effect` and
#: `completed` are the adoption path already working.
RELEASABLE = "pending"


@dataclass(frozen=True)
class Claim:
    """One row of `operation_claims`, as an operator needs to see it."""

    operation_key: str
    kind: str
    input_sha256: str
    owner_token: str
    owner_attempt: int
    status: str

    @property
    def releasable(self) -> bool:
        return self.status == RELEASABLE and bool(self.owner_token)

    def describe(self) -> str:
        lines = [
            f"operation   {self.operation_key}",
            f"kind        {self.kind}",
            f"input       {self.input_sha256}",
            f"status      {self.status}",
            f"owner       {self.owner_token or '(released)'}",
            f"attempt     {self.owner_attempt}",
        ]
        if self.status == "quarantined":
            lines.append(
                "\nQuarantined. Terminal by design: a fail-closed check refused this "
                "operation, and un-quarantining it would run what that check stopped. "
                "Recovery is a new run id, a new run spec and a new gate decision."
            )
        elif self.status in {"effect", "completed"}:
            lines.append(
                f"\nStatus {self.status}: nothing to release. A later attempt adopts this "
                "operation's recorded output, which is the adoption path working."
            )
        elif not self.owner_token:
            lines.append(
                "\nAlready released. A later attempt will take this claim over."
            )
        else:
            lines.append(
                "\nPending and owned. If the owning worker is gone, release it with:\n"
                f"    --release {self.owner_token}\n"
                "Check first that no worker is still running this operation: a release "
                "lets a second attempt work the same key, and while the ledger survives "
                "that, nobody asked for it."
            )
        return "\n".join(lines)


def read_claim(ledger_path: Path | str, operation_key: str) -> Claim:
    """Read one claim row. Read-only, and opens the ledger read-only to prove it."""
    path = Path(ledger_path)
    if not path.is_file():
        raise ClaimError(f"No ledger at {path}")
    # Through `Ledger(read_only=True)` rather than a bare sqlite3 connection: the
    # `mode=ro` URI is what makes "this cannot write" a database handle rather
    # than a promise, and an operator tool reading a ledger under pressure is
    # exactly where that distinction earns its keep.
    reader = Ledger(path, read_only=True)
    with Ledger._connection(reader) as connection:
        row = connection.execute(
            "SELECT operation_key, kind, input_sha256, owner_token, owner_attempt, status "
            "FROM operation_claims WHERE operation_key = ?",
            (operation_key,),
        ).fetchone()
    if row is None:
        raise ClaimError(f"No claim recorded for operation {operation_key}")
    return Claim(*row)


def pending_claims(ledger_path: Path | str) -> list[Claim]:
    """Every pending, owned claim — what an operator wants when a run wedges."""
    path = Path(ledger_path)
    if not path.is_file():
        raise ClaimError(f"No ledger at {path}")
    reader = Ledger(path, read_only=True)
    with Ledger._connection(reader) as connection:
        rows = connection.execute(
            "SELECT operation_key, kind, input_sha256, owner_token, owner_attempt, status "
            "FROM operation_claims WHERE status = ? AND owner_token != '' "
            "ORDER BY operation_key",
            (RELEASABLE,),
        ).fetchall()
    return [Claim(*row) for row in rows]


def release_claim(ledger_path: Path | str, operation_key: str, owner_token: str) -> Claim:
    """Release a pending claim whose owner is gone. Refuses everything else.

    ``owner_token`` is required and must match the recorded owner exactly. It is
    not a convenience — it is the whole safety of the operation. `show` prints
    it; typing it back is a statement that the operator looked at the row rather
    than at a run id.
    """
    claim = read_claim(ledger_path, operation_key)
    if claim.status == "quarantined":
        raise ClaimError(
            f"{operation_key} is quarantined. That is terminal by design; releasing it "
            "would run what a fail-closed check refused."
        )
    if claim.status != RELEASABLE:
        raise ClaimError(
            f"{operation_key} is {claim.status}, not {RELEASABLE}. A later attempt already "
            "adopts its recorded output; there is nothing to release."
        )
    if not claim.owner_token:
        raise ClaimError(f"{operation_key} is already released")
    if owner_token != claim.owner_token:
        raise ClaimError(
            f"{operation_key} is owned by {claim.owner_token!r}, not {owner_token!r}. "
            "The owner must be stated exactly, so a release cannot be aimed at a row "
            "nobody looked at."
        )
    Ledger(Path(ledger_path)).release_pending_operation(operation_key, owner_token)
    return read_claim(ledger_path, operation_key)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument(
        "operation_key",
        nargs="?",
        help="omit to list every pending, owned claim",
    )
    parser.add_argument(
        "--release",
        metavar="OWNER_TOKEN",
        help="release this claim. The owner token must match what `show` printed.",
    )
    args = parser.parse_args(argv)
    ledger_path = args.state_root / "ledger.sqlite3"

    try:
        if args.operation_key is None:
            if args.release:
                parser.error("--release needs an operation key")
            claims = pending_claims(ledger_path)
            if not claims:
                print("no pending owned claims")
                return 0
            for claim in claims:
                print(f"{claim.operation_key}  {claim.kind}  owner={claim.owner_token}")
            return 0
        if args.release:
            claim = release_claim(ledger_path, args.operation_key, args.release)
            print("released.\n")
            print(claim.describe())
            return 0
        print(read_claim(ledger_path, args.operation_key).describe())
        return 0
    except (ClaimError, ValueError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

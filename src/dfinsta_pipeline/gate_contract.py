"""The clauses every gate's authority must check, in one place.

There are two durable human gates in this pipeline — replay verification and the
feature assessment. There were four until 2026-08-08, when hook retirement and
the reversal of a recorded decision went with the rest of the
decide-early-then-correct layer, neither ever having been answered. Each gate has
its own `validate_submission`, and the first six clauses of each are
character-for-character the same:

1. the submitted artifact holds exactly this document (digest, then size);
2. **all three** of the decision's hash fields are the *derived* request hash;
3. the decision names this run and this gate;
4. the decision was made under this policy revision;
5. the decision's actor is the one the request allows.

Those are the security-shaped clauses. Everything a gate does *beyond* them — which
candidates must be ruled on, which verdicts need a rationale, whether a version
field must be present — is genuinely per-gate and stays there.

===============================================================================
  WHY THIS EXISTS RATHER THAN A FOURTH COPY
===============================================================================

(The heading is the argument as it was made, when there were four gates and this
module was written instead of a fourth copy of the clauses. Two of the four have
since been deleted; the argument is what put the clauses in one place, and the
reason it is right does not depend on the count.)

`the-authority-checked-less-than-the-filter` records what happened when one of
these clauses was missing from one gate: `validate_submission` never compared the
actor, so *who may answer* rested entirely on a sandbox validator, and a decision
from `intruder` carrying the right subject hash was admitted. It also bound only
`subject_sha256`, leaving the other two hashes to the filter alone.

That was one gate with one gap. Three copies is three places to get it right and
two places for a fix to be forgotten — the same rot this project spent an
afternoon marking in its documents, except silent, because nothing prints a
security clause that is missing.

===============================================================================
  WHAT IT REFUSES TO DO
===============================================================================

**It takes no digest as an argument.** The subject hash is recomputed from the
request the caller derived. Accepting one would let a caller assert the very
thing being approved, which is what every gate here exists to prevent.

**It raises `ValueError`, not a gate-specific error.** Each gate wraps its own
refusal type around this; the Activities that call it convert to a non-retryable
`ApplicationError`. A shared module that invented its own exception type would
force every gate to catch something none of them names.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .contracts import ArtifactRef, GateDecision, canonical_json

__all__ = ["bind_document", "bind_decision"]


def bind_document(document: Any, reference: ArtifactRef, *, label: str) -> bytes:
    """The artifact holds exactly this document. Returns its canonical bytes.

    The caller fetched and decoded the document; nothing until now ties it to the
    reference the human signed. Canonical bytes make that a hash comparison
    rather than a matter of trust — and the size check is not redundant, because
    a reference whose size disagrees with its digest is a reference nobody
    computed from these bytes.
    """

    body = canonical_json(document).encode("utf-8")
    if hashlib.sha256(body).hexdigest() != reference.sha256:
        raise ValueError(f"Submitted {label} artifact does not hold this document")
    if len(body) != reference.size:
        raise ValueError(f"Submitted {label} artifact size does not match this document")
    return body


def bind_decision(
    decision: GateDecision,
    *,
    subject_sha256: str,
    run_id: str,
    gate_id: str,
    policy_revision: str,
    allowed_actor: str,
) -> None:
    """The decision binds this gate, and was made by somebody allowed to make it.

    `subject_sha256` is the caller's *recomputed* request hash — never a value
    carried alongside the decision.

    **All three hash fields, not just the subject.** A Workflow binds all three to
    the request hash and its sandbox validator checks all three, but that
    validator is a filter and this is the authority. When one gate checked only
    the subject here, a decision wrongly bound on the other two was admitted by
    any route that bypassed the filter — and an Activity is reachable
    independently of the Workflow that normally calls it.
    """

    if decision.subject_sha256 != subject_sha256:
        raise ValueError("Decision subject does not bind the derived gate request")
    if decision.admission_sha256 != subject_sha256:
        raise ValueError("Decision admission hash does not bind the derived gate request")
    if decision.prepared_sha256 != subject_sha256:
        raise ValueError("Decision prepared hash does not bind the derived gate request")
    if decision.run_id != run_id:
        raise ValueError("Decision run does not bind the gate request")
    if decision.gate_id != gate_id:
        raise ValueError("Decision gate does not bind the gate request")
    if decision.policy_revision != policy_revision:
        raise ValueError("Decision was made under a different policy revision")
    # Checked HERE, and this is the clause that was missing. `allowed_actor` is
    # inside the derived bytes precisely so the authority can verify it without
    # trusting anything carried through History.
    if decision.actor != allowed_actor:
        raise ValueError("Decision actor is not authorized for this gate")

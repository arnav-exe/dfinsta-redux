"""The feature gate's missing producer: a stage 4a assessment, recorded.

`feature_gate.py` has been complete and unreachable since it was written, because
the thing it gates on had no producer. The framing carried in the docs — *"stage
4a computes an assessment in the driver world while the gate expects it in CAS"* —
was generous: **nothing computed one at all.** `driver.py` never imports
`assessment`, and the `assess()` that does appear there is the proposal one from
`.proposals`. See `docs/STAGE_4_PRODUCER_DESIGN.md`.

This module records one. It follows option C5 of that note, and the reason is a
measurement: stage 4a is a pure function of `api_surface.json` plus the hook list,
running in **0.00 s**, byte-identical across `PYTHONHASHSEED` values and across
Instagram 430, 439 and 440 (3,696 / 3,844 / 3,831 canonical bytes, four candidates
each). So this side does not *adopt* a caller's document — **it recomputes one**
from the bytes it admitted, and refuses if a supplied document disagrees.

    adopted:      "these bytes were handed to me"
    recomputed:   "these bytes are what this code computes from this recorded input"

Choosing the first when the second costs nothing spends the only thing separating
this gate from a rubber stamp. Where recomputation is genuinely unavailable the
project does the other thing and says so — `replay_gate.resolve_admitted_build`
has to *fetch* a receipt, because rebuilding an APK to check a hash is not free.

===============================================================================
  WHAT IS ADMITTED AND WHAT IS DERIVED — THE LINE MATTERS
===============================================================================

`api_surface.json` is **admitted**, not derived. Nothing in the ledger can attest
that it corresponds to a real Instagram APK, and nothing should pretend to:
`tools/indexer/build_index.py` is not an admitted capability and must not become
one (`CapabilityRole` is deliberately three values, and
`docs/WORKFLOW_REGISTRATION_DESIGN.md` calls widening it "the one genuinely
irreversible mistake available here"). So the chain is:

    admitted:  the API surface bytes, and the decode identity they claim
    derived:   the assessment document, recomputed here from those bytes
    recorded:  a ledger operation whose output ref pins the document

The operation key is keyed on the decode's `content_hash` rather than on the
surface file's own digest, because the file embeds `generated_at` and an absolute
`decode_path` — **measured** — so re-indexing the same decode changes its bytes
while changing nothing that matters. Keying on the content hash makes a re-index
idempotent instead of a second, conflicting operation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .assessment import (
    AssessmentError,
    canonical_bytes,
    candidate_ids,
    document as build_document,
    policy_revision as read_policy_revision,
)
from .contracts import ID_PATTERN, ArtifactRef, canonical_json, canonical_sha256
from .hook_index import API_SURFACE_FILENAME, HEADER_FILENAME, HookIndex
from .hook_manifest import load_manifest
from .ledger import Ledger, assessment_identity
from .store import ContentStore

__all__ = [
    "RecordError",
    "RecordedAssessment",
    "ASSESSMENT_OPERATION_KIND",
    "API_SURFACE_ARTIFACT_KIND",
    "ASSESSMENT_ARTIFACT_KIND",
    "operation_input",
    "record",
    "resolve",
    "resolve_with",
    "main",
]


class RecordError(ValueError):
    """Raised when an assessment cannot be recorded from what is on hand."""


#: The operation kind. Versioned, because the recorded document's shape is part
#: of what a re-derivation reproduces.
ASSESSMENT_OPERATION_KIND = "stage4-assessment-v1"

#: The admitted input.
API_SURFACE_ARTIFACT_KIND = "api-surface-v1"

#: The derived output. Must equal `feature_gate.ASSESSMENT_ARTIFACT_KIND`, which
#: `FeatureGateRequestV1` requires of the ref it pins; a mismatch here would
#: produce a ref the gate rejects, one layer away from where it was made.
ASSESSMENT_ARTIFACT_KIND = "feature-assessment-v1"


@dataclass(frozen=True)
class RecordedAssessment:
    """A recorded assessment and everything needed to re-derive its gate subject."""

    run_id: str
    operation_key: str
    input_sha256: str
    assessment: ArtifactRef
    document: Mapping[str, Any]
    candidate_ids: tuple[str, ...]
    policy_revision: str
    allowed_actor: str


def operation_input(
    run_id: str,
    decode_content_hash: str,
    manifest_sha256: str,
    policy_revision: str,
) -> dict[str, str]:
    """What this operation is *of*. Its canonical hash is the operation key.

    Everything here must be reachable by a party holding only a run id, because
    the operation tables are indexed by content hash and a client that cannot
    rebuild this cannot find the operation. That is precisely why
    `PortRunWorkflow`'s `phase-a-approval` is unanswerable: you need the spec to
    find the operation that would give you the spec. Here the run-keyed authority
    row carries these four values, so the client rebuilds the key rather than
    guessing it.
    """
    return {
        "run_id": run_id,
        "decode_content_hash": decode_content_hash,
        "manifest_sha256": manifest_sha256,
        "policy_revision": policy_revision,
    }


def _bare_hash(value: str) -> str:
    """`index.json` writes `"sha256:0a2a…"`; `SHA256_PATTERN` wants bare hex."""
    return value.split(":", 1)[1] if value.startswith("sha256:") else value


def _index_from_bytes(header: bytes, surface: bytes) -> HookIndex:
    """Rebuild the reader from admitted bytes, not from a directory on disk.

    The point of recording is that the derivation reads the *recorded* input. A
    version of this that re-read the caller's index directory would be checking
    that the caller's disk agrees with itself.
    """
    try:
        header_doc = json.loads(header.decode("utf-8"))
        surface_doc = json.loads(surface.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RecordError(f"admitted index bytes are not readable JSON: {error}") from error
    if not isinstance(header_doc, dict) or not isinstance(surface_doc, dict):
        raise RecordError("admitted index bytes are not JSON objects")
    return HookIndex(Path("<admitted>"), header_doc, surface_doc)


def record(
    state_root: Path | str,
    *,
    run_id: str,
    index_dir: Path | str,
    manifest_path: Path | str,
    allowed_actor: str,
    owner_token: str,
    expect_document_sha256: str | None = None,
) -> RecordedAssessment:
    """Admit the API surface, recompute the assessment, record the operation.

    `expect_document_sha256` is how a caller that computed its own copy — the
    driver, say — has it *checked* rather than trusted. A disagreement is an
    error, not a warning: two derivations that disagree mean one of them is
    reading something the other is not, and picking a winner silently is how the
    wrong one wins.
    """
    for value, label in ((run_id, "run id"), (allowed_actor, "allowed actor")):
        if type(value) is not str or not ID_PATTERN.fullmatch(value):
            raise RecordError(f"{label} must be an identifier, got {value!r}")
    if type(owner_token) is not str or not owner_token:
        raise RecordError("owner token must be a non-empty string")

    index_dir = Path(index_dir)
    manifest_path = Path(manifest_path)
    try:
        header_bytes = (index_dir / HEADER_FILENAME).read_bytes()
        surface_bytes = (index_dir / API_SURFACE_FILENAME).read_bytes()
        manifest_bytes = manifest_path.read_bytes()
    except OSError as error:
        raise RecordError(f"cannot read the assessment's inputs: {error}") from error

    index = _index_from_bytes(header_bytes, surface_bytes)
    decode_content_hash = _bare_hash(index.content_hash)
    if not decode_content_hash:
        raise RecordError(
            f"{index_dir / HEADER_FILENAME} declares no content_hash; without it the "
            "operation key would be keyed on bytes that change every time the index "
            "is rebuilt"
        )
    revision = read_policy_revision(manifest_path)
    hooks = load_manifest(manifest_path)

    document = build_document(index, hooks)
    body = canonical_bytes(document)
    names = candidate_ids(document)
    digest = canonical_sha256(document)
    if expect_document_sha256 is not None and expect_document_sha256 != digest:
        raise RecordError(
            f"the supplied assessment digest {expect_document_sha256} is not what this "
            f"input computes ({digest}). Two derivations disagree; neither is adopted."
        )

    # The file's own digest, so `sha256sum manifest/hooks.json` reproduces it.
    # Hashing the JSON-quoted *text* was deterministic and therefore harmless, and
    # also a number stored under a name no human could check.
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    payload = operation_input(run_id, decode_content_hash, manifest_sha256, revision)
    operation_key = canonical_sha256({"kind": ASSESSMENT_OPERATION_KIND, "input": payload})
    input_sha256 = canonical_sha256(payload)

    state_root = Path(state_root).resolve()
    store = ContentStore(state_root / "cas")
    ledger = Ledger(state_root / "ledger.sqlite3")

    authority = {
        "run_id": run_id,
        "operation_key": operation_key,
        "input_sha256": input_sha256,
        "document_sha256": digest,
        "api_surface_sha256": "",  # filled once the surface is in CAS
        "manifest_sha256": manifest_sha256,
        "policy_revision": revision,
        "allowed_actor": allowed_actor,
    }
    # Refuse BEFORE writing anything. The conflict is fully decidable from values
    # already computed, so discovering it after two CAS blobs and a completed
    # operation had been written would leave the ledger carrying a derivation
    # nothing references — and on the re-index path, one more orphan blob per
    # attempt.
    try:
        recorded_already = ledger.recorded_assessment_for_run(run_id)
    except ValueError:
        recorded_already = None
    if recorded_already is not None and assessment_identity(
        recorded_already
    ) != assessment_identity(authority):
        raise RecordError(
            f"a different assessment is already recorded for run {run_id!r}; a run must "
            "not silently gain a second one, because nobody could then say which a "
            "human was shown"
        )

    surface_ref = store.put_bytes(
        kind=API_SURFACE_ARTIFACT_KIND,
        data=surface_bytes,
        producer_operation_id=operation_key,
        input_hashes=(decode_content_hash,),
    )
    existing = ledger.begin_operation(
        operation_key,
        ASSESSMENT_OPERATION_KIND,
        input_sha256,
        owner_token,
        retry_safe=True,
    )
    if existing is None:
        assessment_ref = store.put_bytes(
            kind=ASSESSMENT_ARTIFACT_KIND,
            data=body,
            producer_operation_id=operation_key,
            input_hashes=(decode_content_hash, surface_ref.sha256, manifest_sha256),
        )
        ledger.record_effect(operation_key, owner_token, assessment_ref)
        ledger.complete_operation(operation_key, assessment_ref)
    else:
        # A completed operation is the answer, not an obstacle: same input, same
        # derivation, same bytes. Re-deriving and comparing is cheap here, so do
        # it rather than trusting the adoption.
        assessment_ref = existing
        if assessment_ref.sha256 != digest:
            raise RecordError(
                f"a completed {ASSESSMENT_OPERATION_KIND} for this input records "
                f"{assessment_ref.sha256}, but this input now computes {digest}"
            )

    recorded = RecordedAssessment(
        run_id=run_id,
        operation_key=operation_key,
        input_sha256=input_sha256,
        assessment=assessment_ref,
        document=document,
        candidate_ids=names,
        policy_revision=revision,
        allowed_actor=allowed_actor,
    )
    ledger.record_assessment_authority(
        {**authority, "api_surface_sha256": surface_ref.sha256}
    )
    return recorded


def resolve_with(ledger: Ledger, store: ContentStore, run_id: str) -> RecordedAssessment:
    """Re-derive everything the gate needs, from a run id and recorded state alone.

    This is the client's path, and it is the reason the authority row exists. It
    reaches the `ArtifactRef` through `require_completed_operation` rather than
    rebuilding one, because `producer_operation_id` and `input_hashes` are inside
    the gate's subject hash — **measured**: changing either changes the request
    hash — so a reconstructed ref makes the client refuse every genuine gate.

    The candidate ids come from the recorded document's own bytes through the one
    decoder in `assessment.py`. A list supplied by a caller would let the human
    rule on candidates the pinned bytes do not contain, and
    `feature_gate.validate_submission` never re-reads the blob, so nothing
    downstream would catch it.

    **The ledger and store are passed in rather than built here** so the trusted
    submission client and a preparing Activity call literally the same function
    over the same handles. A version that opened its own connection from a path
    would give the client a second, unguarded route to the ledger, quietly
    stepping around the single `configure_runtime(..., read_only=True)` that is
    supposed to be the one statement about what the client may do.
    """
    row = ledger.recorded_assessment_for_run(run_id)
    ref = ledger.require_completed_operation(
        row["operation_key"], ASSESSMENT_OPERATION_KIND, row["input_sha256"]
    )
    if ref.sha256 != row["document_sha256"]:
        raise RecordError(
            "the recorded operation's output does not match the authority row's document"
        )
    body = store.read_blob(ref.sha256, ref.size)
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RecordError(f"recorded assessment is not readable JSON: {error}") from error
    if canonical_bytes(document) != body:
        # The bytes in CAS are what the digest pins; if re-canonicalising them
        # changes them, the recorded document is not in canonical form and the
        # digest a human signs would not be the digest of what they were shown.
        raise RecordError("recorded assessment bytes are not canonical")
    return RecordedAssessment(
        run_id=run_id,
        operation_key=row["operation_key"],
        input_sha256=row["input_sha256"],
        assessment=ref,
        document=document,
        candidate_ids=candidate_ids(document),
        policy_revision=row["policy_revision"],
        allowed_actor=row["allowed_actor"],
    )


def resolve(state_root: Path | str, run_id: str, *, read_only: bool = True) -> RecordedAssessment:
    """`resolve_with` for a caller that has a state root and no runtime.

    Read-only by default, because the only reason to call this rather than
    :func:`resolve_with` is that you are outside the Temporal world — and
    everything outside it that reads a recorded assessment is reading, not
    writing.
    """
    state_root = Path(state_root).resolve()
    return resolve_with(
        Ledger(state_root / "ledger.sqlite3", read_only=read_only),
        ContentStore(state_root / "cas"),
        run_id,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    write = sub.add_parser("record", help="admit, recompute and record an assessment")
    write.add_argument("--state-root", type=Path, required=True)
    write.add_argument("--run-id", required=True)
    write.add_argument("--index", type=Path, required=True, help="an index directory")
    write.add_argument("--manifest", type=Path, default=Path("manifest/hooks.json"))
    write.add_argument("--actor", required=True, help="who may answer the gate")
    write.add_argument("--owner-token", required=True)
    write.add_argument("--expect", help="a digest this input must compute, checked not trusted")

    read = sub.add_parser("show", help="re-derive from a run id, read-only")
    read.add_argument("--state-root", type=Path, required=True)
    read.add_argument("--run-id", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "record":
            recorded = record(
                args.state_root,
                run_id=args.run_id,
                index_dir=args.index,
                manifest_path=args.manifest,
                allowed_actor=args.actor,
                owner_token=args.owner_token,
                expect_document_sha256=args.expect,
            )
        else:
            recorded = resolve(args.state_root, args.run_id)
    except (RecordError, AssessmentError, ValueError, FileNotFoundError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(f"run              {recorded.run_id}")
    print(f"operation        {recorded.operation_key}")
    print(f"assessment       {recorded.assessment.uri} ({recorded.assessment.size} bytes)")
    print(f"policy_revision  {recorded.policy_revision}")
    print(f"allowed_actor    {recorded.allowed_actor}")
    print(f"candidates       {len(recorded.candidate_ids)}")
    for name in recorded.candidate_ids:
        print(f"    {name}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

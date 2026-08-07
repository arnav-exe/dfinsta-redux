"""Record a retirement docket so a gate can be raised on it, and re-derive it.

The submission client resolves a gate subject from **a run id and nothing else**
— `GateKind.resolve` takes one argument. The operation tables are keyed by
content hash, so without a run-keyed authority row a gate is structurally
unanswerable, which is exactly why `PortRunWorkflow`'s `phase-a-approval` is
deliberately unregistered rather than trusted. This module writes that row for
the hook-retirement gate and reads it back.

    python -m dfinsta_pipeline.retirement_record record --state-root <dir> \
        --run-id retire-441 --version 441 --investigations <json> \
        --allowed-actor arnav --owner-token <token>
    python -m dfinsta_pipeline.retirement_record show --state-root <dir> --run-id retire-441

===============================================================================
  RECOMPUTE, DO NOT ADOPT
===============================================================================

The docket is rebuilt here from the committed evidence and the supplied
investigations, never taken from a caller. `--expect-docket-sha256` lets a caller
say what it thinks the answer is and gets an error if it disagrees; it does not
let a caller supply one. Recomputation is affordable — the docket is a few
kilobytes derived from files already on disk — and where it genuinely is not
affordable this project fetches a receipt instead and says so.

===============================================================================
  WHAT KEYS THE OPERATION
===============================================================================

Not the docket's own digest. The operation is keyed on the *inputs* the docket
derives from — the version, the hook manifest, the evidence files read, the
investigations, and the policy revision — so re-recording an identical docket is
idempotent rather than minting a second conflicting operation for the same
result. The precedent is `assessment_record`, which keys on the decode's
`content_hash` and not on the surface file, because the surface file embeds a
timestamp and re-indexing the same decode changes its bytes and nothing else.

The evidence contributes as `{relative path: sha256}` rather than as a directory
digest. A directory digest would change when an unrelated version's file arrived,
and a docket about 441 is not different because 442 was ported.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .assessment import policy_revision as read_policy_revision
from .contracts import ArtifactRef, canonical_json, canonical_sha256
from .expectation import versions_with_evidence
from .history import BASELINE_VERSION, _NUMERIC
from .ledger import Ledger
from .retirement import (
    Investigation,
    RetirementCase,
    RetirementError,
    build_case,
    candidates,
)
from .retirement_gate import DOCKET_ARTIFACT_KIND
from .store import ContentStore

__all__ = [
    "RecordError",
    "DOCKET_OPERATION_KIND",
    "RecordedDocket",
    "read_investigations",
    "build_docket",
    "operation_input",
    "record",
    "resolve",
    "resolve_with",
    "main",
]


class RecordError(ValueError):
    """One error type out of this module, so a caller has one thing to catch."""


DOCKET_OPERATION_KIND = "hook-retirement-docket-v1"


@dataclass(frozen=True)
class RecordedDocket:
    """Everything the gate needs, reached from a run id."""

    run_id: str
    operation_key: str
    input_sha256: str
    docket: ArtifactRef
    document: Mapping[str, Any]
    hook_ids: tuple[str, ...]
    version: str
    policy_revision: str
    allowed_actor: str


def read_investigations(path: Path | str) -> dict[str, Investigation]:
    """`{hook_id: Investigation}` from a JSON object.

    An agent writes this file. It is the only part of a docket that is not
    derived from committed state, which is precisely why it is pinned into the
    operation key: two dockets built from the same evidence and *different*
    investigations are different questions, and a human who read one must not be
    recorded as having answered the other.
    """

    location = Path(path)
    try:
        data = json.loads(location.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RecordError(f"{location}: {error}") from error
    if not isinstance(data, dict):
        raise RecordError(f"{location}: investigations must be a JSON object keyed by hook")
    out: dict[str, Investigation] = {}
    for hook, item in data.items():
        try:
            out[str(hook)] = Investigation.from_dict(item)
        except RetirementError as error:
            raise RecordError(f"{location}: {hook}: {error}") from error
    return out


def _evidence_digests(root: Path, version: str, baseline: str) -> dict[str, str]:
    """`{relative path: sha256}` for every evidence file the docket rests on.

    Per file rather than per directory: a docket about 441 must not change its
    identity because 442 was ported, and a directory digest would do exactly
    that.
    """

    series = [
        item
        for item in versions_with_evidence(root, baseline=baseline)
        if int(item) <= int(version)
    ]
    out: dict[str, str] = {}
    for index, item in enumerate(series):
        paths = [
            root / "manifest" / "static_evidence" / f"{item}.jsonl",
            root / "manifest" / "runtime_evidence" / f"{item}.jsonl",
        ]
        if index:
            paths.append(
                root / "manifest" / "differentials" / f"{series[index - 1]}-{item}.jsonl"
            )
        for path in paths:
            if path.is_file():
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                out[str(path.relative_to(root))] = digest
    if not out:
        raise RecordError(
            f"no committed evidence at or before {version}; there is nothing to build "
            "a docket from"
        )
    return out


def build_docket(
    root: Path | str,
    *,
    version: str,
    investigations: Mapping[str, Investigation],
    policy_revision: str,
    baseline: str = BASELINE_VERSION,
) -> tuple[dict[str, Any], tuple[RetirementCase, ...]]:
    """Every open retirement case at `version`, as one signable document.

    Refuses a candidate with no investigation rather than building a case with an
    empty one. A docket entry saying "this hook is red and nobody looked into it"
    is a request to rule without evidence, and the answer it invites is the one
    this whole design exists to make expensive.
    """

    root = Path(root)
    if not _NUMERIC.fullmatch(version):
        raise RecordError(f"{version!r} is not a version number")
    try:
        open_cases = candidates(root, version=version, baseline=baseline)
    except RetirementError as error:
        raise RecordError(str(error)) from error
    if not open_cases:
        raise RecordError(
            f"every assessed hook is release-ready at {version}. A gate with no "
            "question does not need a human"
        )

    missing = sorted(s.hook_id for s in open_cases if s.hook_id not in investigations)
    if missing:
        raise RecordError(
            f"no investigation for {', '.join(missing)}. Every hook in a docket needs "
            "one: a case that says only that the numbers are red is a request to rule "
            "without evidence"
        )
    extra = sorted(set(investigations) - {s.hook_id for s in open_cases})
    if extra:
        raise RecordError(
            f"investigation for {', '.join(extra)}, which is not a candidate at "
            f"{version}. Either the hook is release-ready or it was already retired"
        )

    cases: list[RetirementCase] = []
    for standing in open_cases:
        try:
            cases.append(
                build_case(
                    root,
                    hook_id=standing.hook_id,
                    version=version,
                    investigation=investigations[standing.hook_id],
                    baseline=baseline,
                )
            )
        except RetirementError as error:
            raise RecordError(f"{standing.hook_id}: {error}") from error

    document = {
        "schema_version": 1,
        "version": version,
        "policy_revision": policy_revision,
        "cases": [case.to_dict() for case in cases],
    }
    return document, tuple(cases)


def operation_input(
    run_id: str,
    version: str,
    manifest_sha256: str,
    evidence: Mapping[str, str],
    investigations_sha256: str,
    policy_revision: str,
) -> dict[str, Any]:
    """What determines the docket. Everything, and nothing derived from it."""

    return {
        "run_id": run_id,
        "version": version,
        "manifest_sha256": manifest_sha256,
        "evidence": dict(sorted(evidence.items())),
        "investigations_sha256": investigations_sha256,
        "policy_revision": policy_revision,
    }


def record(
    state_root: Path | str,
    *,
    run_id: str,
    version: str,
    investigations_path: Path | str,
    allowed_actor: str,
    owner_token: str,
    root: Path | str = ".",
    manifest_path: Path | str | None = None,
    baseline: str = BASELINE_VERSION,
    expect_docket_sha256: str | None = None,
) -> RecordedDocket:
    """Build the docket, put it in CAS, and file the run-keyed authority row."""

    try:
        return _record(
            state_root,
            run_id=run_id,
            version=version,
            investigations_path=investigations_path,
            allowed_actor=allowed_actor,
            owner_token=owner_token,
            root=root,
            manifest_path=manifest_path,
            baseline=baseline,
            expect_docket_sha256=expect_docket_sha256,
        )
    # One error type out of this module, matching `assessment_record.record`. A
    # caller that had to catch four unrelated exception types would catch
    # `Exception`, and then a genuine bug in the ledger would read as a refusal.
    except RetirementError as error:
        raise RecordError(str(error)) from error
    except (TypeError, ValueError) as error:
        if isinstance(error, RecordError):
            raise
        raise RecordError(str(error)) from error


def _record(
    state_root: Path | str,
    *,
    run_id: str,
    version: str,
    investigations_path: Path | str,
    allowed_actor: str,
    owner_token: str,
    root: Path | str,
    manifest_path: Path | str | None,
    baseline: str,
    expect_docket_sha256: str | None,
) -> RecordedDocket:
    root = Path(root)
    manifest = Path(manifest_path) if manifest_path else root / "manifest" / "hooks.json"
    state_root = Path(state_root)

    manifest_bytes = manifest.read_bytes()
    investigations_bytes = Path(investigations_path).read_bytes()
    investigations = read_investigations(investigations_path)
    revision = read_policy_revision(manifest)

    document, cases = build_docket(
        root,
        version=version,
        investigations=investigations,
        policy_revision=revision,
        baseline=baseline,
    )
    body = canonical_json(document).encode("utf-8")
    docket_sha256 = hashlib.sha256(body).hexdigest()
    if expect_docket_sha256 is not None and expect_docket_sha256 != docket_sha256:
        raise RecordError(
            f"docket is {docket_sha256}, not the expected {expect_docket_sha256}. This "
            "module recomputes rather than adopting; a disagreement means the evidence "
            "or the investigations moved"
        )

    payload = operation_input(
        run_id,
        version,
        hashlib.sha256(manifest_bytes).hexdigest(),
        _evidence_digests(root, version, baseline),
        hashlib.sha256(investigations_bytes).hexdigest(),
        revision,
    )
    key = canonical_sha256({"kind": DOCKET_OPERATION_KIND, "input": payload})
    input_sha256 = canonical_sha256(payload)

    ledger = Ledger(state_root / "ledger.sqlite3")
    store = ContentStore(state_root / "cas")

    # Conflict checked BEFORE any write, as `assessment_record` does. A refused
    # record that had already put a blob in CAS leaves an orphan nothing points
    # at, and the store has no sweeper.
    try:
        existing_row = ledger.recorded_retirement_docket_for_run(run_id)
    except ValueError:
        existing_row = None
    if existing_row is not None and existing_row.get("operation_key") != key:
        raise RecordError(
            f"a different retirement docket is already recorded for {run_id}. Two "
            "dockets under one run is the state where nobody can say which one the "
            "human read"
        )

    adopted = ledger.begin_operation(
        key, DOCKET_OPERATION_KIND, input_sha256, owner_token, retry_safe=True
    )
    if adopted is None:
        reference = store.put_bytes(
            kind=DOCKET_ARTIFACT_KIND,
            data=body,
            # The operation key itself. `record_effect` refuses an artifact whose
            # `producer_operation_id` is anything else — the ref carries its own
            # lineage, and a descriptive-but-wrong id would make the blob
            # unattachable to the operation that produced it.
            producer_operation_id=key,
            input_hashes=(input_sha256,),
        )
        ledger.record_effect(key, owner_token, reference)
        ledger.complete_operation(key, reference)
    else:
        reference = adopted
        if reference.sha256 != docket_sha256:
            raise RecordError(
                "the recorded docket does not match the one just computed from the "
                "same inputs"
            )

    ledger.record_retirement_docket_authority(
        {
            "run_id": run_id,
            "operation_key": key,
            "input_sha256": input_sha256,
            "docket_sha256": reference.sha256,
            "version": version,
            "policy_revision": revision,
            "allowed_actor": allowed_actor,
        }
    )
    return RecordedDocket(
        run_id=run_id,
        operation_key=key,
        input_sha256=input_sha256,
        docket=reference,
        document=document,
        hook_ids=tuple(case.hook_id for case in cases),
        version=version,
        policy_revision=revision,
        allowed_actor=allowed_actor,
    )


def resolve_with(ledger: Ledger, store: ContentStore, run_id: str) -> RecordedDocket:
    """Re-derive the docket for a run, from recorded state alone.

    Goes through `require_completed_operation` rather than reading the blob by
    the row's digest: the row carries **coordinates**, so the operation's own
    checks are not bypassed for a caller who happens to hold a run id.
    """

    row = ledger.recorded_retirement_docket_for_run(run_id)
    reference = ledger.require_completed_operation(
        row["operation_key"], DOCKET_OPERATION_KIND, row["input_sha256"]
    )
    body = store.read_blob(reference.sha256, reference.size)
    document = json.loads(body.decode("utf-8"))
    if canonical_json(document).encode("utf-8") != body:
        raise RecordError("the recorded docket is not in canonical form")
    if document.get("version") != row["version"]:
        raise RecordError("the recorded docket names a different version than its row")
    return RecordedDocket(
        run_id=run_id,
        operation_key=row["operation_key"],
        input_sha256=row["input_sha256"],
        docket=reference,
        document=document,
        hook_ids=tuple(case["hook_id"] for case in document["cases"]),
        version=str(row["version"]),
        policy_revision=str(row["policy_revision"]),
        allowed_actor=str(row["allowed_actor"]),
    )


def resolve(
    state_root: Path | str, run_id: str, *, read_only: bool = True
) -> RecordedDocket:
    state_root = Path(state_root)
    return resolve_with(
        Ledger(state_root / "ledger.sqlite3", read_only=read_only),
        ContentStore(state_root / "cas"),
        run_id,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--baseline", default=BASELINE_VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    recording = sub.add_parser("record")
    recording.add_argument("--run-id", required=True)
    recording.add_argument("--version", required=True)
    recording.add_argument("--investigations", type=Path, required=True)
    recording.add_argument("--allowed-actor", required=True)
    recording.add_argument("--owner-token", required=True)
    recording.add_argument("--manifest", type=Path)
    recording.add_argument("--expect-docket-sha256")

    showing = sub.add_parser("show")
    showing.add_argument("--run-id", required=True)
    showing.add_argument("--document", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "record":
            recorded = record(
                args.state_root,
                run_id=args.run_id,
                version=args.version,
                investigations_path=args.investigations,
                allowed_actor=args.allowed_actor,
                owner_token=args.owner_token,
                root=args.root,
                manifest_path=args.manifest,
                baseline=args.baseline,
                expect_docket_sha256=args.expect_docket_sha256,
            )
        else:
            recorded = resolve(args.state_root, args.run_id)
            if args.document:
                print(json.dumps(recorded.document, indent=2, sort_keys=True))
                return 0
    except (RecordError, OSError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2

    print(f"run              {recorded.run_id}")
    print(f"version          {recorded.version}")
    print(f"operation key    {recorded.operation_key}")
    print(f"docket           {recorded.docket.sha256}")
    print(f"hooks            {', '.join(recorded.hook_ids)}")
    print(f"policy revision  {recorded.policy_revision}")
    print(f"may answer       {recorded.allowed_actor}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

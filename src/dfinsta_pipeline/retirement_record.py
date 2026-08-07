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
from .retirement import Ruling, publish
from .retirement_gate import DOCKET_ARTIFACT_KIND, RetirementRulingsV1
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
    "admitted_rulings",
    "publish_admitted",
    "raise_gate",
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


def admitted_rulings(
    ledger: Ledger, store: ContentStore, run_id: str
) -> tuple[RetirementRulingsV1, str]:
    """The rulings a human actually made, and the decision that carries them.

    Reads the run-keyed row the admitting Activity wrote, fetches the document by
    its digest and size, and cross-checks the docket it names against the row.
    The cross-check is not ceremony: the row and the blob are two records of one
    fact, and a reader that trusted either alone would not notice them diverging.
    """

    row = ledger.admitted_retirement_rulings_for_run(run_id)
    body = store.read_blob(str(row["rulings_sha256"]), int(row["rulings_size"]))
    document = RetirementRulingsV1.from_dict(json.loads(body.decode("utf-8")))
    if document.docket_sha256 != row["docket_sha256"]:
        raise RecordError("the admitted rulings name a different docket than their row")
    return document, str(row["decision_id"])


def publish_admitted(
    state_root: Path | str,
    run_id: str,
    *,
    root: Path | str = ".",
    path: Path | str | None = None,
) -> list[str]:
    """Turn admitted rulings into rows `expectation` reads. The consumer.

    **This function is the reason the gate is worth having.** Three times this
    project has shipped a gate that was complete and disconnected at one end —
    `the-gates-rulings-have-no-consumer`, `nothing-computes-a-stage-4a-assessment`,
    `the-post-build-gate-cannot-be-satisfied` — and a retirement that was
    approved, admitted and never written would be the fourth.

    Each `retire` becomes a row; `keep` and `defer` write nothing, because the
    file's only meaning is "no longer expected". The row's `decision_id` is the
    **gate's**, so the permanent record points back at the decision a human
    signed rather than at an id this module minted for itself.
    """

    state_root = Path(state_root)
    ledger = Ledger(state_root / "ledger.sqlite3", read_only=True)
    store = ContentStore(state_root / "cas")
    document, decision_id = admitted_rulings(ledger, store, run_id)
    recorded = resolve_with(ledger, store, run_id)

    cases = {case["hook_id"]: case for case in recorded.document["cases"]}
    written: list[str] = []
    for item in document.rulings:
        if item.verdict != "retire":
            continue
        case = RetirementCase.from_dict(cases[item.hook_id])
        ruling = Ruling(
            schema_version=1,
            hook_id=item.hook_id,
            verdict=item.verdict,
            rationale=item.rationale,
            # The actor is not in the rulings document — it is in the decision the
            # ledger recorded — so `ruled_by` comes from the docket's allowed
            # actor, which `validate_submission` has already proved the decision's
            # actor equals. Two names for one fact, checked equal before either
            # was trusted.
            ruled_by=recorded.allowed_actor,
            case_sha256=item.case_sha256,
            decision_id=decision_id,
            ruled_at=str(recorded.document.get("recorded_at") or "") or _now_from(document),
        )
        publish(case, ruling, root=root, path=path, decision_id=decision_id)
        written.append(item.hook_id)
    return written


def _now_from(document: RetirementRulingsV1) -> str:
    """A stamp for a published row when the docket carries none.

    Not a clock read. This layer must never read the clock — a replay would then
    rewrite a line with a new time and a reader could not order two records — so
    the fallback is derived from the document itself and is stable across
    replays. It is a marker that the gate produced this row, not a wall time, and
    it is deliberately ugly so nobody mistakes it for one.
    """

    return f"admitted:{document.sha256[:16]}"


def raise_gate(
    endpoint: str,
    task_queue: str,
    run_id: str,
    *,
    gate_timeout_seconds: int,
) -> str:
    """Start the Workflow that asks a human, and return its id. The starter.

    **Nothing in `src/` or `tools/` starts any other gate.** `start_workflow` for
    `FeatureAssessmentRunWorkflow` appears only in an integration script, so that
    gate is raisable by hand and by nothing else — a disconnection at the far end
    from the one the consumer above closes. Reproducing it here would have made
    this gate complete and unraisable, which is the exact shape this project keeps
    shipping.

    Imported lazily: `temporalio` is the only runtime dependency and everything
    else in this module must work with no server anywhere near it.
    """

    import asyncio

    from temporalio.client import Client

    from .retirement_gate import RetirementRunRequestV1
    from .retirement_workflow import HookRetirementRunWorkflow

    async def _start() -> str:
        client = await Client.connect(endpoint)
        handle = await client.start_workflow(
            HookRetirementRunWorkflow.run,
            RetirementRunRequestV1(1, run_id, gate_timeout_seconds),
            # The run id IS the workflow id, as the other gates do it. That is
            # what makes `submission show <workflow_id>` reach the right gate
            # from the only identifier a human has.
            id=run_id,
            task_queue=task_queue,
        )
        return handle.id

    return asyncio.run(_start())


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

    raising = sub.add_parser("raise", help="start the Workflow that asks a human")
    raising.add_argument("--run-id", required=True)
    raising.add_argument("--endpoint", default="localhost:7233")
    raising.add_argument("--task-queue", default="dfinsta")
    raising.add_argument(
        "--gate-timeout-seconds",
        type=int,
        default=7 * 24 * 3600,
        help="how long the gate stays open. A week by default: this is the gate "
        "whose whole purpose is to outlast a weekend, and an unanswered one leaves "
        "every hook still expected",
    )

    publishing = sub.add_parser(
        "publish", help="write admitted rulings into manifest/retirements.jsonl"
    )
    publishing.add_argument("--run-id", required=True)
    publishing.add_argument("--retirements", type=Path)

    args = parser.parse_args(argv)
    try:
        if args.command == "raise":
            workflow_id = raise_gate(
                args.endpoint,
                args.task_queue,
                args.run_id,
                gate_timeout_seconds=args.gate_timeout_seconds,
            )
            print(f"raised {workflow_id}")
            print(
                "Answer it with:  python -m dfinsta_pipeline.submission "
                f"--state-root {args.state_root} show {workflow_id}"
            )
            return 0

        if args.command == "publish":
            retired = publish_admitted(
                args.state_root, args.run_id, root=args.root, path=args.retirements
            )
            if not retired:
                print(
                    f"{args.run_id}: nothing retired. Every hook was ruled keep or "
                    "defer, and all of them stay expected."
                )
            else:
                print(f"retired: {', '.join(retired)}")
                print(
                    "Commit the file: the expectation reads the committed one, and an "
                    "uncommitted row works here and vanishes on clone."
                )
            return 0

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

"""Record a reversal docket so a gate can be raised on it, and act on the answer.

The submission client resolves a gate subject from **a run id and nothing else**
— `GateKind.resolve` takes one argument. The operation tables are keyed by content
hash, so without a run-keyed authority row a gate is structurally unanswerable,
which is exactly why `PortRunWorkflow`'s `phase-a-approval` is deliberately
unregistered rather than trusted. This module writes that row for the reversal
gate, reads it back, starts the Workflow, and — the part that matters — turns an
admitted answer into the recorded withdrawal and the manifest edit it implies.

    python -m dfinsta_pipeline.reversal_record record --state-root <dir> \
        --run-id reconsider-441 --version 441 --allowed-actor arnav \
        --owner-token <token> [--index <decode index>]
    python -m dfinsta_pipeline.reversal_record show --state-root <dir> --run-id reconsider-441
    python -m dfinsta_pipeline.reversal_record raise --state-root <dir> \
        --run-id reconsider-441 --build-id <the worker's>
    python -m dfinsta_pipeline.reversal_record publish --state-root <dir> \
        --run-id reconsider-441 --recorded-at 2026-08-09T10:00:00Z --confirm

===============================================================================
  RECOMPUTE, DO NOT ADOPT
===============================================================================

The docket is rebuilt here from committed evidence by `reconsider.reconsiderations`
— the same function the human-facing report calls — and never taken from a caller.
`--expect-docket-sha256` lets a caller say what it thinks the answer is and gets an
error if it disagrees; it does not let a caller supply one.

===============================================================================
  A SWEEP THAT COULD NOT FINISH IS NOT A CLEAN SWEEP
===============================================================================

`reconsiderations()` returns `(found, not_run)`. `build_docket` refuses to build a
docket with no findings **and** a non-empty `not_run`: that combination is not
"nothing to ask", it is "the rules that could have found something did not run",
and recording it as an empty docket would put a reassuring silence where a
question belongs. When there *are* findings, `not_run` travels inside the signed
document so the human can see what the sweep could not check.

===============================================================================
  WHAT KEYS THE OPERATION
===============================================================================

Not the docket's own digest, following `retirement_record`: the operation is keyed
on the inputs the docket derives from, so re-recording an identical docket is
idempotent rather than minting a second conflicting operation for the same result.

The inputs are every file `reconsider` actually reads — the hook manifest, the
rulings store, the retirements and reversals files, the app source the block
enforcement check reads, this version's observation store, **every** evidence file
from the baseline (not only those at or before the version — see
`_input_digests`), and the two index files the endpoint-absence rule searches.
Each contributes as `{relative path: sha256}`, and a **missing** file contributes
an empty digest rather than being omitted, because "the app source was not there"
is an input: it is the difference between a rule that ran and a rule that reported
it could not.
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
from .contracts import ID_PATTERN, ArtifactRef, canonical_json, canonical_sha256
from .expectation import versions_with_evidence
from .history import BASELINE_VERSION, _NUMERIC
from .observation import OBSERVATIONS
from .ledger import Ledger
from .reconsider import ReconsiderError, Reconsideration, reconsiderations
from .reversal import REVERSALS, Reversal, ReversalError, apply_unblock, plan_unblock
from .reversal import append as append_reversal
from .reversal import withdrawn
from .reversal_gate import (
    DOCKET_ARTIFACT_KIND,
    WITHDRAWING_VERDICT,
    ReversalGateError,
    ReversalRulingsV1,
    ReversalSubjectV1,
    docket_subjects,
    item_id as derive_item_id,
)
from .store import ContentStore

__all__ = [
    "RecordError",
    "DOCKET_OPERATION_KIND",
    "RecordedReversalDocket",
    "PublishedReversals",
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


DOCKET_OPERATION_KIND = "reversal-docket-v1"

#: The files `reconsider` reads that are not evidence files. Always keys of the
#: operation input, with an empty digest when absent: a rule that could not read
#: the app source reports a different result from one that read it and found
#: everything enforced, so the two must not share an operation key.
_DECISION_INPUTS = (
    Path("manifest") / "hooks.json",
    Path("manifest") / "rulings.jsonl",
    Path("manifest") / "retirements.jsonl",
    REVERSALS,
    Path("dfinsta_source_439") / "newCode" / "com" / "dfinstagram" / "hooks.smali",
)

#: The index files the endpoint-absence rule actually reads. `structural.jsonl` is
#: only checked for existence by `HookIndex.load` and is 65 MB on a real decode,
#: so it contributes its presence and not its bytes — `descriptors_with_literal`
#: reads `api_surface.json` alone.
_INDEX_INPUTS = ("header.json", "api_surface.json")
_INDEX_PRESENCE_ONLY = ("structural.jsonl",)


@dataclass(frozen=True)
class RecordedReversalDocket:
    """Everything the gate needs, reached from a run id."""

    run_id: str
    operation_key: str
    input_sha256: str
    docket: ArtifactRef
    document: Mapping[str, Any]
    items: tuple[ReversalSubjectV1, ...]
    version: str
    policy_revision: str
    allowed_actor: str

    @property
    def item_ids(self) -> tuple[str, ...]:
        return tuple(item.item_id for item in self.items)


@dataclass(frozen=True)
class PublishedReversals:
    """What a publish actually did, per verdict.

    Richer than `retirement_record.publish_admitted`'s list of hook ids, because
    here the three verdicts are three different outcomes worth reporting: `keep`
    is a decision that the questioned block stands, and reporting it as "nothing
    happened" alongside `defer` would erase the difference a human just drew.
    """

    withdrawn: tuple[str, ...]
    already_recorded: tuple[str, ...]
    kept: tuple[str, ...]
    deferred: tuple[str, ...]
    manifest_path: Path | None


def _digest(path: Path) -> str:
    """A file's SHA-256, or `""` when it is not there. Absence is an input."""

    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, ValueError):
        return ""


def _input_digests(
    root: Path, version: str, baseline: str, index_dir: Path | None
) -> dict[str, str]:
    """`{relative path: sha256}` for every file the sweep read.

    Per file rather than per directory, following `retirement_record`: a docket
    about 441 must not change its identity because an unrelated file arrived.

    **Every version in the series, NOT only those at or before `version`**, and
    that is where this deliberately differs from `retirement_record`. That module
    scopes to `<= version` because a retirement case is built from evidence up to
    the port it is about. This docket is built by `reconsider`, which reaches
    `roster`, which walks *every* version with evidence from the baseline — so a
    442 runtime probe changes whether a hook counts as never-having-run and
    therefore changes the findings. Scoping this to `<= version` made the
    operation key stand still while the docket moved: recording again refused,
    correctly but with a message about a digest mismatch rather than about the
    evidence, and the docstring's claim to key on "the inputs the docket derives
    from" was false as written.

    Unlike that module this does **not** refuse when there is no evidence at all.
    The endpoint-absence rule needs no runtime evidence, so a docket built purely
    from "these endpoints have vanished from the app" is a legitimate question;
    what makes a docket refusable is having nothing to ask, and `build_docket`
    decides that from the findings rather than from the inputs.
    """

    out: dict[str, str] = {}
    for relative in _DECISION_INPUTS:
        out[str(relative)] = _digest(root / relative)

    # The observation store `block_never_observed` reads. Only the reported
    # version's, because `never_observed` is asked about one version and reads
    # one file — unlike the evidence series, which `roster` walks whole.
    #
    # **Keyed even when absent**, like the app source and for the same reason:
    # "there is no observation evidence" and "every watched path was seen" are
    # opposite states that both produce no finding, and a key that did not move
    # when the first became the second would let a docket built before any device
    # session was taken be adopted by the run that took one. That is the exact
    # defect this function's docstring records having already had once, with the
    # evidence series playing the same part.
    out[str(OBSERVATIONS / f"{version}.jsonl")] = _digest(
        root / OBSERVATIONS / f"{version}.jsonl"
    )

    series = list(versions_with_evidence(root, baseline=baseline))
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
                out[str(path.relative_to(root))] = _digest(path)

    if index_dir is not None:
        index_dir = Path(index_dir)
        for name in _INDEX_INPUTS:
            out[f"index:{name}"] = _digest(index_dir / name)
        for name in _INDEX_PRESENCE_ONLY:
            out[f"index:{name}"] = "present" if (index_dir / name).is_file() else ""
    return out


def _grouped(found: Sequence[Reconsideration]) -> list[dict[str, Any]]:
    """One docket item per decision-to-withdraw, not one per rule that fired.

    `reconsider` reports per trigger, so a block can appear twice — once inert,
    once with its endpoint gone. Those are two pieces of evidence about one
    decision and there is one thing a human can do about them. Grouping here is
    what makes the gate's unit of question equal `reversal.withdrawn`'s unit of
    record; see `reversal_gate`'s module docstring for what asking per trigger
    would have cost.

    Order is the order the triggers first reported, which is `reconsider`'s own
    (sorted within each rule, rules in a fixed sequence). Stable, so the docket's
    digest does not depend on dictionary iteration.
    """

    order: list[tuple[str, str, str]] = []
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in found:
        key = (item.kind, item.original_decision_id, item.subject)
        if key not in grouped:
            order.append(key)
            grouped[key] = {
                "item_id": derive_item_id(item.kind, item.original_decision_id, item.subject),
                "kind": item.kind,
                "subject": item.subject,
                "original_decision_id": item.original_decision_id,
                "triggers": [],
                "summaries": [],
                "evidence": [],
            }
        entry = grouped[key]
        entry["triggers"].append(item.trigger)
        entry["summaries"].append(item.summary)
        # Every line, de-duplicated but not sorted: two rules firing on one
        # decision often repeat "ruled block on <date>", and a human reading the
        # same sentence twice learns to skim the list that is the whole case.
        for line in item.evidence:
            if line not in entry["evidence"]:
                entry["evidence"].append(line)
    return [grouped[key] for key in order]


def build_docket(
    root: Path | str,
    *,
    version: str,
    policy_revision: str,
    baseline: str = BASELINE_VERSION,
    index_dir: Path | str | None = None,
) -> tuple[dict[str, Any], tuple[ReversalSubjectV1, ...]]:
    """Every decision under reconsideration at `version`, as one signable document.

    Refuses two ways, and the second is the one worth reading. A docket with no
    items is a gate with no question. A docket with no items **and** a rule that
    could not run is worse than that: it is an empty answer to a question nobody
    finished asking, and returning it would let "no index supplied" reach a human
    as "nothing is wrong".
    """

    root = Path(root)
    if not _NUMERIC.fullmatch(version):
        raise RecordError(f"{version!r} is not a version number")
    try:
        found, not_run = reconsiderations(
            root, version=version, baseline=baseline, index_dir=index_dir
        )
    except (ReconsiderError, ReversalError, ValueError, OSError) as error:
        raise RecordError(str(error)) from error

    if not found:
        if not_run:
            raise RecordError(
                "nothing was found to reconsider, but "
                f"{len(not_run)} rule(s) did not run: {'; '.join(not_run)}. That is an "
                "incomplete sweep, not a clean one — raising a gate on it would put a "
                "silence in front of a human where a question belongs"
            )
        raise RecordError(
            f"every recorded decision still matches the evidence at {version}. A gate "
            "with no question does not need a human"
        )

    items = _grouped(found)
    document = {
        "schema_version": 1,
        "version": version,
        "policy_revision": policy_revision,
        # In the signed bytes on purpose. See the module docstring: a human must
        # be able to see, in what they sign, which rules could not be run.
        "rules_not_run": list(not_run),
        "items": items,
    }
    try:
        subjects = docket_subjects(document)
    except ReversalGateError as error:
        raise RecordError(str(error)) from error
    return document, subjects


def operation_input(
    run_id: str,
    version: str,
    baseline: str,
    inputs: Mapping[str, str],
    policy_revision: str,
) -> dict[str, Any]:
    """What determines the docket. Everything, and nothing derived from it.

    `baseline` is in here because `roster` walks the evidence series from it, so
    two sweeps with the same files and different baselines are two different
    questions — and a key that did not move would let the second adopt the first's
    answer.
    """

    return {
        "run_id": run_id,
        "version": version,
        "baseline": baseline,
        "inputs": dict(sorted(inputs.items())),
        "policy_revision": policy_revision,
    }


def record(
    state_root: Path | str,
    *,
    run_id: str,
    version: str,
    allowed_actor: str,
    owner_token: str,
    root: Path | str = ".",
    baseline: str = BASELINE_VERSION,
    index_dir: Path | str | None = None,
    expect_docket_sha256: str | None = None,
) -> RecordedReversalDocket:
    """Build the docket, put it in CAS, and file the run-keyed authority row.

    Deliberately takes **no** manifest path. `reconsiderations` reads
    `<root>/manifest/hooks.json` unconditionally, so a `--manifest` pointing
    elsewhere would have taken the policy revision from one file and the docket
    from another — half-scoped, which this project has twice found to be worse
    than unscoped because it looks right.
    """

    try:
        return _record(
            state_root,
            run_id=run_id,
            version=version,
            allowed_actor=allowed_actor,
            owner_token=owner_token,
            root=root,
            baseline=baseline,
            index_dir=index_dir,
            expect_docket_sha256=expect_docket_sha256,
        )
    # One error type out of this module, matching `retirement_record.record`. A
    # caller that had to catch four unrelated exception types would catch
    # `Exception`, and then a genuine bug in the ledger would read as a refusal.
    except (ReconsiderError, ReversalError, ReversalGateError) as error:
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
    allowed_actor: str,
    owner_token: str,
    root: Path | str,
    baseline: str,
    index_dir: Path | str | None,
    expect_docket_sha256: str | None,
) -> RecordedReversalDocket:
    root = Path(root)
    # The one `reconsiderations` reads, so the policy revision and the docket
    # cannot come from different files.
    manifest = root / "manifest" / "hooks.json"
    state_root = Path(state_root)

    # Validated BEFORE anything durable is written, as `retirement_record` does.
    # `derived_gate_id` refuses a run id that would not make a valid identifier,
    # and it does so at gate-derivation time — one module and one durable write
    # too late. A row filed under `"reconsider 441!"` resolves happily and then
    # cannot be turned into a gate at all: answerable in a test, unanswerable in
    # production.
    for value, label in ((run_id, "run id"), (allowed_actor, "allowed actor")):
        if not ID_PATTERN.fullmatch(value):
            raise RecordError(
                f"{label} {value!r} is not a valid identifier, so no gate could ever "
                "be raised for it"
            )
    # Refused HERE rather than at publish time, which is where `Reversal` would
    # refuse it. A docket recorded as answerable only by `agent` raises a gate a
    # human can wait a week on and whose answer can then never be written — the
    # "answerable in a test, unanswerable in production" trap, arriving a week
    # late. Only a human withdraws a decision; an agent may assemble the case.
    if allowed_actor.strip().lower() == "agent":
        raise RecordError(
            "allowed actor is 'agent'. A human withdraws a decision; an agent may "
            "assemble the case for withdrawing it, and `reversal.Reversal` would "
            "refuse the row this gate's answer becomes"
        )

    revision = read_policy_revision(manifest)
    document, items = build_docket(
        root,
        version=version,
        policy_revision=revision,
        baseline=baseline,
        index_dir=index_dir,
    )
    body = canonical_json(document).encode("utf-8")
    docket_sha256 = hashlib.sha256(body).hexdigest()
    if expect_docket_sha256 is not None and expect_docket_sha256 != docket_sha256:
        raise RecordError(
            f"docket is {docket_sha256}, not the expected {expect_docket_sha256}. This "
            "module recomputes rather than adopting; a disagreement means the evidence "
            "or the recorded decisions moved"
        )

    payload = operation_input(
        run_id,
        version,
        baseline,
        _input_digests(root, version, baseline, Path(index_dir) if index_dir else None),
        revision,
    )
    key = canonical_sha256({"kind": DOCKET_OPERATION_KIND, "input": payload})
    input_sha256 = canonical_sha256(payload)

    ledger = Ledger(state_root / "ledger.sqlite3")
    store = ContentStore(state_root / "cas")

    # Conflict checked BEFORE any write, as `retirement_record` does. A refused
    # record that had already put a blob in CAS leaves an orphan nothing points
    # at, and the store has no sweeper.
    try:
        existing_row = ledger.recorded_reversal_docket_for_run(run_id)
    except ValueError:
        existing_row = None
    if existing_row is not None and existing_row.get("operation_key") != key:
        raise RecordError(
            f"a different reversal docket is already recorded for {run_id}. Two "
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

    ledger.record_reversal_docket_authority(
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
    return RecordedReversalDocket(
        run_id=run_id,
        operation_key=key,
        input_sha256=input_sha256,
        docket=reference,
        document=document,
        items=items,
        version=version,
        policy_revision=revision,
        allowed_actor=allowed_actor,
    )


def resolve_with(
    ledger: Ledger, store: ContentStore, run_id: str
) -> RecordedReversalDocket:
    """Re-derive the docket for a run, from recorded state alone.

    Goes through `require_completed_operation` rather than reading the blob by the
    row's digest: the row carries **coordinates**, so the operation's own checks
    are not bypassed for a caller who happens to hold a run id.
    """

    row = ledger.recorded_reversal_docket_for_run(run_id)
    reference = ledger.require_completed_operation(
        row["operation_key"], DOCKET_OPERATION_KIND, row["input_sha256"]
    )
    body = store.read_blob(reference.sha256, reference.size)
    document = json.loads(body.decode("utf-8"))
    if canonical_json(document).encode("utf-8") != body:
        raise RecordError("the recorded docket is not in canonical form")
    if document.get("version") != row["version"]:
        raise RecordError("the recorded docket names a different version than its row")
    try:
        items = docket_subjects(document)
    except ReversalGateError as error:
        raise RecordError(str(error)) from error
    return RecordedReversalDocket(
        run_id=run_id,
        operation_key=row["operation_key"],
        input_sha256=row["input_sha256"],
        docket=reference,
        document=document,
        items=items,
        version=str(row["version"]),
        policy_revision=str(row["policy_revision"]),
        allowed_actor=str(row["allowed_actor"]),
    )


def admitted_rulings(
    ledger: Ledger, store: ContentStore, run_id: str
) -> tuple[ReversalRulingsV1, str]:
    """The rulings a human actually made, and the decision that carries them.

    Reads the run-keyed row the admitting Activity wrote, fetches the document by
    its digest and size, and cross-checks the docket it names against the row. The
    cross-check is not ceremony: the row and the blob are two records of one fact,
    and a reader that trusted either alone would not notice them diverging.
    """

    row = ledger.admitted_reversal_rulings_for_run(run_id)
    body = store.read_blob(str(row["rulings_sha256"]), int(row["rulings_size"]))
    document = ReversalRulingsV1.from_dict(json.loads(body.decode("utf-8")))
    if document.docket_sha256 != row["docket_sha256"]:
        raise RecordError("the admitted rulings name a different docket than their row")
    return document, str(row["decision_id"])


def _timestamp(value: str) -> str:
    """An ISO 8601 stamp with an offset, or a refusal.

    **Parsed, not merely non-blank.** `Reversal.__post_init__` requires every
    other field to be non-empty and does not check `recorded_at` at all, so
    `--recorded-at banana` went straight into `manifest/reversals.jsonl` and the
    command exited 0. The whole reason this layer takes the stamp as a parameter
    rather than reading a clock is *"so a re-run rewrites the line already on
    disk rather than stamping a new time no reader can order"* — and an
    unparseable stamp defeats exactly that, permanently, in a file whose contract
    is that nothing is ever deleted from it.

    The offset is required for the reason `GateDecision`'s is: a naive timestamp
    cannot be ordered against one from another machine.

    **Returns the stripped value, and the caller must use it.** Validating
    `value.strip()` and then writing `value` accepted
    `"  2026-08-09T10:00:00+00:00  "` and put the padding into the permanent
    record — in a form this function itself refuses on the next read. `Z` is
    accepted but not rewritten: what a human typed is what gets recorded, and
    `datetime.fromisoformat` reads both spellings back.
    """

    from datetime import datetime  # noqa: PLC0415  (only this one function needs it)

    if not isinstance(value, str) or not value.strip():
        raise RecordError("publishing needs a timestamp; this layer must not read the clock")
    stamp = value.strip()
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError as error:
        raise RecordError(
            f"{value!r} is not an ISO 8601 timestamp ({error}). It would be written "
            "into manifest/reversals.jsonl, which nothing ever deletes from"
        ) from error
    if parsed.tzinfo is None:
        raise RecordError(
            f"{value!r} has no UTC offset. A naive stamp cannot be ordered against "
            "one written on another machine"
        )
    return stamp


def _declared(manifest_path: Path, endpoint: str) -> bool:
    """Whether any hook still declares this endpoint, by the app's spelling rule.

    `assessment.normalise`, the same function `plan_unblock` matches with, so a
    decision recorded as `feed/timeline_stream/` sees the manifest's
    `/feed/timeline_stream/`. Two spellings of one rule is how an entire grouping
    went invisible on 440.
    """

    from .assessment import normalise  # noqa: PLC0415

    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    hooks = document.get("hooks")
    if not isinstance(hooks, list):
        raise RecordError(f"{manifest_path} has no 'hooks' array")
    target = normalise(endpoint)
    return any(
        normalise(dep) == target
        for entry in hooks
        if isinstance(entry, dict)
        for dep in entry.get("semantic_deps") or ()
    )


def publish_admitted(
    state_root: Path | str,
    run_id: str,
    *,
    recorded_at: str,
    confirm: bool,
    root: Path | str = ".",
    manifest_path: Path | str | None = None,
    reversals_path: Path | str | None = None,
) -> PublishedReversals:
    """Turn admitted rulings into recorded reversals. The consumer.

    **This function is the reason the gate is worth having.** Four times this
    project has shipped a gate that was complete and disconnected at one end —
    `the-gates-rulings-have-no-consumer`, `nothing-computes-a-stage-4a-assessment`,
    `the-post-build-gate-cannot-be-satisfied`, `the-feature-gate-has-no-producer` —
    and a withdrawal that was approved, admitted and never written would be the
    fifth.

    Each `withdraw` becomes a `reversal.Reversal`; `keep` and `defer` write
    nothing, because both leave the questioned decision in force. A block
    withdrawal additionally goes through `plan_unblock`/`apply_unblock`, which
    records the reversal **and then** rewrites `manifest/hooks.json` — in that
    order, so a failed manifest write leaves a human's decision on disk rather
    than an unblocked endpoint nobody decided to unblock.

    `confirm` is demanded even though a human already signed at the gate, matching
    `reversal.py`'s own CLI. The gate ruling authorises *the decision*; this flag
    authorises *this machine rewriting this file now*, and they are different
    permissions granted at different times.

    `recorded_at` is supplied, never read from the clock here, for the reason
    `rulings.py --recorded-at` is: a re-run must rewrite the line already on disk
    rather than stamping a new time no reader can order.
    """

    state_root = Path(state_root)
    root = Path(root)
    manifest = Path(manifest_path) if manifest_path else root / "manifest" / "hooks.json"
    ledger = Ledger(state_root / "ledger.sqlite3", read_only=True)
    store = ContentStore(state_root / "cas")
    # Reassigned, not merely checked. A validated-then-discarded return is how the
    # padding got into the record.
    recorded_at = _timestamp(recorded_at)
    if not confirm:
        raise RecordError(
            "withdrawing a decision changes what the app enforces and what a port is "
            "expected to carry; pass confirm"
        )

    document, decision_id = admitted_rulings(ledger, store, run_id)
    recorded = resolve_with(ledger, store, run_id)
    # Three records of one fact, checked equal before any of them is trusted:
    # `admitted_rulings` has already compared the rulings document against its own
    # row, and this compares both against the docket the operation resolves to.
    # Without it a mismatch would surface as a `KeyError` on the lookup below —
    # a traceback past every handler, where the honest answer is a refusal.
    if document.docket_sha256 != recorded.docket.sha256:
        raise RecordError(
            "the admitted rulings answer a different docket than the one recorded "
            f"for {run_id}; nothing here can say which one the human read"
        )
    items = {entry["item_id"]: entry for entry in recorded.document["items"]}
    unanswered = sorted(set(recorded.item_ids) - {r.item_id for r in document.rulings})
    if unanswered:
        # `validate_submission` proved this at admission time, so reaching it means
        # the ledger disagrees with itself. Refused rather than skipped: a decision
        # nobody answered must not be read as one left in force by choice.
        raise RecordError(
            f"the admitted rulings do not answer {', '.join(unanswered)}, which the "
            "recorded docket asks about"
        )

    if not _NUMERIC.fullmatch(recorded.version):
        raise RecordError(f"the recorded docket names {recorded.version!r}, not a version")
    # Derived, exactly as a retirement's is: the version AFTER the port the
    # docket was built from, so restoring a hook cannot reach back into a port
    # that has already been assessed.
    effective_from = str(int(recorded.version) + 1)

    withdrew: list[str] = []
    already: list[str] = []
    kept: list[str] = []
    deferred: list[str] = []
    touched_manifest = False

    by_id = {ruling.item_id: ruling for ruling in document.rulings}
    for item_id_ in recorded.item_ids:
        ruling = by_id[item_id_]
        entry = items[item_id_]
        subject = str(entry["subject"])
        kind = str(entry["kind"])
        original = str(entry["original_decision_id"])
        recorded_already = (original, subject) in withdrawn(
            kind, root, path=reversals_path
        )

        if ruling.verdict != WITHDRAWING_VERDICT:
            if recorded_already:
                # Checked on THIS branch too, and it was not at first. A human
                # ruling `keep` on a decision already withdrawn on the record is
                # saying the block stands while the record says it does not, and
                # the report said "kept in force" over a manifest that no longer
                # declared the endpoint. Reporting a contradiction as an outcome
                # is the failure this gate exists to prevent, arriving from the
                # branch nobody guarded.
                raise RecordError(
                    f"{subject} was ruled {ruling.verdict!r}, but its withdrawal from "
                    f"{original} is already on record. The answer and the record "
                    "contradict each other; resolve it by hand before publishing"
                )
            (kept if ruling.verdict == "keep" else deferred).append(subject)
            continue

        reversal = Reversal(
            schema_version=1,
            withdraws=kind,
            subject=subject,
            original_decision_id=original,
            # The GATE's decision id, not one minted here. `reversal.reversal_id`
            # is what a human typing the command at a terminal gets; a withdrawal
            # that came through a gate must point back at the decision they
            # signed, exactly as `retirement_record.publish_admitted` does.
            decision_id=decision_id,
            # The actor is not in the rulings document — it is in the decision the
            # ledger recorded — so this comes from the docket's allowed actor,
            # which `validate_submission` has already proved the decision's actor
            # equals. Two names for one fact, checked equal before either was
            # trusted. `Reversal` then refuses `agent` on its own account.
            ruled_by=recorded.allowed_actor,
            rationale=ruling.rationale,
            recorded_at=recorded_at,
            effective_from="" if kind == "block" else effective_from,
        )

        if recorded_already:
            if kind == "retirement" or not _declared(manifest, subject):
                # A completed publish, re-run. Skipped rather than refused, so
                # that a partial failure can be finished by running the command
                # again — which is the only reason to look this state up at all.
                already.append(subject)
                continue
            raise RecordError(
                f"{subject}'s withdrawal is recorded but {manifest} still declares it. "
                "The record and the manifest disagree, and this cannot tell which one a "
                "human meant; resolve it by hand before publishing again"
            )

        try:
            if kind == "block":
                plan = plan_unblock(reversal, manifest_path=manifest)
                apply_unblock(
                    plan, confirm=True, root=root, reversals_path=reversals_path
                )
                touched_manifest = True
            else:
                append_reversal(reversal, root=root, path=reversals_path)
        except (ReversalError, OSError) as error:
            raise RecordError(f"{subject}: {error}") from error
        withdrew.append(subject)

    return PublishedReversals(
        withdrawn=tuple(withdrew),
        already_recorded=tuple(already),
        kept=tuple(kept),
        deferred=tuple(deferred),
        manifest_path=manifest if touched_manifest else None,
    )


def raise_gate(
    endpoint: str,
    task_queue: str,
    run_id: str,
    *,
    gate_timeout_seconds: int,
    build_id: str = "",
    deployment_name: str = "dfinsta-pipeline",
    wait_for_worker_seconds: float = 30.0,
) -> str:
    """Start the Workflow that asks a human, and return its id. The starter.

    A gate with no starter is raisable by hand and by nothing else, which is the
    disconnection at the far end from the one `publish_admitted` closes.
    `FeatureAssessmentRunWorkflow` shipped in exactly that state and
    `retirement_record.raise_gate` is the first one written on purpose; this is
    the second.

    **`build_id` is not optional in practice, and finding that out took a real
    server.** Every workflow here is `versioning_behavior=PINNED`, which a worker
    may only run with `use_worker_versioning=True` — and a versioned worker is
    dispatched tasks only for its deployment's *current* version, which nothing in
    this project sets. Started without an override, the Workflow is accepted by
    the server, appears in the UI, and is never picked up by any worker; every
    query then times out with no error that names the cause.

    So pass the same `--build-id` the worker was started with. Omit it only if an
    operator has set a current version out of band
    (`temporal worker deployment set-current-version`).

    Imported lazily: `temporalio` is the only runtime dependency and everything
    else in this module must work with no server anywhere near it.

    **Every failure leaves as `RecordError`**, so `main` prints `refused: …` and
    exits 2. `retirement_record.raise_gate` raises a bare `RuntimeError` for the
    no-worker case and its CLI does not catch it — the documented failure mode
    that starter spends three paragraphs on lands as a traceback and exit 1.
    Matching a defect is not a virtue; this one converts.
    """

    import asyncio
    import time

    from temporalio.client import Client
    from temporalio.common import PinnedVersioningOverride, WorkerDeploymentVersion
    from temporalio.service import RPCError

    from .reversal_gate import ReversalRunRequestV1
    from .reversal_workflow import ReversalRunWorkflow

    override = None
    if build_id:
        override = PinnedVersioningOverride(
            WorkerDeploymentVersion(deployment_name, build_id)
        )

    async def _start() -> str:
        client = await Client.connect(endpoint)
        # Retried, because a pinned start is refused until a worker for that exact
        # deployment version has polled the queue: *"Pinned version
        # 'dfinsta-pipeline:<build>' is not present in task queue … of type
        # 'Workflow'"*. Raising a gate right after starting a worker is the normal
        # order of operations, so a bare failure here is a race a human loses
        # roughly every time.
        deadline = time.monotonic() + wait_for_worker_seconds
        while True:
            try:
                handle = await client.start_workflow(
                    ReversalRunWorkflow.run,
                    ReversalRunRequestV1(1, run_id, gate_timeout_seconds),
                    # The run id IS the workflow id, as every gate here does it:
                    # that is what lets `submission show <workflow_id>` reach the
                    # right gate from the only identifier a human has.
                    id=run_id,
                    task_queue=task_queue,
                    versioning_override=override,
                )
            except RPCError as error:
                if "not present in task queue" not in str(error):
                    raise
                if time.monotonic() >= deadline:
                    raise RecordError(
                        f"no worker for {deployment_name}:{build_id} has polled "
                        f"{task_queue!r} within {wait_for_worker_seconds}s, so a "
                        "pinned start cannot be accepted. Start the worker with that "
                        "--build-id first, or set a current deployment version and "
                        "omit --build-id"
                    ) from error
                await asyncio.sleep(0.5)
                continue
            return handle.id

    try:
        return asyncio.run(_start())
    except RecordError:
        raise
    # An unreachable endpoint is the most ordinary way this is used wrongly — a
    # typo in `--endpoint`, or a server that is not up yet. Measured, not guessed:
    # `Client.connect` reports a refused connection as a bare **`RuntimeError`**
    # out of the Rust bridge, not as `RPCError` or `OSError`, and the first
    # version of this handler caught only the latter two and let the real case
    # through as a traceback and exit 1. `RuntimeError` is safe to catch here
    # because this function touches no ledger — it is the read-only ledger guard
    # that makes a bare `RuntimeError` mean "bug" elsewhere in this package.
    except (RPCError, OSError, RuntimeError) as error:
        raise RecordError(
            f"could not reach a Temporal server at {endpoint}: {error}"
        ) from error


def resolve(
    state_root: Path | str, run_id: str, *, read_only: bool = True
) -> RecordedReversalDocket:
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
    recording.add_argument("--allowed-actor", required=True)
    recording.add_argument("--owner-token", required=True)
    recording.add_argument(
        "--index",
        type=Path,
        help="a decode index for this version. Without it the `block_endpoint_absent` "
        "rule is skipped, and the skip travels inside the docket a human signs",
    )
    recording.add_argument("--expect-docket-sha256")

    showing = sub.add_parser("show")
    showing.add_argument("--run-id", required=True)
    showing.add_argument("--document", action="store_true")

    raising = sub.add_parser("raise", help="start the Workflow that asks a human")
    raising.add_argument("--run-id", required=True)
    raising.add_argument("--endpoint", default="localhost:7233")
    raising.add_argument("--task-queue", default="dfinsta")
    raising.add_argument(
        "--build-id",
        default="",
        help="the --build-id the worker was started with. Without it the Workflow "
        "is started, never dispatched, and every query times out — every workflow "
        "here is PINNED, and a versioned worker only receives tasks for its "
        "deployment's current version. Omit only if that has been set out of band",
    )
    raising.add_argument("--deployment-name", default="dfinsta-pipeline")
    raising.add_argument(
        "--gate-timeout-seconds",
        type=int,
        default=7 * 24 * 3600,
        help="how long the gate stays open. A week by default; an unanswered one "
        "leaves every questioned decision in force",
    )

    publishing = sub.add_parser(
        "publish", help="record admitted withdrawals and unblock the manifest"
    )
    publishing.add_argument("--run-id", required=True)
    publishing.add_argument("--manifest", type=Path)
    publishing.add_argument("--reversals", type=Path)
    publishing.add_argument(
        "--recorded-at",
        required=True,
        help="ISO 8601 stamp for the recorded reversals. Supplied, never read from "
        "the clock here, so a re-run rewrites the line already on disk",
    )
    publishing.add_argument("--confirm", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "raise":
            workflow_id = raise_gate(
                args.endpoint,
                args.task_queue,
                args.run_id,
                gate_timeout_seconds=args.gate_timeout_seconds,
                build_id=args.build_id,
                deployment_name=args.deployment_name,
            )
            print(f"raised {workflow_id}")
            print(
                "Answer it with:  python -m dfinsta_pipeline.submission "
                f"--state-root {args.state_root} show {workflow_id}"
            )
            return 0

        if args.command == "publish":
            published = publish_admitted(
                args.state_root,
                args.run_id,
                recorded_at=args.recorded_at,
                confirm=args.confirm,
                root=args.root,
                manifest_path=args.manifest,
                reversals_path=args.reversals,
            )
            if published.withdrawn:
                print(f"withdrew: {', '.join(published.withdrawn)}")
            if published.already_recorded:
                print(f"already withdrawn: {', '.join(published.already_recorded)}")
            if published.kept:
                print(f"kept in force: {', '.join(published.kept)}")
            if published.deferred:
                print(f"deferred: {', '.join(published.deferred)}")
            if published.manifest_path:
                print(f"manifest written: {published.manifest_path}")
            if not published.withdrawn and not published.already_recorded:
                print(
                    f"{args.run_id}: nothing withdrawn. Every questioned decision "
                    "stands."
                )
            else:
                print(
                    "Commit the files: the expectation and the manifest read the "
                    "committed ones, and an uncommitted row works here and vanishes "
                    "on clone."
                )
            return 0

        if args.command == "record":
            recorded = record(
                args.state_root,
                run_id=args.run_id,
                version=args.version,
                allowed_actor=args.allowed_actor,
                owner_token=args.owner_token,
                root=args.root,
                baseline=args.baseline,
                index_dir=args.index,
                expect_docket_sha256=args.expect_docket_sha256,
            )
        else:
            recorded = resolve(args.state_root, args.run_id)
            if args.document:
                print(json.dumps(recorded.document, indent=2, sort_keys=True))
                return 0
    # `ValueError` alongside the rest: `Ledger.recorded_reversal_docket_for_run`
    # raises a plain one for a run that was never recorded, which is the most
    # ordinary way this command is used wrongly — a typo in a run id — and it is
    # the one that would otherwise leave as a traceback and exit 1 instead of
    # `refused:` and exit 2.
    except (RecordError, ReversalError, ReversalGateError, ValueError, OSError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2

    print(f"run              {recorded.run_id}")
    print(f"version          {recorded.version}")
    print(f"operation key    {recorded.operation_key}")
    print(f"docket           {recorded.docket.sha256}")
    print(f"decisions        {', '.join(recorded.item_ids)}")
    for entry in recorded.document["items"]:
        print(f"  {entry['kind']}: {entry['subject']}  [{', '.join(entry['triggers'])}]")
    for line in recorded.document.get("rules_not_run", ()):
        print(f"  RULE NOT RUN     {line}")
    print(f"policy revision  {recorded.policy_revision}")
    print(f"may answer       {recorded.allowed_actor}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# Replay Workflow Registration — Design

Status: draft for independent review. No `workflow.py` or `worker.py` change has been made.
Author pass: 2026-07-31, at `b6feddf`.

This document decides how the five proven replay checkpoint Activities enter the durable
Temporal Workflow. It exists because `HANDOVER.md` section 6 requires a reviewed design and a
failing-test slice *before* any registration edit, and because the constraints below were only
discovered by measurement, not by reading the inherited documents.

## 1. Constraints established by measurement

Each of these was verified against current code at `b6feddf`, not inherited from prose.

**C1. Temporal History has a hard 256 KB budget, and the naive design breaks it for 340.**
`tests/test_phase_a_temporal.py:302` asserts `len(history_json) < 256 * 1024`, and
`docs/ADK_PIPELINE_PLAN.md:247` lists "No large bytes, secrets, private paths, or credentials
appear in Temporal History" as a Phase A acceptance criterion.

`AdmittedReplayV3` embeds `intent`, `resolution`, and `source_manifest`. Their compact-JSON
floor is 102,066 bytes for target 340 and 15,010 bytes for target 430. Passing that object to
five Activities writes at least 510,330 bytes for 340 — **1.9x over budget** — while 430 fits
comfortably at 75,050 bytes. A design validated only on 430 would pass and then fail on 340.

**C2. The ledger is the authority; the passed candidate is only a cross-check.**
`Ledger.require_admitted_replay_v3` (`src/dfinsta_pipeline/ledger.py:483`) selects the row
`WHERE run_id = ?` (line 495-496), rebuilds the object from the stored `admitted_json`
(line 502), rejects a non-canonical stored copy (line 503), requires
`reconstructed == candidate` (line 511), and **returns `reconstructed`** (line 514). The
Activity therefore executes against the ledger's copy, never the caller's.
`require_admitted_replay_verification_grant_v1` (line 667) follows the same shape, keyed by
`grant_id` (line 683).

This is what makes C1 solvable. The full object is not the source of authority, so passing a
reference does not weaken admission — provided the reference still pins the exact expected
content by hash.

**C3. No replay Activity heartbeats.** The only `activity.heartbeat` call in
`src/dfinsta_pipeline/activities.py` is at line 3226, inside Phase A's `apply_activity`.
Real stage durations from the recorded evidence roots are 932 s (340 build), 1,457 s (430
build), 1,619 s (340 final verify), and 2,448 s (430 final verify). Without heartbeats, worker
loss is undetectable until `start_to_close_timeout` expires.

**C4. Stage-set membership is derivable from data, not from a target literal.**
340 runs `(decode, apply, build, verify)`; 430 runs `(framework, decode, apply, build, verify)`
(`tests/test_phase_b_real_replay_harness.py:166-175`). The discriminator is
`ToolchainProfileV3.frameworks`: empty for 340, non-empty for 430
(`tests/test_phase_b_real_replay_harness.py:84-87`). So `if admitted.profile.frameworks:` is a
data branch, not the `340/430` conditional forbidden by `docs/ADK_PIPELINE_PLAN.md:254`.

**C5. The second gate cannot be raised up front.** `AdmittedReplayVerificationGrantV1`
(`src/dfinsta_pipeline/replay_contracts.py:2821`) embeds a `ReplayPatchedApkReceiptV1`, which
does not exist until the build Activity completes. Final verification therefore requires a gate
decision raised *mid-run*, after build, over a subject that the Workflow only learns at that
point.

## 2. Decisions

### D1. A new Workflow class, not an extension of `PortRunWorkflow`

`PortRunWorkflow` stays byte-identical. The replay chain becomes a separate
`@workflow.defn` class in `workflow.py`.

Rationale: `tests/test_phase_a_temporal.py:306-309` replays a saved Phase A History against
`PortRunWorkflow` and separately requires `IncompatiblePortRunWorkflow` to raise
`NondeterminismError`. Any new command inserted into `PortRunWorkflow.run` — even behind a
condition — risks changing the command sequence for already-saved Histories. A separate defn
makes old-History compatibility trivially true rather than argued, which matches the project's
stated preference for fail-closed over convenient.

Cost: the two Workflows do not share a single run identity. That is acceptable now; Phase A
approval and replay execution are already separate authorities with separate gate decisions.

### D2. Pass a reference, not the object

Activities keep their names and receipt schemas. The Workflow passes a small
`ReplayStageRequestV1` carrying `schema_version`, `run_id`, and `admitted_replay_sha256`; the
Activity loads authority from the ledger by `run_id` and requires the recomputed canonical hash
to equal `admitted_replay_sha256`.

This preserves exactly the property C2 shows the current equality check provides — the
Workflow's view and the ledger's view are proven identical — while replacing a 102 KB payload
with roughly 150 bytes. History for a 340 run drops from >510 KB to well under budget.

Per lesson `lsn_d4ad159503f0c0bb`, this is a **new** schema, not a new required field on an
existing one. `AdmittedReplayV3` and `AdmittedReplayVerificationGrantV1` wire identities are
untouched.

**Open question for review:** the existing Activities take `candidate: AdmittedReplayV3`, and
four tests assert that signature. Introducing a reference argument changes the signature.
Two options, and I do not think the choice is obvious:

- **D2a**: change the five signatures to accept `ReplayStageRequestV1`. Activity *names* and
  receipt hashes are unchanged, so Temporal identity and all recorded evidence remain valid,
  but the direct-Activity harness and four signature assertions must be updated.
- **D2b**: leave the five Activities untouched and add five thin Workflow-facing Activities
  that resolve the reference and delegate. Nothing existing changes, at the cost of doubling
  the registered surface and creating a second call path — which the project has previously
  rejected in a different form (`lsn_e4b0b9e054c3d8ed`, standalone CLI).

I lean D2a: "do not change Activity identity" most plausibly means the registered name and the
receipt contract, both of which D2a preserves, and D2b's second execution path is the exact
smell the CLI rejection warned about. This needs an explicit reviewer ruling before code.

### D3. Both gates as validated Updates, the second raised after build

The Workflow raises gate one before the first stage and gate two after the build Activity
returns, each via `@workflow.update` with a validator mirroring `validate_submit_decision`
(`workflow.py:105-142`): actor authorization, exact hash binding to the pending gate, timestamp
window, and single-submission. Gate two additionally binds the completed build receipt that
gate one could not name (C5).

### D4. Timeouts sized from recorded evidence, no heartbeat timeout

Given C3, `heartbeat_timeout` stays unset. `start_to_close_timeout` is sized per role from the
recorded durations with generous margin: framework 15 min; decode, apply, and build 60 min;
verify 90 min. `retry_policy` is `maximum_attempts=2` so a retry exercises the proven adoption
path rather than relaunching, and `cancellation_type` is `WAIT_CANCELLATION_COMPLETED` so
quarantine completes before the Workflow observes cancellation.

Adding heartbeats to the replay Activities would be a real improvement, but it changes proven
Activity internals and belongs in a separate reviewed slice.

## 3. Failing-test slice to write first

1. Stage sequence per target, derived from `profile.frameworks` and never from a target literal.
2. A saved Phase A History still replays against an unchanged `PortRunWorkflow`, and the
   incompatible definition still raises `NondeterminismError`.
3. A replay-Workflow History stays within the 256 KB budget **for target 340**, which is the
   case that fails under the naive design.
4. Gate two is rejected before build completes and accepted only when it binds the completed
   build receipt.
5. Registration tripwires flip deliberately: the five exclusion tests become inclusion tests in
   the same commit that registers, never silently.

## 4. NO-GO conditions

Do not proceed if any holds: the 340 History test cannot be made to pass without raising the
256 KB budget; reference-passing is shown to weaken admission in a case C2 does not cover;
old Phase A History replay breaks; or the reviewer rules that changing the five signatures
violates the Activity-identity constraint and D2b's second call path is also unacceptable —
in which case the payload problem needs a different answer before any registration.

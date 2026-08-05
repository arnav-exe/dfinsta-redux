# Replay Workflow Registration — Design

Status: draft for independent review. No `workflow.py` or `worker.py` change has been made.
Author pass: 2026-07-31, at `b6feddf`.

This document decides how the five proven replay checkpoint Activities enter the durable
Temporal Workflow. It exists because `HANDOVER.md` section 6 requires a reviewed design and a
failing-test slice *before* any registration edit, and because the constraints below were only
discovered by measurement, not by reading the inherited documents.

## 1. Constraints established by measurement

Each of these was verified against current code at `b6feddf`, not inherited from prose.

**C1. The payload is large and target-asymmetric, but the 256 KB budget is not a hard limit here.**

`AdmittedReplayV3` embeds `intent`, `resolution`, and `source_manifest`. Their compact-JSON
floor is 102,066 bytes for target 340 (independently reproduced at 102,078 with `canonical_json`)
and 15,010 bytes for target 430. Passing that object to five Activities writes at least
510,330 bytes for 340, and the grant embeds a sixth copy. 430 costs 75,050 bytes. A design
validated only on 430 would look fine and 340 would be 7x heavier.

**Scope correction, recorded because an earlier draft overstated it.**
`tests/test_phase_a_temporal.py:302` does assert `len(history_json) < 256 * 1024`, but that
assertion lives in `test_history_is_compact_private_and_replay_safe`, which starts
`PortRunWorkflow.run` (line 290) and replays `[PortRunWorkflow]` (line 307). Under decision D1
the replay chain is a *separate* Workflow, so it cannot fail that test. Nor does ~500 KB breach
any Temporal limit: the SDK warns per payload at 512 KB
(`.venv/.../temporalio/converter/_payload_limits.py:17`) and each payload here is ~102 KB, well
under the 2 MB payload error and 50 MB History ceiling.

The case for shrinking the payload therefore rests on three real grounds, not on a failing test:

- `docs/ADK_PIPELINE_PLAN.md:247` requires that no "large bytes, secrets, **private paths**, or
  credentials appear in Temporal History". `SourceManifestV1.records` is a full inventory of
  source file paths and digests, so this is a direct hit regardless of size.
- History is re-deserialized on every workflow task replay, forever.
- The 256 KB budget is a guarantee worth *extending* to the new Workflow deliberately (test 3
  in section 3), not one to quietly leave behind.

Note what `docs/ADK_PIPELINE_PLAN.md:186` does **not** cover: it enumerates "APKs, decode trees,
indexes, screenshots, and full reports". A `ResolutionSpecV3` is none of those. Do not cite it here.

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

**Resolved in favour of wrappers (D2b).** An earlier draft leaned the other way; two independent
reviews moved it.

- **D2a**, rejected: change the five signatures to accept the reference. Preserves Activity
  names and receipt hashes, but the five Activities' real-run evidence is bound to specific
  commits (`d8d0187`, `5388d80`, `a07c8d4`, `609bacf`). Editing them invalidates that evidence
  and forces a fresh 340+430 real run — roughly 68 minutes of compute for build and verify
  alone — plus independent re-review, before anything can be claimed again.
- **D2b**, chosen: leave the five proven Activities byte-identical. Add thin registered
  wrappers that take the handle, load exact authority from the ledger, and delegate.

The "second execution path" objection that motivated the D2a lean does not survive inspection:
the wrapper only *loads*, and the inner function still runs its own
`Ledger.require_admitted_replay_v3` against the loaded object, so authority is validated twice,
not bypassed. That is materially different from the rejected standalone CLI
(`lsn_e4b0b9e054c3d8ed`), which self-asserted capability hashes and issued its own receipts.

**The handle must carry the hash, not just the id.** A bare `run_id` argument would be strictly
weaker than today: `ledger.py:508-512` cross-checks `candidate_values` and `reconstructed !=
candidate`, which proves the caller's view equals the ledger's. Passing
`(run_id, admitted_replay_sha256)` and requiring `reconstructed.sha256 == handle.admitted_replay_sha256`
preserves exactly that property at ~150 bytes. Do not let a size argument erode it into a bare
id lookup.

**Consequence to handle deliberately:** with the five inner Activities untouched, the four
existing exclusion tests would keep passing *vacuously*. Section 3 item 5 must therefore flip
them to assert the wrapper is registered and the inner name still absent from `workflow.py`.

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

`maximum_attempts=2` is deliberate and `1` would be wrong. Ledger adoption only fires on a
*second* Temporal attempt: `Ledger.begin_operation` returns an existing artifact only when the
status is `effect` or `completed` (`ledger.py:213-214`). With `maximum_attempts=1` that proven
adoption path becomes unreachable, and a worker lost after `record_effect` fails the run
outright. Every other second-attempt outcome fails closed — `"Operation is quarantined"`
(`ledger.py:215-216`) or `"Operation is already claimed"` (`ledger.py:217-219`, because
`_activity_owner()` embeds `info.attempt` at `activities.py:144-146` and all five claim with
`retry_safe=False`). No path yields two effects. Pair it with `non_retryable_error_types` for
the exception classes the Activities actually raise, so a genuine failure is not masked by a
second attempt's `"quarantined"`.

Adding heartbeats to the replay Activities would be a real improvement, but it changes proven
Activity internals and belongs in a separate reviewed slice.

## 2b. Prerequisites — things registration breaks that do not exist today

**P1. Cancellation is destructive and terminal, and registration is what introduces it.**
All five Activities claim with `retry_safe=False` and quarantine on `CancelledError`
(`activities.py:1544-1550`, `:1852`, `:2101`, `:2435`, `:3099`). `Ledger.begin_operation`
refuses a quarantined key permanently (`ledger.py:215-216`), and operation keys derive from
admitted content, so recovery needs a new `run_id`, a new run spec, and a new human gate
decision. `start_to_close` expiry, workflow cancellation, and worker shutdown all deliver
`CancelledError`, and `Worker(graceful_shutdown_timeout=...)` defaults to zero while
`worker.py` sets no value.

Today the chain is only ever driven in-process by a harness that never cancels. After
registration a routine `Ctrl-C` during a 25-minute build permanently burns that admitted
replay. This must be resolved *before* registering: either give the Activities a reviewed
non-destructive cancellation path, or accept it explicitly with a set
`graceful_shutdown_timeout`, generous budgets, an operator runbook, and a test that asserts the
fail-closed behaviour so it is reviewed rather than discovered.

**P2. The "old History still replays" guarantee is currently untested.**
`test_history_is_compact_private_and_replay_safe` generates, in the same process from current
code, the history it then replays. There is no stored fixture in the repository. It is a
self-consistency check that cannot fail when someone changes `PortRunWorkflow`. Capture a
stored fixture at current HEAD *before* any change; without it, decision D1's central claim is
unverifiable.

## 2c. Two prohibitions that keep the ADK layer reachable

Both are free — they are things not to do, not work to do.

**Do not widen `CapabilityRole`.** It is `Literal["install_framework", "decode", "build"]`
(`replay_contracts.py:25`), re-checked in `AdmittedReplayV3.capability` (`:1594-1596`), and
`ToolchainProfileV3` forces bindings, tool roles, and execution plans to agree exactly
(`:637-650`). Because `admitted_replays_v3` is append-only (`ledger.py:100-105`), a widened
vocabulary is encoded in recorded rows that can never be rewritten. This is the one genuinely
irreversible mistake available here. A future ADK agent launches no subprocess and is not a
capability role.

**Do not add anything that assumes one decision per run.** The Workflow hard-codes
`gate_id="phase-a-approval"` with a single decision slot, but the *schema* already permits
many: `decisions.run_id` has no UNIQUE constraint and `gate_id` is a real column
(`ledger.py:46-49`), keyed only by `decision_id` and `idempotency_id`. So multi-gate needs no
migration and is safely deferred — provided this slice does not bake singularity into a helper.

Explicitly **rejected** for this slice, having been proposed and refuted: `maximum_attempts=1`;
threading an explicit `task_queue=` for a second worker that does not exist; and rewriting
`record_decision_activity` to use unbound ledger calls, which is Phase A's uniform convention
across thirteen sites and is idempotent by construction.

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

## 3b-corrected (2026-08-03)

A measured pass over F1-F4 found several claims in the section below to be wrong. The
follow-ups are kept verbatim as the record of what was believed; read this first.

**F3 is closed, by construction, for one constant.** A replay stage can act on a cancellation
only through a heartbeat response or a local `WORKER_SHUTDOWN` after
`graceful_shutdown_timeout` — and no replay stage heartbeats. So raising that window above the
longest stage budget (300 -> 10,800 s) means the destructive cancellation cannot arrive
mid-stage. What remains open is non-destructive cancellation *within* the window, which does
rewrite a reviewed invariant.

**The operating rule inverts.** A worker *kill* delivers no cancellation, leaves the claim
`pending`, and is recoverable by `release_pending_operation` with every completed stage
adopted — the run is **wedged, not burned**. A worker *stop* that exhausts the window is the
destructive one. Until cancellation is non-destructive, killing is the safe way to stop a
worker mid-stage — the opposite of the rule this document and `worker.py` carried.

**F4 does not require editing proven Activity bodies.** Every long operation inside a stage
yields the event loop (`await execute(...)`, and the `asyncio.to_thread` supervisors), and the
activity context is installed in the enclosing task — so a heartbeater in the *unproven
wrapper* works, with the five proven bodies byte-identical. §3b's "adding them also edits
proven Activity bodies" is false.

**F4 must not land before F3's remaining half.** Heartbeating is what opens the channel for
server-originated cancellation — a workflow cancel, a timeout, or a transient network failure
while recording a heartbeat. All land in the handler that quarantines. Shipping heartbeats
first would convert a flaky thirty seconds of network into a burned run.

**`cancellation_type=WAIT_CANCELLATION_COMPLETED` currently buys nothing** for the replay
stages, since the cancel it waits on is never delivered. It remains the correct default; the
rationale recorded for it is not the reason it is correct.

**Only 3 of the 5 stages quarantine unconditionally on cancel.** Build and verify already
release when cancellation lands before a workspace exists. §3b's "all five ... quarantine on
`CancelledError`" was wrong when written.

**Two ledger properties nothing documented**: `quarantine_operation` silently no-ops for a
non-owner, and `record_effect` is owner-fenced. So overlapping attempts cannot corrupt the
ledger — the loser can neither publish nor burn.

**A terminal case outside F1-F4**: once the verification grant is admitted, its five `UNIQUE`
columns plus the gate validator's timestamp window make that gate unrepeatable, so a crash
between admission and verify leaves a run that cannot be re-driven. Reached by normal
progress, not by an accident.

**F1 is understated.** The harness and the Workflow differ by a `-run` segment on the grant
and gate ids, and by the *entire suffix* on the capability id
(`-final-apk-decode-java` vs `-run-final-verification-decode`).

**F1a is closed, and its stated blocker was wrong.** `_verification_request_and_decision` now
calls `replay_gate.derive_verification_request`, so harness and Workflow compute one subject
through one function; the local `final_decode_capability` builder is deleted, and the run id is
named once by `authority_run_id(target)`. The reason F1 gave for deferring — that re-pinning
`capability_id` "without a real run would be asserting values nobody has observed" — does not
hold: all three ids are pure functions of `run_id` (`replay_gate.derived_identifier`, no ledger,
no store, no clock), so they are computable and checkable at rest. The new pins are
`real-replay-{340,430}-run-final-verification-{grant,gate,decode}`, and
`RealReplayHarnessFastTests.test_verification_gate_ids_are_derived_from_the_harness_run_id`
asserts both that the harness derives rather than restates and that the derivation still yields
those strings. What *does* still need the real run is F1b: the resulting `request.sha256`,
`grant_sha256` and evidence JSON, which no derivation can settle — but those change on any
re-run anyway.

**F2 has a cheap half, now done.** Public aliases in `activities.py` remove the private
coupling with every body and the executed call graph byte-identical. The real extraction —
deduplicating `resolve_admitted_build` against `_replay_verification_predecessors` — still
edits proven code and still waits for the real run.
**Superseded 2026-08-05: the real extraction is done and the aliases are deleted. See
§3c-measured.**

## 3c-measured (2026-08-04) — the first run through the registered Workflow

**Both targets completed.** Evidence markers at `success.json` under each run root.

| | 340 | 430 |
|---|---|---|
| stages | decode, apply, build, verify | install_framework, decode, apply, build, verify |
| activities scheduled | 8 | 9 |
| History, `to_json()` bytes | 64,563 | 62,261 |
| History, serialized | 27,326 | 23,426 |
| verification receipt | success, 65 assertions, 59 proofs | success, 16 assertions, 8 proofs |
| wall clock to verify | 3,301 s | 4,523 s |
| queries answered | 20 of 86 | 31 of 144 |

**F1 is settled with real evidence.** Both runs derive
`real-replay-{340,430}-run-final-verification-{grant,gate,decode}`, the Workflow published a
gate whose id equals the derivation, and the trusted client re-derived the subject from the run
id alone and typed back its own prefix (`357fdb5cdb91`, `70d045303780`). All three hash fields
are one derived request hash, as the contract requires. F1b — the request, grant and evidence
digests no derivation can settle — is now recorded per run.

**Section 3 item 3 is settled.** The 340 History is 64,563 bytes against the 256 KB budget, a
quarter of it. Under the design that passes `AdmittedReplayV3` by value it would have carried
over 510 KB of recipe and source paths. The privacy search found the control (the admitted
replay digest) inside the decoded surface and *not* in the raw JSON, with 19 and 21 payloads
decoded, so "no repository path, no source-tree marker" is an assertion that could have failed.

**Section 3 item 1 is settled by data, not by a target literal.** 430 ran
`install_framework` and 340 did not, from `ToolchainProfileV3.frameworks` alone. The first
version of the harness compared against the sibling harness's vocabulary, which says
`framework`; `ReplayRunResultV1` rejects any stage outside `REPLAY_STAGE_ORDER`, so that
comparison could never have passed on 430 and would have passed vacuously on 340.

**F2 is done** (2026-08-05). Its stated reason for waiting was that extracting
`_replay_verification_predecessors` edits a body inside the verify Activity's proven execution
path and would invalidate the real-run evidence. That evidence was re-established on both
targets through the registered Workflow, so the extraction was made:
`activities.resolve_replay_build(admitted)` is now the one implementation, and both
`_replay_verification_predecessors` and `replay_gate.resolve_admitted_build` call it. The three
public aliases are deleted — they removed the *private* half of the coupling and left the
duplication, which was the point of F2.

**Checked against the two completed runs rather than against the tests alone.** Re-deriving
each target's verification request through the extracted seam reproduces the exact subject hash
that run bound — `357fdb5cdb91143a` for 340 and `70d045303780eed5` for 430 — so the refactor is
byte-identical in effect on the only two real ports that exist. The signature-drift test is
replaced by one that asserts both callers name the same function and neither inlines the chain.


`tests/integration/test_registered_replay_harness.py` drives a real 340/430 replay
through `ReplayRunWorkflow` on a live Temporal server: this harness builds the authority and
starts the run, `python -m dfinsta_pipeline.worker` hosts it, and
`python -m dfinsta_pipeline.submission` answers the gate. Three processes, because that is how
it is operated. Two things were established in the first minute and one overturns §3b-corrected.

**The worker could not run a single real stage.** `run_worker` called `configure_runtime(state_root)`
and nothing else, so `source_root` was unset and `executor_paths` empty. `replay_apply_tree` and
`replay_verify_final_apk` refuse without the first; every subprocess-launching stage resolves its
executable through the second. Fourteen Activities registered, none of them runnable. Every
registration test passed, because a registration test proves a name is present, not that the
process hosting it can do the work. Fixed with `--source-root`, a repeatable
`--executor-path SHA256=PATH` and `--attempts-root`.

**A running stage blocks the worker's event loop, so F4 is not a small change.** §3b-corrected
argued heartbeats were cheap because "every long operation inside a stage yields the event loop
(`await execute(...)`, and the `asyncio.to_thread` supervisors)". That is **false for the decode
stage**, which contains no `asyncio.to_thread` at all: it reads the stock APK out of the CAS
(79 MB for 340, 133 MB for 430), writes it into the workspace, and afterwards runs
`capture_decoded_tree_fd` over tens of thousands of files — all synchronous. Measured, not
argued: the first version of this harness polled `query("status")` and died with
`RPCError: Timeout expired`, and `temporal workflow query`, an unrelated client, reports
`query timed out before a worker could process it` against the same running stage.

The worker records the other side of this itself. Its log fills with
`Query not found when attempting to respond to it … query task not found, or already expired` —
191 of them across one 340 and one 430 run: the server expired each query task while the loop
was blocked, and the worker answered after the stage let go.

**How wide the gap is, from the completed runs.** The harness samples a five-second query every
poll and records the outcome as `worker_query_responsiveness`. **66 of 86 samples went
unanswered on 340 and 113 of 144 on 430 — and the longest unbroken blocked stretch was over
nine minutes**, beginning 80 seconds in. Answered samples cluster at stage boundaries. So a
heartbeat interval would have to exceed nine and a half minutes to survive this workload
unchanged, and `heartbeat_timeout` longer still — which is most of what heartbeats were for.

**Attributed per stage, which corrects the first reading of this.** The blocking is not the
decode's tree capture specifically; it is every stage, and the worst offender already threads
its main work:

| stage | 340 blocked | 430 blocked |
|---|---|---|
| `replay_apply_tree` | 92% | 91% |
| `replay_decode` | 82% | 80% |
| `replay_build_patched_apk` | 62% | 68% |
| `prepare_replay_verification_gate` | 100% (1 sample) | 80% |

`apply` already runs `apply_port` under `asyncio.to_thread` and is still 91% blocked, so
threading a stage's *subprocess or mutation* is not what matters. And
`prepare_replay_verification_gate` launches nothing at all — it only reads the ledger and the
store — yet blocks too.

What all of them share is `decoded_artifact.materialize_decoded_tree` and
`capture_decoded_tree_fd`: two primitives, 15 call sites across the five stages, each walking,
hashing and writing tens of thousands of files synchronously. **That is the F4 prerequisite,
and it is two functions rather than five stages.**

It cannot land before the cancellation item, and not only for the reason already given:
`asyncio.to_thread` introduces new cancellation points, and a cancelled `to_thread` does not
stop the thread. That is exactly why `apply_port`'s task is wrapped in `_await_apply_mutation`,
a supervisor that waits for the thread before propagating cancellation. Any new threading has
to follow that pattern, so what cancellation *means* has to be settled first.

A heartbeater task in the wrapper would therefore be starved for the whole capture — exactly
when a heartbeat matters — and a `heartbeat_timeout` sized to a working heartbeater would then
expire and deliver the cancellation that quarantines. **F4 now has a prerequisite nobody had
written down**: move the synchronous capture off the loop, or accept and measure heartbeat gaps
the size of a full tree capture. The harness records a `worker_query_responsiveness` timeline so
the gap is a number rather than an impression.

Progress is consequently read from History, which the server serves with no worker involvement.
Queries are used only where the Workflow is parked in `wait_condition`, which is also why the
documented submission client works: at that moment nothing holds the loop.

**The verification grant is no longer single-shot in the only way that mattered.** The trap in
3b-corrected's last paragraph is real and both doors are now pinned by tests: a *different*
decision for the same run collides in `admitted_replay_verification_grants_v1`
(`test_phase_b_verification_grant`), and the journalled decision `submission.py` resubmits
verbatim is refused by the validator's `decision_time < gate_time` clause
(`test_phase_b_replay_workflow.test_a_decision_from_a_superseded_gate_is_refused`). Neither check
is wrong; asking twice is. `resolve_replay_verification_grant_activity` now runs before the gate
and, on a hit, the Workflow verifies against the recorded grant and never raises it. The result
still names the decision id, so a resumed run does not report a success nobody approved.

## 3d — can quarantine-after-workspace become a release? (audit, 2026-08-05)

The reachable destructive path, and the last thing between here and heartbeats. Audited
adversarially rather than assumed. **The finding is that release is safe for every invariant
this pipeline owns, and the one counter-case is external.**

**Nothing a stage writes between `workspace_created = True` and `record_effect` is shared.**
Every write lands inside the per-attempt workspace `attempts_root/<key>/sha256(owner)` or in the
CAS. Every `Ledger.*` call in that region is a `SELECT`. Workspaces are per-attempt because
`_activity_owner()` embeds `info.attempt`, and `_exclusive_directory` uses `os.mkdir` with no
`exist_ok`, so an activity-task redelivery of the *same* attempt raises `FileExistsError` before
`workspace_created` is set — the release branch, failing closed.

**The ledger already fences a zombie.** `release_pending_operation` blanks the owner, so a late
`record_effect` from attempt 1 sees a token that is not its own and raises. Two effects are
structurally impossible: the transition to `effect` requires `status == 'pending'` under
`BEGIN IMMEDIATE`. And CAS publication is atomic — `put_blob` writes a hidden temp with
`O_EXCL`, `fchmod(0o444)`, `fsync`, then `os.link` under the digest name, and every reader
re-verifies mode, size, uid, inode stability and hash.

**The repo already does this by hand and argues it is safe.** `claims.py` releases a `pending`
claim whose workspace is on disk — which is exactly what a SIGKILLed worker leaves — and says
so. The only thing it adds is a human confirming attempt 1 is dead. *That is the guard, and it
is the whole guard.*

**The counter-case.** `execute` owns only its direct child; a JVM grandchild can outlive
cancellation, and if `_clean_up` times out (`_CLEANUP_TIMEOUT_SECONDS = 1.0`) the process is
still running. Under release, attempt 2 could then build against state attempt 1 is mid-write
on — but only through a directory outside the workspace, and the only such directory is
apktool's `$HOME` framework fallback. **Closed 2026-08-05**: `ToolchainProfileV3` now refuses a
profile that declares frameworks while any role plan omits `framework_dir`. The rule lives at
the profile rather than in `RoleExecutionPlan` because only the profile knows whether there are
frameworks, and a native tool with no framework concept must still be expressible.

**Two latent fencing gaps, also closed**, both unreachable today and live the moment two
attempts can run: `record_effect` tested its idempotency shortcut *before* the owner check, so a
caller holding byte-identical output returned without ever being compared to the claim; and
`complete_operation` takes no owner at all, which turns out to be sound — the effect is the
authorisation — and is now pinned as such rather than reading like an oversight.

**Quarantine-after-workspace is drift.** It entered as a blanket quarantine with no release
branch at all; the release escape was carved out a day later and the cancel path kept the
original default. No commit message, no comment, no doc argues for it.

**What still stands between this and landing the release.** A liveness guard: an automatic
release removes the human check `claims.py` requires. The shape that preserves the counter-case
defence is to keep terminality for exactly the case where the subprocess could not be proven
dead — the `_clean_up` timeout — and release otherwise. Plus a workspace reaper (nothing removes
stale attempt directories, and a build workspace holds two APKs and a decoded tree), and an
update to `replay_workflow.py`'s retry-policy rationale, which currently argues from quarantine
being what makes attempt 2 fail closed.

## 3e — the liveness guard, designed and not yet built (2026-08-05)

§3d established that releasing a cancelled operation is safe *provided attempt 1 is dead*, and
that `claims.py` gets that guarantee from a human. An automatic release has to get it from the
code. This is the shape, written before implementing because it touches all five proven Activity
bodies and the executor's cancellation machinery.

**The rule.** Post-workspace, release — **except** where the subprocess could not be *proven*
reaped, which stays terminal. That keeps quarantine for exactly the case §3d's counter-case
needs and releases the rest, which is every ordinary cancellation.

**Where the signal is, measured across the three cleanup paths in `executor.py`:**

| path | shape | can it report? |
|---|---|---|
| `execute`'s own, on timeout or cancel (`:473`) | `await asyncio.shield(cleanup)` | **yes** — the `RuntimeError` propagates |
| `_launch_with_cancellation_cleanup` (`:378`) | `await asyncio.shield(_clean_up(process))` | **yes** |
| `_supervise_late_launch` (`:360`) | fire-and-forget task, result consumed by `_consume_supervised_result` | **no**, and structurally cannot: the launch never returned a handle inside the window, so there is nothing to await |

So two paths can carry it and the third is the case where a process may exist that we never got
a handle for — which must be treated as *not proven dead*, i.e. terminal, for the same reason.

**What to build.** A distinct exception (`ProcessNotReaped`, subclassing `RuntimeError` so the
existing non-retryable list still covers it) raised by `_clean_up` on timeout and by
`_supervise_late_launch`'s failure to obtain a handle; the five handlers then read
`isinstance(error, ProcessNotReaped)` to choose quarantine over release. Matching on a type, not
on the message — the current signal is the string `"Subprocess did not exit after kill"`.

**A masking defect found while measuring this, worth fixing regardless.** On `execute`'s main
path a cleanup timeout raises out of `await asyncio.shield(cleanup)` and *replaces* the
`CancelledError` that got there. So a cancelled activity whose child would not die is reported
as a plain failure, and the distinction the rule above depends on is destroyed one frame below
where it is read. Same shape as the unprotected `release_pending_operation` closed earlier the
same day.

**Two things that must land with it, not after:**

* **A workspace reaper, or a bounded attempts budget.** Release makes retries possible where
  they previously could not happen, and nothing in `src/` removes stale attempt directories. A
  build workspace holds a stock APK, an intermediate APK, a final APK and a full decoded tree.
* **`replay_workflow.py`'s retry rationale becomes false.** It argues `maximum_attempts=2` is
  safe because "every other second-attempt outcome fails closed", reasoning *from* quarantine
  being what stops attempt 2. Under release attempt 2 re-runs the stage instead of refusing.
  That is the intended behaviour, but the comment currently says the opposite.

## 3f — threading the decoded-tree primitives (designed and measured, 2026-08-05)

F4's other prerequisite, reviewed with a benchmark rather than an estimate. **The design already
exists in the codebase**: `replay_verify_final_apk` at `activities.py:3097` already runs
`capture_decoded_tree_fd` in a thread under a drain-then-propagate supervisor, file descriptor
and all. The change generalises that to the seven loop-side sites.

**Landed 2026-08-05, and measured on a real 340 port through the registered Workflow** — the
reproducible number, from a committed harness, superseding the standalone benchmark below:

| | before | after |
|---|---|---|
| query samples answered | 20 of 86 (23%) | **58 of 63 (92%)** |
| longest unbroken blocked stretch | 28 samples (~560 s) | **3 samples** |
| `replay_apply_tree` | 92% blocked | **0%** |
| `replay_decode` | 82% | 5% |
| `replay_build_patched_apk` | 62% | 17% |
| `prepare_replay_verification_gate` | 100% | **100%** |
| expired query tasks in the worker log | 69 | **0** |

The run itself was unaffected: four stages, verification success with 65 assertions, History
64,550 bytes of the 256 KB budget. Mid-decode queries that previously timed out now answer in
roughly 110 ms.

**The gate stage is unchanged, exactly as this design predicted**, because
`load_decoded_tree`'s standalone call sites in the validation helpers were deliberately scoped
out — it launches no subprocess and its blocking is entirely that function. That prediction
holding is the best evidence the diagnosis was right, and it is what makes the remaining work
worth doing rather than guesswork. The stage runs for seconds, so it is low priority.

**The prior standalone benchmark**, on a 209,426-file, 1.22 GB decoded tree with a 10 ms probe
standing in for a heartbeater. Recorded for its detail, but **note it is not reproducible from
the tree** — no benchmark script was committed:

| | idle | on the loop (today) | in a thread |
|---|---|---|---|
| probe ticks served | 98% | **0 of ~6,000** | ~93% |
| longest unbroken block | 0.012 s | **59-67 s, per capture** | **0.16 s** |
| wall-clock cost | — | — | 0-6% |

On the loop the probe did not tick once. A handful of these back to back is the nine-minute
stretch the real runs recorded. One thread is enough; a pool would add GIL contention without
shortening the tail. The benchmark ran on tmpfs, which is the *pessimistic* case — real disk
raises the I/O share and lowers the GIL share.

**Three things to get right, all of which would be silent if got wrong.**

* **Only the drain-then-propagate supervisors.** `_await_apply_mutation`,
  `_await_backend_composition` and `_await_verification_work` latch a cancellation and wait for
  the task. `_await_verification_execution` **cancels its task**, which is right for its own
  subject — an `execute()` coroutine that must kill and reap — and wrong for thread work.
  `capture_decoded_tree_fd` re-opens the caller's descriptor as its first act
  (`decoded_artifact.py:504`), so a `finally` closing that fd under a live thread could see the
  number reused and capture **the wrong directory**, publishing a valid-looking manifest. A
  wrong answer, not an error.
* **Heartbeat from the loop, never from the thread.** `activity.info()` works inside
  `asyncio.to_thread` because it copies the context, but `activity.heartbeat()` does not: for an
  `async def` activity temporalio skips the thread-safe wrapper, so the raw callback does a
  `put_nowait` on a non-thread-safe queue and *then* raises `RuntimeError: no running event
  loop` — details enqueued, flush never scheduled. And a test would not catch it:
  `ActivityEnvironment`'s heartbeat callback is a plain sync lambda, so heartbeat-from-thread
  passes there and fails against a real worker.
* **`load_decoded_tree` is a third primitive that must move.** It reads and hashes every blob
  (`decoded_artifact.py:558`), has 18 call sites in `activities.py`, and is the *only* reason
  `prepare_replay_verification_gate_activity` — which launches no subprocess — measured 80-100%
  blocked. Scoping the change to two primitives leaves the cheapest-looking stage the most
  blocked.

**Ordering.** The threading change is independent of the liveness guard and can land either
side of it; it adds no heartbeat and changes no ledger semantics. Landing it alone buys query
responsiveness, 0% to 93%, without opening the server-originated cancellation channel at all.
Heartbeats still come last, and `DEFAULT_GRACEFUL_SHUTDOWN_SECONDS` must be raised with them.

**Test gaps it would have to close**: `capture_decoded_tree_fd` has no direct unit test at all
(only fault-injection mocks); `_capture_decoded_tree_manifest` is named by no test; the four
supervisors have one direct test between them; nothing anywhere asserts a heartbeat.

**A working precedent for the heartbeat itself, found 2026-08-05.** Phase A's `apply_activity`
is the one Activity in the repo that calls `activity.heartbeat()`, and it does so **from the
event loop** in an `async def` activity — which is exactly the shape F4 needs and the opposite
of the from-a-thread shape that fails. It is worth reading before writing the heartbeater.

It also still carries the pre-2026-08-05 cancel handler — a bare
`except asyncio.CancelledError: quarantine_operation(...)`. **Examined and deliberately left**,
because its situation is not the replay stages': it claims with `retry_safe=True` rather than
`False`, it has no workspace and launches no subprocess, and its `record_effect` happens
*before* the heartbeat loop, so a cancellation there quarantines an operation whose effect is
already published and which `_adopt_existing` could otherwise adopt. That is arguably wrong for
the same reason the replay handler was, but it is a different argument about a different
activity — and this one's spec carries injection knobs (`apply_delay_seconds`,
`crash_after_effect`), so what it is *for* should be established before its semantics are
changed.

## 3b. Known follow-ups, deliberately not done in this slice

**F1. The harness and the Workflow derive different verification-gate ids.**
`tests/integration/test_real_replay_harness.py::_verification_request_and_decision` names ids
`real-replay-{target}-final-verification-*` and builds its capability with
`final_decode_capability(JAVA_SHA256, target)`. `replay_gate.derive_verification_request`
derives `{run_id}-final-verification-*`, and the harness `run_id` is
`real-replay-{target}-run`, so the two differ by a `-run` segment. Same run, two different
gate subjects and therefore two different request hashes.

The harness should call `replay_gate.derive_verification_request` so the proven path and the
Workflow agree byte-for-byte. It is **not** changed here because
`RealReplayHarnessFastTests.test_final_decode_capability_is_exact_for_both_targets` pins
`capability_id` to the target-derived string, and re-pinning it without a real run would be
asserting values nobody has observed. Do this in the slice that re-runs 340 and 430 through the
registered Workflow, where the new pins can be verified against actual evidence.

**F2. `replay_gate.resolve_admitted_build` depends on three private `activities` helpers.**
`_replay_build_predecessors`, `_replay_build_operation_identity` and
`_validate_replay_patched_apk_receipt`. The clean fix is to extract the pre-grant half of
`_replay_verification_predecessors` into a public seam. That was not done because
`_replay_verification_predecessors` sits inside the verify Activity's proven execution path,
and changing it trips the constraint that proven internals stay untouched. A signature-drift
test guards the coupling meanwhile. Bundle the extraction with the next real run, which
re-establishes the evidence anyway.

**F3. Cancellation remains destructive.** `graceful_shutdown_timeout` is now set and
documented, but it is a partial mitigation: no timeout makes stopping a worker safe during a
40-minute stage. A genuinely non-destructive cancellation path means editing the proven
Activities, which is its own reviewed slice.

**F4. Still no heartbeats**, so worker loss is undetected until `start_to_close` expiry, up to
three hours for verify. Adding them also edits proven Activity bodies.

## 4. NO-GO conditions

Do not proceed if any holds: the 340 History test cannot be made to pass without raising the
256 KB budget; reference-passing is shown to weaken admission in a case C2 does not cover;
old Phase A History replay breaks; or the reviewer rules that changing the five signatures
violates the Activity-identity constraint and D2b's second call path is also unacceptable —
in which case the payload problem needs a different answer before any registration.

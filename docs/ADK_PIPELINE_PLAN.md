# Google ADK Pipeline Plan

## Objective

Build a reusable multi-agent pipeline that ports DFInsta to new Instagram releases. Instagram 430 is the first fully traced replay fixture and future baseline candidate, not the final destination.

Temporal is the durable outer orchestrator for stage order, multi-day human waits, retries, cancellation, and workflow history. Google ADK owns bounded multi-agent reasoning for mapping, assessment, diagnosis, and drift review. Deterministic programs remain responsible for decoding, indexing, patching, building, signing, verification, and device evidence collection.

This plan incorporates the dry-run evidence in `docs/FINDINGS.md`, the current implementation records, `docs/adk_pipeline_design.md`, and `pipeline_flowchart.md`. The latter two describe the original concept but contain pre-ADK-2.x assumptions and unsafe authority boundaries corrected below.

## Authority Boundary

Agents may:

- Query deterministic indexes and request bounded excerpts.
- Rank mapping candidates and explain semantic evidence.
- Assess feature and privacy drift.
- Diagnose deterministic failures.
- Propose target resolutions, policy changes, and durable-memory updates.
- Return `unresolved` when evidence is insufficient.

Agents may not:

- Edit decoded trees or custom source directly.
- Execute arbitrary shell commands or choose unrestricted paths.
- Weaken verification to make a build pass.
- Sign, install, clear data, enter credentials, distribute, or commit changes without the appropriate human gate.
- Promote their own conclusions into permanent policy or baseline state.

Deterministic stages apply only schema-valid, immutable, approved specifications. Side effects run as ledger transactions keyed by operation kind and canonical input hashes. A replay adopts an existing validated result, quarantines incomplete output, and never infers completion from path existence alone.

## Persistent Records

The original flowchart's single Hook Manifest is split by lifetime and authority:

| Record | Purpose | Ownership |
|---|---|---|
| `FeaturePolicy` | Desired behavior and privacy disposition for a semantic feature | Human-owned, versioned |
| `HookIntent` | Version-independent intent, allowed strategies, semantic dependencies, and forbidden fallbacks | Human-owned, versioned |
| `BaselineBundle` | Accepted stock identity, patch source, resolved implementation, behavior contract, verifier, evidence, and waivers | Immutable after promotion |
| `RunSpec` | Baseline, target APK, toolchain, policies, signing policy, and device permissions for one run | Immutable |
| `ArtifactRef` | Kind, content hash, URI/path, producer, input hashes, and schema version | Immutable |
| `SurfaceIndex` | Structural, API, manifest, privacy, DEX, and packaging facts for one APK | Regenerable, content-addressed |
| `TargetResolution` | Target descriptors, methods, anchors, remaps, payload parameters, confidence evidence, and proof obligations | Per target, immutable after approval |
| `TargetPortSpec` | Complete approved input to deterministic patch/build/verify stages | Per target, immutable |
| `StageResult` | Attempt ID, input hashes, output references, status, and diagnostics | Append-only |
| `DecisionRecord` | Human decision, scope, evidence fingerprint, policy revision, and rationale | Append-only |
| `EvidenceClaim` | Feature/state, exact APK, device, preference state, restart, cache state, assertions, and result | Append-only |
| `DriftAuditReport` | Independent check against project objective, authority policy, and run scope | Append-only |
| `PromotionRecord` | Accepted release bundle, evidence, waivers, and signer identity | Human-approved, immutable |

Decision memory must not permanently suppress reassessment. A decision is reusable only while its semantic feature identity, delivery mechanism, evidence fingerprint, and policy revision remain compatible.

The initial implementation uses four envelopes rather than implementing every conceptual record as a separate service:

1. `IntentSpec`: feature policy plus version-independent hook intents.
2. `RunSpec`: admitted baseline, target, tool capability grants, budgets, and requested gates.
3. `ResolutionSpec`: target mappings, packaging backend, operations, and generated proof obligations.
4. `RunResult`: append-only stage results, decisions, evidence, audits, release identity, and promotion status.

Records split further only when independent lifetimes or concurrency demonstrate the need.

## Corrected Workflow

### 0. Run Admission

Validate the baseline bundle, target APK identity, package/version/variant, toolchain lock, privacy policy, signing policy, target devices, and allowed mutations.

Stop on any identity mismatch, unsupported APK composition, missing baseline acceptance, unapproved tool, or unresolved policy revision.

### 1. Target Ingest And Index

Decode the target into a content-addressed workspace and generate:

- Descriptor-to-file and DEX topology index.
- Superclass, interface, field, and method-shape index.
- Stable strings and endpoint occurrence index with bounded context references.
- Manifest, resource, preference, privacy, native-library, and SDK surface.
- Package composition and split/ABI/density inventory.

No decoded tree or large index enters ADK session state or an LLM prompt. Session state stores immutable references and hashes only.

### 2. Packaging Capability Probe

Determine whether full resource rebuilding is safe and which capabilities the target packaging backend supports. Candidate backends include full rebuild and stock-payload DEX grafting.

The probe runs before port planning because packaging constrains custom resources, manifest components, DEX placement, and Activities. A failed full rebuild never silently falls back to grafting.

### 3. Contract And Feature Drift

Deterministic workers compare the accepted baseline and target surfaces. They report additions, removals, changed occurrence roles, vanished semantic dependencies, delivery-mechanism changes, privacy drift, and packaging drift.

Initially one bounded read-only assessment agent evaluates deterministic evidence packets across these responsibilities. Specialist agents are added only when benchmarks show distinct failure clusters:

- Semantic feature and delivery classification.
- Telemetry, crash, permissions, and data-flow changes.
- Interpretation of deterministic packaging-capability failures.

A deterministic renderer produces the canonical report. LLM narrative is optional and non-authoritative.

This stage produces policy proposals only. Changed delivery mechanisms reopen previous decisions; no prior decision is silently inherited.

### 4. Per-Intent Resolution

Deterministic candidate generators resolve named descriptors, interfaces, stable references, method shapes, occurrence roles, field types/finality, and multi-signal intersections.

One or more `HookMappingAgent` calls receive only ranked candidates and bounded excerpts. Each returns alternatives, evidence, confidence, and unresolved dependencies. Numeric confidence is supplementary; automatic acceptance requires deterministic uniqueness and proof obligations.

### 5. Compile Target Port Specification

Deterministically compile approved decisions and resolutions into one immutable `TargetPortSpec`. Every operation must state exact anchor cardinality, idempotence marker, payload/remap, expected static sequence, forbidden structures, packaging backend, and behavioral claim.

### Gate 1. Port Plan Approval

The human submits a validated Temporal Update for one combined plan containing feature/privacy dispositions, unresolved alternatives, target resolutions, payload changes, packaging choice, proof obligations, and proposed device tests. The human may approve, correct, redesign, retire, waive, or defer each item. This gate is mandatory when policy, mechanism, packaging, privacy, resources, permissions, or mappings changed.

### 6. Clean Apply, Build, And Static Verification

Apply the immutable spec to a clean target workspace, build with the approved backend, and verify exact final structures and preserved payloads. Agents cannot mutate the spec inside this stage.

Failure routing:

- Environmental failure: bounded retry only with a changed environment/tool hash.
- Decode/index corruption: one fresh regeneration from the immutable APK.
- Anchor/remap/payload failure: return to resolution or scope review; no build retry.
- Repeated identical failure: stop immediately.

`FailureDiagnosisAgent` may classify evidence and propose the next transition. It cannot edit the work tree or verification contract.

### Gate 2. Signing Authorization

The human submits a hash-bound Temporal Update approving use of the stable release signing policy. Signing is deterministic; key access is not delegated to an LLM. Distribution is a separate later decision.

### 7. Release-Candidate Finalization

Align, sign, enforce package/min-SDK/signer/scheme policy, run final structural verification, and publish a no-clobber release candidate with bound lineage.

### Gate 3. Device And Data Authorization

Approve through a Temporal Update one hash-bound device plan listing every allowed install/update, signer migration, uninstall, clear-data operation, account use, preference mutation, tracing action, and cache operation. Credentials remain human-entered.

### 8. Device Smoke And Behavior Validation

First bind installed `base.apk` to the release candidate. Then verify startup, settings route, other-user exclusion, retained stock action, and per-feature behavior.

Each behavior claim ends as `passed`, `failed`, `inconclusive`, `not_exercised`, `blocked`, or `waived`. A verified contrast requires measured UI preference state, successful restart on both sides, state-specific required assertions, and a declared cache protocol.

The initial deterministic device executor supports installed-artifact binding, approved install/update, settings-state capture, approved switch mutation, force-stop/start, evidence capture, and restoration. It does not enter credentials or clear app data. Those remain manual actions recorded by the gate and evidence ledger.

### Gate 4. Distribution And Promotion Authorization

The human reviews failures, inconclusive claims, and proposed waivers, then submits a Temporal Update authorizing or rejecting distribution of the exact tested APK hash and promotion of its baseline bundle. Mandatory claims must pass or receive an explicit scoped waiver with rationale. The pipeline never distributes or commits automatically.

### 9. Baseline Promotion

Produce a proposed `BaselineBundle`, feature-policy changes, hook-intent changes, target resolution, and decision records. A human-reviewed source-control change promotes them. Agents never commit institutional memory directly.

## Drift Audits

An independent read-only `DriftAuditAgent` runs at four checkpoints:

1. After admission: target, scope, and end-goal alignment.
2. After target resolution: one-off assumptions, unsupported confidence, and authority violations.
3. After build/static verification: verification weakening, unexpected payload changes, and retry loops.
4. Before distribution: evidence completeness, waivers, signer identity, and baseline-promotion readiness.

The drift auditor cannot edit, approve, or block by itself. Deterministic policy checks convert severe findings into a required human review.

## Temporal And Google ADK Architecture

Temporal is the sole durable workflow engine. Phase A pins `temporalio==1.30.0`; Google ADK remains deferred until deterministic generalization, when `google-adk[db]==2.5.0` will be evaluated for bounded reasoning Activities.

- One Temporal `PortRunWorkflow` owns compact stage state, budgets, current gate, accepted decision IDs, and immutable artifact references.
- All file, database, subprocess, network, secret, ADK, signing, and device I/O runs in Activities. Workflow code remains deterministic and sandboxed.
- Authoritative human gates use validated Temporal Updates, not Signals, ADK `RequestInput`, or unrestricted Temporal UI/CLI submissions.
- Updates bind actor, run/gate IDs, exact subject hashes, policy revision, decision, rationale, timestamp, and idempotency ID. Authentication occurs in a trusted client before submission.
- Long-running Activities heartbeat, handle cancellation, use attempt-scoped outputs, and return compact references only.
- Temporal Activities are at-least-once. The external ledger and canonical operation key provide adoption, quarantine, and duplicate-effect protection.
- Temporal History is operational history; the external ledger remains authority for artifacts, decisions, evidence, and release lineage.
- APKs, decode trees, indexes, screenshots, and full reports stay in external content-addressed storage and never enter Temporal payloads or ADK prompts.
- Signing and device Activities use separate restricted task queues. Secrets remain only in their workers and never enter History, search attributes, ledger records, or ADK state.
- Workflow executions are pinned to a worker deployment version. Saved histories are replay-tested before workflow-code deployment.
- ADK agents run as bounded read-only Activities. Their structured outputs are stored externally and deterministically validated before workflow routing.
- Do not initially use the experimental deep Temporal-ADK plugin. Evaluate it only if per-model-turn durability becomes necessary.
- Do not use child workflows until a subprocess needs an independent lifecycle, gate, cancellation policy, or event-history boundary.

### Phase A Implementation Checkpoint

Commits `3e91eb5`, `ac4da5b`, `a92ae6d`, `ce97a9e`, and `618aca1` implement and harden the first durable slice. The current suite has 31 Phase A tests and 93 tests overall.

Proven:

- Strict versioned decoding for the four initial envelopes, canonical recursive JSON hashing, and immutable compact references.
- Admission, prepare, decision-recording, and apply Activities with a validated Temporal Update between prepare and apply.
- Gate decisions bind the canonical run, admission artifact, prepared artifact, actor, policy, timestamp, decision identity, and idempotency identity.
- Every downstream operation key and output reference includes complete upstream artifact and decision hashes.
- The SQLite ledger records append-only pending, effect, completion, and quarantine events; update/delete triggers prevent history rewriting. A transactional current-claim index allows only one owner to execute a pending operation.
- A synthetic post-effect retry validates and adopts one CAS effect before completion. Cooperative cancellation waits for cleanup and leaves the effect quarantined. Hard loss around a real subprocess remains unproven.
- A Worker can be replaced while the Workflow waits at a gate. Tests disable sticky caching to force History reconstruction and use the documented pinned-version override for synthetic traffic.
- Temporal CLI 1.8.1 / Server 1.31.2 has been stopped and restarted against the same SQLite file; fresh Worker and trusted-client connections recovered the pending gate and completed the Workflow.
- A three-day logical gate boundary is covered with Temporal time skipping. Saved History replays with unchanged code and fails against a deliberately incompatible Workflow definition.
- The executor binds each request to the admitted capability's canonical SHA-256, verifies an absolute executable against that capability before launch, renders only an exact argv template, passes only admitted environment values, constrains resolved workspace paths and audits declared mutations, validates artifact kinds, and rejects split APK sets. It uses `create_subprocess_exec`, never a shell.

Still pending before Phase A is considered production-ready:

- Launch the trusted client as a separate authenticated OS process. Fresh SDK connections after a persistent server-process restart are proven, but identity is still a test string rather than authenticated authority.
- Persist a sanitized representative History corpus and replay open and closed histories in deployment CI.
- Replace synthetic actor equality with authentication in a trusted submission client.
- Exercise hard Worker/process loss during a real child process. Current evidence covers injected failure and cooperative cancellation.
- Add OS-level confinement before treating an admitted tool as hostile. Workspace path and mutation checks are policy enforcement around a trusted digest, not a filesystem sandbox.
- Execute only immutable worker-owned tool copies. Portable Python cannot guarantee that a pathname hashed before launch still names the same bytes at process creation.
- Prove future signing/device worker secret isolation when those restricted task queues are introduced.

## Implementation Phases

### Phase A. Temporal Durability, Minimal Contracts, And Capability Model

Implement the four initial envelopes, strict unknown-field rejection, canonical serialization, hash-bound gate decisions, a local content-addressed store, and an append-only local stage ledger. A gate response binds actor, run ID, gate ID, subject hashes, policy revision, decision, rationale, timestamp, and idempotency ID.

Define a deterministic executor capability model before subprocess execution: approved tool digests, fixed command templates, workspace-root containment, artifact-kind checks, environment allowlist, and allowed mutation paths. Initial APK-composition support is a single monolithic APK; split sets fail at admission until explicitly implemented.

Pin the Temporal Python SDK and build one synthetic `PortRunWorkflow`: admission Activity, prepare Activity, approval Update, apply Activity, and final result. This phase contains no APK build, ADK agent, child workflow, signing, or device action.

Status: the synthetic durability, contract, ledger, and executor-capability slices are implemented. The production persistence/authentication items listed in the checkpoint above remain open; no APK, ADK, signing, or device operation has entered the Workflow.

Acceptance:

- Altering any referenced artifact/report hash fails validation.
- Any upstream hash change invalidates every downstream stage.
- Physical workspace paths are location metadata, not canonical identity.
- Unknown schema versions and unknown fields fail closed.
- Stale or hash-unbound approvals cannot authorize a transition.
- An executable outside the admitted digest allowlist cannot run.
- Worker and trusted client processes can stop while a gate waits and resume from persistent Temporal state.
- Invalid, stale, duplicate, and hash-mismatched Updates are rejected.
- An accepted decision is written to the external ledger before apply begins.
- Killing or cancelling an Activity after its synthetic side effect cannot duplicate or promote partial output.
- Time-skipping tests cover multi-day waits and gate timeout behavior.
- Saved History replays with unchanged Workflow code and rejects deliberate nondeterminism.
- No large bytes, secrets, private paths, or credentials appear in Temporal History.

### Phase B. Generic Intent And Resolution Engine

Convert the proven 340 and 430 implementations into version-independent `IntentSpec` data plus version-scoped `ResolutionSpec` fixtures. Implement one target-neutral engine that compiles a resolution into clean apply operations, a selected packaging backend, and generated static-verification assertions.

All descriptors, methods, fields, registers, DEX names, DEX counts, endpoints, anchors, and payload-preservation expectations live in fixture/spec data. Generic Python code contains no target-version branch.

Initial backends:

- Full apktool rebuild with an admitted toolchain profile.
- Stock-payload DEX graft with data-driven changed entries and explicit custom-DEX allocation/collision checks.

Acceptance:

- Both 340 reconstruction and 430 graft execute through the same engine from clean APKs.
- A second invocation adopts the validated ledger result rather than duplicating or overwriting it.
- Incomplete output is detected and quarantined.
- Every operation has exact anchor cardinality, idempotence marker, and generated final-DEX proof.
- 430-specific values occur only in its resolution fixture.

### Phase C. Generalization And Index Layer

Implement cached extraction plus the minimum demonstrated indexes: descriptor/DEX topology, method and field shape, stable strings/endpoints with occurrence context, and packaging inventory. Add bounded read-only candidate queries.

Immediately test the engine with mutation fixtures and a held-out or deliberately perturbed resolution before adding ADK:

- Case-collision filename and DEX movement.
- Ambiguous multi-candidate host.
- Missing anchor and changed field finality.
- Extra DEX collision.
- Unsafe resource rebuild requiring an explicit backend decision.

Acceptance:

- Repeated indexing produces byte-identical canonical JSON for the same APK/tool hashes.
- Descriptor lookup handles case-collision suffixes and DEX movement.
- Candidate queries do not rescan the full tree.
- Expected ambiguous fixtures remain unresolved.
- False automatic mapping acceptance is zero.
- No fixture requires a generic engine code change.

### Phase D. Google ADK Activity Capability Spike

Only after the deterministic generalization gate passes, pin `google-adk[db]==2.5.0` and invoke one bounded read-only mapping agent from a Temporal Activity. The Activity resolves bounded evidence references, runs ADK, stores complete structured output externally, validates it, and returns an `ArtifactRef`.

Acceptance:

- Temporal retry/adoption cannot silently select two different model outputs for one generation ID.
- Invalid agent output is non-retryable; another generation consumes an explicit workflow budget.
- ADK sessions and artifacts remain non-authoritative reasoning trace.
- No write-capable repository tool is exposed to the agent.
- Google ADK remains responsible for specialist composition and reasoning; Temporal only schedules the bounded invocation.

### Phase E. Temporal Golden Replay And Device Executor

Wrap the proven generic stages in `PortRunWorkflow` Activities and replay 340 and 430 without LLM mutation. Extend the gated device executor only with the controlled operations listed in Stage 8.

430 fixture assertions remain fixture data: six operations, four custom descriptors, 20 DEX files, unsafe full-resource rebuild, stock payload preservation, approved signer, installed-byte identity, startup, settings defaults, and same-key update.

Acceptance:

- Temporal Workflow and generic Activity code contain no 340/430 conditionals or constants.
- Both fixtures route from admitted input to their expected deterministic results.
- 430 preserves `observed`, `verified`, and `inconclusive` distinctions.
- Signing and device plans pause and resume through validated Temporal Updates.
- Unauthorized install, toggle, data clear, or credential action cannot execute.

### Phase F. Bounded Mapping Agent

Add one mapping/assessment agent only where deterministic candidate generation remains ambiguous. Benchmark against labeled 300-to-340 and 340-to-430 mappings plus mutation fixtures.

Acceptance:

- Automatic acceptance still requires deterministic uniqueness and proof obligations.
- Top-k recall, unresolved rate, review time, and false automatic acceptances are reported.
- False automatic acceptance remains zero.
- Agent output that conflicts with the fixture oracle remains unresolved and routes to Gate 1.
- No agent-proposed mapping can bypass anchor, data-flow, packaging, or static checks.

### Phase G. Feature Assessment And Unseen Release

Add deterministic surface diff plus bounded semantic assessment only after mapping performance is measured. Run the complete system against an unseen Instagram release without changing generic engine or workflow code.

Acceptance:

- New, removed, and changed-delivery features are compared against a labeled diff fixture before live use.
- Prior decisions reopen when semantic evidence changes.
- Recommendations cite evidence and never become policy without Gate 1.
- Ambiguity and packaging risk stop at the correct gate.
- The release bundle is reproducible from immutable inputs.
- A target-specific orchestration or generic engine code change is a failed generalization trial.

## Budgets And Terminal States

- Environmental execution: at most two retries, each with a changed environment/tool hash.
- Decode/index corruption: one clean regeneration from the immutable APK.
- Mapping proposals: at most two generations per unresolved intent.
- Port-spec revisions: at most three per run.
- Failure-diagnosis calls: at most two per failed stage.
- Invalid gate responses: at most three before the run becomes `blocked`.
- Identical input/failure hashes: no retry.
- Human gate timeout: configurable `blocked`; never implicit approval.
- Budget exhaustion: `blocked`, `deferred`, or `failed`, never an automatic loop.

## 430 Promotion Boundary

The current 430 release candidate has passed clean installation, exact installed-byte identity, logged-out and logged-in startup, settings entry, five checked defaults, and a same-key in-place update. It can be used immediately as a golden mechanical replay fixture.

It becomes the accepted behavioral baseline only after strict core contrasts are completed or explicitly waived. Profile-ad runtime absence remains inconclusive without eligible inventory and need not block pipeline implementation.

## Deferred Work

- Automatic distribution.
- Automatic git commits.
- Broad resource pruning or cache invalidation.
- Profile-ad inventory hunting as a prerequisite for orchestration.
- Full LLM topology before the Temporal durability spike, generic engine, and generalization fixtures pass.
- The experimental deep Temporal-ADK plugin.
- PostgreSQL/object storage until local single-worker replay is proven.

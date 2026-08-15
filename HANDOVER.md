# DFInsta Redux Project Handover

Evidence snapshot: 2026-07-31. The audited development baseline is branch `port-430` at commit `b68256ec64f50eeaecd64ea4ee3e5cc026f55022` (`Document final replay verification evidence`). `HANDOVER.md` is the concise entry point; supporting sources include [the detailed session handoff](docs/SESSION_HANDOFF.md), [the orchestration plan](docs/ADK_PIPELINE_PLAN.md), [the 340 reconstruction record](docs/RECONSTRUCTION_1.4.1.md), [the 430 mapping](docs/PORT_430_MAPPING.md), and [the device-validation record](docs/DEVICE_VALIDATION_1.4.1.md).

## Corrections, 2026-08-08

**This document is a 2026-07-31 snapshot and four things in it are now false.** Read this before
section 6, whose "exact continuation point" is one of them.

1. **The replay chain IS registered.** Nine statements here say it is deliberately not
   (`:34`, `:49`, `:59`, `:399`, `:404-415`, `:553`, `:596-600`). Registration landed 2026-07-31,
   and there are now two registered workflows — `ReplayRunWorkflow` and
   `FeatureAssessmentRunWorkflow` (`PortRunWorkflow` was registered here too until it was
   deleted on 2026-08-15: it ported nothing) — with 340 and 430 both completed
   through `ReplayRunWorkflow` against a live server on 2026-08-04. **An agent following the
   continuation point in section 6 would redo finished work.**
2. **`docs/FINDINGS.md` and `docs/adk_pipeline_design.md` are tracked**, not untracked
   (`:131`, `:386`, `:547`). Check with `git ls-files docs/`.
3. **The "eight anchored operations" claim at `:121` is wrong about two other files.**
   `docs/RECONSTRUCTION_1.4.1.md:52` and `docs/BLOG_NOTES.md:110` both say **seven**, and
   `dfinsta_source_1.4.1/patches/anchored_patches.json` holds seven.
4. **Every test count here is stale** (`:337-344`). The suite is ~3400 and moves most days;
   `docs/IMPLEMENTATION_STATE.md` carries the current figure.

For current state read [`docs/ROADMAP.md`](docs/ROADMAP.md) — it is the authority — then
[`docs/IMPLEMENTATION_STATE.md`](docs/IMPLEMENTATION_STATE.md). Everything below is preserved as
the audited record of what was true on 2026-07-31, including its evidence grading, which is why it
is not being rewritten.

## How to read claims

This handover does not treat its conclusions as infallible. Important claims use these fields:

- **Evidence**: repository paths, symbols, commits, tests, or external evidence files that support the claim.
- **Confidence**: high means independently encoded by current code/tests and matching artifacts; medium means supported by historical reports or device observations; low means an unresolved hypothesis.
- **Uncertainty / disproof**: the remaining limitation and concrete evidence that would overturn the claim.
- **Fresh review**: `yes` means the incoming agent should independently review the conclusion before extending it.

When a prose document conflicts with executable contracts, current code plus passing tests wins for software behavior. Device behavior requires the versioned behavior contract plus matching evidence, not code alone.

## 1. Executive overview

DFInsta Redux reconstructs and forward-ports a distraction-reducing Instagram APK modification. It recovers a maintainable patch from stock and modified APKs, maps behavior across heavily obfuscated Instagram versions, applies target-specific Smali/resource operations, rebuilds or DEX-grafts an APK, verifies the resulting archive and decoded Smali, and records provenance in a durable Temporal/CAS/SQLite pipeline.

The project exists because a direct port of the legacy DFInsta 1.3 patch is unsafe. Instagram obfuscation changes class names, fields, method layouts, DEX placement, resources, and network paths between versions. Copying an old whole host class into a newer APK imports thousands of stale incidental references. The maintained approach expresses target-neutral intent, resolves it against each target, and applies narrow operations to that target's own classes.

Principal inputs and outputs:

| Input | Role | Output |
|---|---|---|
| Stock Instagram 340 APK plus DFInsta 1.4.1 oracle | Recover and validate the maintained 340 baseline | `dfinsta_source_1.4.1/`, full rebuilt unsigned APK, verification report |
| Stock Instagram 430 APK plus Android API 36 framework | Forward-port the approved behavior without rebuilding lossy resources | `dfinsta_source_430/`, stock-preserving DEX-graft APK, build/verification reports |
| `pipeline_specs/intent_v2.json`, target resolutions, source manifests | Target-neutral intent and exact target operations | Compiled target port specification and static assertions |
| Gate decisions, tool capabilities, APK/framework/source artifacts | Durable execution authority | CAS artifacts, SQLite ledger events, stage receipts, final verification receipt |
| Signing policy and private signing environment | Separate release gate | Aligned/signed APK plus release and signed-verification reports |

Major pipeline stages are: admit immutable inputs and capabilities; install framework if required; decode stock APK; stage admitted patch sources; compile/apply deterministic operations; build with apktool/aapt1; for 430, graft only changed DEX entries into stock; separately authorize and re-decode the final APK; run static assertions and operation proofs; adopt completed results without relaunch; later sign/publish and validate on a device under separate gates.

**Current high-level conclusion:** the target-neutral engine and all framework/decode/apply/build/final-verification Activities are implemented. Real 430 proof matches current executable code; 340 has historical real proof plus current fixture/unit compatibility, but its real run predates the latest generic verifier normalization. ~~Those Activities are deliberately not registered in the durable Workflow.~~ **Registered 2026-07-31; see the corrections at the top.** Authenticated production authority, hard process-loss recovery, signing/publication orchestration, and controlled runtime contrasts remain separate work.

- **Evidence:** `src/dfinsta_pipeline/activities.py` symbols `replay_install_frameworks_checkpoint_activity`, `replay_decode_checkpoint_activity`, `replay_apply_tree_checkpoint_activity`, `replay_build_patched_apk_checkpoint_activity`, and `replay_verify_final_apk_checkpoint_activity`; exclusion assertions in `tests/test_phase_b_*_activity.py`; `src/dfinsta_pipeline/worker.py:34`; `docs/SESSION_HANDOFF.md` Phase B checkpoint; canonical evidence listed in section 5.
- **Confidence:** high for mechanical direct-Activity execution; medium for the broader release readiness statement.
- **Uncertainty / disproof:** a fresh 340/430 run at a later commit could expose toolchain or source drift. A registered Workflow replay test would disprove the current registration gap. Controlled device evidence could improve runtime confidence.
- **Fresh review:** yes, before Workflow registration or any release claim.

## 2. Architecture and repository map

### Core components

| Path | Responsibility | Authority / important symbols |
|---|---|---|
| `src/dfinsta_pipeline/contracts.py` | Phase A canonical artifacts, gates, run contracts | `canonical_json`, `canonical_sha256`, `RunSpec`, `GateDecision` |
| `src/dfinsta_pipeline/worker.py` | Worker entry point and registered Activities | `run_worker`; currently excludes all replay checkpoint Activities |
| `src/dfinsta_pipeline/executor.py` | Admitted subprocess execution with exact argv/environment/mutation policy | `execute`; no shell, bounded direct-child cleanup, not an OS sandbox |
| `src/dfinsta_pipeline/store.py` | Immutable filesystem content-addressed store | no-clobber publication and strict blob reads |
| `src/dfinsta_pipeline/ledger.py` | SQLite decisions, replay/grant authority, append-only operation events, current owner claims | `Ledger`; use concrete class methods for authority-sensitive calls |
| `src/dfinsta_pipeline/port_contracts.py` | Strict target-independent operations, backends, assertions, target resolutions | operation/assertion dataclasses and decoders |
| `src/dfinsta_pipeline/compiler.py` | Pure intent + resolution compiler | `compile_port`; must remain target-literal-free |
| `src/dfinsta_pipeline/apply.py` | Generic operation application to a decoded tree | exact postconditions and fail-closed partial-state behavior |
| `src/dfinsta_pipeline/backend.py` | Executes full rebuild and stock DEX-graft archive composition | archive writer/reader and ZIP metadata verification; backend contract classes are defined in `port_contracts.py` |
| `src/dfinsta_pipeline/verifier.py` | Static assertions, operation proofs, semantic Smali round-trip checks | final decoded-tree verification and Smali normalization |
| `src/dfinsta_pipeline/replay_contracts.py` | Replay-v3 authority and stage/final receipts | `AdmittedReplayV3`, `ReplayPatchedApkReceiptV1`, `AdmittedReplayVerificationGrantV1`, `ReplayFinalApkVerificationReceiptV1` |
| `src/dfinsta_pipeline/activities.py` | Ledger-owned, retry/adoption-aware Activities | checkpoint symbols named above; currently unregistered |
| `src/dfinsta_pipeline/decoded_artifact.py` | Exact Linux decoded-tree capture/materialization | descriptor-relative, no-follow manifests and CAS child binding |
| `src/dfinsta_pipeline/source_admission.py` | Immutable authored source staging | source manifests under `pipeline_specs/source_manifests/` |

Data flow is authority-first: a gate decision and immutable artifact references are recorded; `AdmittedReplayV3` binds exact capabilities/tool plans and source/APK/framework lineage; each Activity obtains a single-owner ledger claim, materializes CAS inputs into an attempt-local workspace, invokes `execute()` where external work is required, validates outputs, publishes CAS effects, and appends completion. Retries validate and adopt completed output rather than relaunching. Final APK decode needs a second decision and `AdmittedReplayVerificationGrantV1` because the original decode capability only authorized the stock APK.

### Patch and evidence trees

| Path | Meaning | Maintained or generated |
|---|---|---|
| `dfinsta_source_1.4.1/` | Privacy-hardened source reconstructed for Instagram 340 | maintained baseline |
| `dfinsta_source_1.4.1/newCode/`, `newRes/`, `appendRes/`, `resourcePatches/`, `manifest/`, `patches/` | Custom classes, resources, resource deltas, manifest components, endpoint/anchored operations | maintained |
| `dfinsta_source_1.4.1/oracleDeltas/` | Normalized historical oracle evidence | maintained evidence, not forward-copy source |
| `dfinsta_source_430/` | Minimal resource-free Instagram 430 port | maintained target source |
| `dfinsta_source_430/newCode/` | Exactly four approved custom classes | maintained; overlaid into `smali_classes20` |
| `dfinsta_source_430/patches/anchored_patches.json` | Seven exact host operations | maintained; descriptor/anchor/marker cardinality is authority |
| `pipeline_specs/` | Shared intent, generated target resolutions, exact source manifests | tracked generated specifications; regenerate only with reviewed clean decodes |
| `tools/reconstruction/` | 340 inventory, delta extraction, preparation, build, and verifier | maintained tools |
| `tools/port_430/` | Fresh 430 decode, minimal overlay/patch, DEX graft, verifier | maintained tools |
| `tools/phase_b/generate_specs.py` | Deterministically regenerates `pipeline_specs/` | maintained generator |
| `tools/release/finalize.py`, `release/signing_policy.json` | Separate align/sign/verify/no-clobber publication gate | maintained; secrets remain external |
| `tools/device_validation/runner.py` | Provenance-bound ADB startup/settings/feature evidence | maintained; device dependent |
| `tests/` | Phase A/B contracts, executor, ledger, activity, fixture, verifier, and fast harness tests | maintained |
| `tests/integration/test_real_replay_harness.py` | Opt-in real apktool chain and evidence exporter | maintained; skipped by default |
| `work/`, `TESTING-PLAYGROUND/*-src/`, `instagram_source/`, `.pipeline-state/` | Decodes, builds, device evidence, experimental state | generated/evidence; never edit as patch source |
| `apks/`, `apktool_2.9.3.jar` | Local ignored upstream/oracle artifacts | immutable inputs, hash before use |

### Authoritative documents

- [`docs/SESSION_HANDOFF.md`](docs/SESSION_HANDOFF.md): detailed chronological technical checkpoint and evidence hashes. Early checkpoint test counts such as 97/391/394 are historical, not current; use its latest dated checkpoint.
- [`docs/ADK_PIPELINE_PLAN.md`](docs/ADK_PIPELINE_PLAN.md): durable orchestration threat model, completed Phase A/Phase B guarantees, and production gaps.
- [`docs/RECONSTRUCTION_1.4.1.md`](docs/RECONSTRUCTION_1.4.1.md) and [`docs/DFINSTA_1.4.1_DELTA.md`](docs/DFINSTA_1.4.1_DELTA.md): 340 recovery method and oracle delta. The latter describes oracle behavior; privacy decisions were later applied in [`docs/PRIVACY_1.4.1.md`](docs/PRIVACY_1.4.1.md) and [`docs/CLEANUP_1.4.1.md`](docs/CLEANUP_1.4.1.md).
- [`docs/PORT_430_MAPPING.md`](docs/PORT_430_MAPPING.md): 340-to-430 mapping and device history. Its “Next Deterministic Work” predates the completed release finalizer and mechanical Phase B verifier, so use it for mapping/runtime gaps, not current orchestration status.
- [`docs/DEVICE_VALIDATION_1.4.1.md`](docs/DEVICE_VALIDATION_1.4.1.md): versioned device behavior and stale UI-test warning.
- `dfinsta_source_1.4.1/behavior_contract.json` and `dfinsta_source_430/behavior_contract.json`: machine-readable runtime contract and evidence links.
- `AGENTS.md`: mandatory build/source/porting safety rules.

**Architecture conclusion:** canonical authority is immutable artifacts plus ledger records, not caller paths or self-asserted hashes.

- **Evidence:** `store.py`, `decoded_artifact.py`, `ledger.py` append-only triggers, `replay_contracts.py`, tests named `test_unrecorded_*`, `test_*tamper*`, and AgentMemory lesson `lsn_e4b0b9e054c3d8ed`.
- **Confidence:** high within tested same-host assumptions.
- **Uncertainty / disproof:** same-UID hostile mutation and OS-level process confinement are not proven. A successful mutation/takeover test against current code would disprove the tested boundary.
- **Fresh review:** yes for security claims or deployment outside a trusted worker host.

## 3. APK and Smali technical knowledge

### Fixed target facts

| Target | Exact input | Backend | DEX/custom placement | Resources |
|---|---|---|---|---|
| 340 | Instagram `340.0.0.22.109`, version code `374010893`, arm64-v8a, min API 28 | apktool 2.9.3 full rebuild with aapt1 | 11 DEX files; nine maintained custom descriptors in `classes11.dex` | full source/resource/manifest reconstruction |
| 430 | Instagram `430.0.0.53.80`, version code `383611248`, arm64-v8a, min API 28, listed density split metadata | apktool changed-DEX assembly then stock ZIP graft | replace `classes.dex`, `classes3.dex`, `classes4.dex`, `classes6.dex`; add four custom descriptors in `classes20.dex`; preserve all other DEX | preserve stock `resources.arsc`, binary manifest, and `res/` bytes exactly |

Input hashes verified on 2026-07-31:

- `apktool_2.9.3.jar`: `7956eb04194300ce0d0a84ad18771eebc94b89fb8d1ddcce8ea4c056818646f4`
- stock 340 APK: `68f4546f8cb597a668d6033916200ef99191a9006350fcd986fd33392aea5113`
- stock 430 APK: `38ae9861b9ca89f60f41767324e1c3d54a4e3a00ed5555b92660a08e6db14754`
- DFInsta 1.4.1 oracle APK: `0b7b858216d113019af4cf76d9db330ca583573c27dd59fff6cfaffbed7c7776`
- API 36 framework: `1f95cd4676f3e16e0432a0f19c01026593101fd26d8190233c70803de8453473`
- Java launcher used by the real harness: `1a86d087fa5a5be1ed3e8a531ae891da85fc80aad15ab6fa98060763f2eb7000`

Current executable authority resolves 340 to 59 total operations: 45 Smali edits (30 endpoint manifest records expand to 38 compiled method-scoped endpoint operations, plus seven anchored edits) and 14 resource/manifest/overlay operations. One compiled endpoint operation covers two matched literal occurrences. It resolves 430 to eight operations: seven host Smali edits plus one custom-code overlay. The seventh anchored edit, `install_settings_long_click_actionbar`, was added on 2026-08-01 after a device session showed the original settings hook was runtime-inert; see `docs/PORT_430_MAPPING.md`. `docs/RECONSTRUCTION_1.4.1.md` and `docs/BLOG_NOTES.md` contain an older “eight anchored” count; the current patch manifest, generated resolution, and fixture tests supersede that number.

- **Evidence:** `pipeline_specs/resolutions/instagram_340.json`, `pipeline_specs/resolutions/instagram_430.json`, both source manifests, `dfinsta_source_1.4.1/patches/anchored_patches.json`, `dfinsta_source_430/patches/anchored_patches.json`, `PhaseBFixtureTests`, `PhaseBApplyFixtureTests`, and `RealReplayHarnessFastTests.test_target_table_exact_hashes_and_counts`.
- **Confidence:** high for the exact current specifications and pinned artifact hashes.
- **Uncertainty / disproof:** any maintained source, generated spec, backend, tool, or target change can invalidate these counts/topologies; generator output or fixture disagreement would disprove the claim.
- **Fresh review:** yes after any source/spec/tool/target change; regenerate and review rather than copying these numbers forward.

### Porting and patch invariants

1. Diff independent clean stock and modified decodes. `tools/reconstruction/inventory.py` resolves classes by in-file `.class` descriptor and strips `.line`, comments, and blank lines for comparison. Never derive a delta from an already overlaid tree.
2. Map behavior, not old obfuscated filenames. Prefer stable named types/interfaces and endpoint strings, then structural shape, then numeric constants. `docs/FINDINGS.md` is useful working-tree evidence, but it is ~~currently untracked~~ **tracked since 2026-08-01**; `docs/DFINSTA_1.4.1_DELTA.md` and `docs/PORT_430_MAPPING.md` are the tracked maps.
3. Reuse the target decode's exact path after resolving the descriptor. Obfuscated classes can move across `smali_classesN`, and Windows apktool can add unstable `.N` filename suffixes for case collisions.
4. Apply narrow instruction sequences. `patches/anchored_patches.json` ignores `.line` and blank-line noise, enforces exact anchor and marker counts, is idempotent, and rejects missing, duplicate, or partially applied state.
5. Never copy a complete old Instagram host class forward. Whole-class overlays are allowed only for the approved custom code bundles identified by exact source manifests.
6. Treat `.locals`/`.registers` and every reused register as part of the anchor contract. Current payloads deliberately reuse registers proven live at their insertion points: for example, 430 `install_settings_long_click` reuses `v0`, `v6`, and `p3`; 340 `clear_feed_cache_reference` anchors `.locals 10`. Do not increase locals casually: parameter register aliases can shift in a `.registers` method. Re-evaluate invoke argument width/contiguity and use `/range` only when required by the exact target method.
7. Preserve exact method signatures and invoke kind. Examples: `startapp.setContext(Landroid/app/Application;)V`, `hooks.throwIfBlocked(Ljava/net/URI;)V`, `View.setOnLongClickListener(Landroid/view/View$OnLongClickListener;)V`, and `replaceReelsEndpoint(Ljava/lang/String;)Ljava/lang/String;`. A verifier-clean assembly does not prove a wrong receiver/interface invoke is runtime-safe.
8. Keep Tigon blocking inside the existing request-failure try/catch. Both 340 and 430 inject `hooks.throwIfBlocked(URI)` into `TigonServiceLayer`; its `IOException` becomes a normal failed request. Moving it outside the protected range changes behavior.
9. Do not infer semantic changes from routine DEX round-trip noise. Redundant catch directives, label names, unused aliases, method order, and `const-string`/`const-string/jumbo` can change. `verifier.py` normalizes method-scoped labels to target instruction positions, distinguishes switch payload references, and sorts fully canonicalized methods. It must still reject changed branch targets, handlers, payload ownership, signatures, annotations, or method bodies.
10. Use comments, not no-op labels, as source idempotence markers when baksmali may delete labels. The 430 endpoint operations use `# dfinsta_reels_*_endpoint` markers. Comments are source-application markers; final verification relies on semantic instructions, not comments.
11. Preserve synthetic/bridge and annotation structures unless the target-specific operation explicitly covers them. The old oracle contains synthetic lambdas and broad round-trip differences, but those are not forward-port intent. A changed custom descriptor set or host method sequence must fail static verification.
12. Respect multidex placement assertions. Descriptor lookup may locate a moved class, but target assertions separately enforce approved DEX topology and custom descriptor placement.

### Resources and archive behavior

340 can be rebuilt fully with aapt1. Its maintained source adds resources/manifest content, removes `assets/drawables.bin`, and excludes inherited Amplitude/ACRA behavior under the hardened policy. See `dfinsta_source_1.4.1/README.md` and `docs/CLEANUP_1.4.1.md`.

430 must remain resource-free unless a new non-lossy packaging method is independently proven. Apktool reports sparse-resource warnings and its rebuilt resource payload lost stock data. The accepted design compiles only changed DEX files, grafts them into the exact stock ZIP, strips old signing entries, and preserves all other local/central ZIP metadata and payload bytes. The final custom settings surface is a framework `AlertDialog`; it uses no custom resource ID or manifest component.

For newly added ASCII DEX only, CPython `zipfile.writestr` deterministically clears input data-descriptor/UTF flags and maps zero external attributes to safe `0600`. `backend.py` permits that exact normalization only for added entries. Retained and replacement entries remain strict.

### Toolchain/build/runtime failure modes

- Apktool 2.9.3 and aapt1 are required. Apktool 3.x removed aapt1 support.
- Apktool build mutates its decoded input under `build/` and can create `framework/1.apk` even for a no-framework build. Activities admit, scan, clean, and revalidate only those observed scratch effects.
- Real decoded trees contain `AUX.smali` and hundreds of case-distinct paths. Exact canonical decodes are Linux-only; do not apply Windows reserved-name or casefold uniqueness rules to them.
- Build success proves assembly, not Android verifier/runtime behavior. Final re-decode/static assertions do not replace installation/startup/feature contrast tests.
- Signature mismatch prevents update install. A test key cannot update an oracle/release-key APK; uninstalling loses data and requires explicit user approval.
- Feature preference changes require process restart. Warm caches can preserve feed/Reels content; record cache state rather than assuming a fixed number of swipes clears it.
- The user's own story remains visible when Stories are disabled; absence of all story UI is not the intended assertion.
- Legacy UI Automator expects `Password`; current logged-out screens commonly show `Join Instagram` and `I already have a profile`.

**Smali correctness conclusion:** current 340/430 source and generated operation proofs survive apktool assemble/re-decode semantic normalization.

- **Evidence:** real success files in section 5; tests `PhaseBVerifierTests.test_smali_labels_are_normalized_for_operation_and_overlay_proofs`, `PhaseBFixtureTests.test_430_exact_ownership_payload_and_final_sequences`, and tool source-policy tests.
- **Confidence:** high for these exact APK/tool hashes; medium for future Instagram versions.
- **Uncertainty / disproof:** ART installation or class loading could reveal verifier-sensitive issues not represented by static checks. A future baksmali version may normalize differently.
- **Fresh review:** yes for every new target, changed apktool version, register edit, handler boundary, or invoke signature.

## 4. Development workflow

### Environment

`pyproject.toml` requires Python `>=3.11,<3.14` and pins `temporalio==1.30.0`. On 2026-07-31 the system `python3` was 3.14.4 and therefore unsupported; `.venv/bin/python` was 3.13.14. Use the virtualenv explicitly.

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -e .
```

Known/observed tools:

| Tool | Required/observed version | Notes |
|---|---|---|
| Python | 3.11-3.13; observed venv 3.13.14 | never use current system 3.14 for this project |
| `temporalio` | 1.30.0 | SDK test server reported CLI 1.8.1 / Server 1.31.2 |
| apktool | exactly 2.9.3 | repository-local ignored JAR, hash above |
| Java | observed OpenJDK 25.0.3 | historical real runs used `/usr/lib/jvm/java-25-openjdk-amd64/bin/java`; version is observed, not contractually proven minimal |
| Android build tools | observed 36.0.0 | `aapt`, `apksigner`, `zipalign` under Android SDK; not on `PATH` in this audit |
| ADB | SDK `platform-tools/adb` exists | not on `PATH` in this audit; no device was exercised for this handover |
| API 36 framework | exact hash above | required for 430 decode/build |

### Prepare and rebuild 340

Use a fresh stock decode and fresh outputs. Example from `dfinsta_source_1.4.1/README.md`:

```bash
java -jar apktool_2.9.3.jar d \
  -p <new-340-framework-cache> \
  -o <new-stock-340-decode> \
  'apks/com.instagram.android_340.0.0.22.109-374010893_minAPI28(arm64-v8a)(nodpi).apk'

.venv/bin/python tools/reconstruction/rebuild.py \
  <new-stock-340-decode> \
  dfinsta_source_1.4.1 \
  apktool_2.9.3.jar \
  --work-tree <new-340-work-tree> \
  --output-apk <new-340-unsigned.apk>
```

Expected outputs are the unsigned APK, adjacent endpoint/anchored reports, and `<output>.verification.json`. Put all outputs in a new empty parent directory. `rebuild.py` refuses existing work/APK paths but does not preflight its three derived report paths, whose writers overwrite; manually require those report paths to be absent. It builds with `--use-aapt1` and runs hardened verification. Do not add apktool `-f`: the decode destination must truly be new.

For patch-development internals, use `tools/reconstruction/README.md`: inventory independent decodes, analyze class/value deltas, extract normalized direct-host diffs, then apply endpoint and anchored manifests. Do not rerun `bootstrap_source.py` over the maintained source; it refuses overwrite by design.

### Build 430 from clean stock

```bash
.venv/bin/python tools/port_430/build.py \
  <new-stock-430-decode> \
  'apks/com.instagram.android_430.0.0.53.80-383611248_minAPI28(arm64-v8a)(360,400,420,480dpi).apk' \
  dfinsta_source_430 \
  apktool_2.9.3.jar \
  work/430-port/framework-res-api36.apk \
  --framework-path <new-framework-cache> \
  --work-tree <new-430-work-tree> \
  --output-apk <new-430-unsigned.apk>
```

Every generated destination must be absent. Expected outputs include the unsigned graft, `*-intermediate.apk`, anchored report, `<output>.verification.json`, and `<output>.build.json`. The build decodes stock itself; the first positional path is a new destination.

### Regenerate target specs

Only regenerate after reviewed source/anchors and independent clean decodes are available:

```bash
.venv/bin/python tools/phase_b/generate_specs.py \
  --repo-root . \
  --output pipeline_specs \
  --stock-340-decode <clean-340-decode> \
  --stock-340-apk <stock-340.apk> \
  --stock-430-decode <clean-430-decode> \
  --stock-430-apk <stock-430.apk>
```

Then require `PhaseBFixtureTests.test_generator_output_is_byte_identical` and review every changed resolution/source-manifest hash. The generator writes tracked files; do not run it casually in a dirty source tree.

### Real ledger-owned replay

This is intentionally opt-in and long-running. Use a new absolute root outside the repository; never reuse or delete failed evidence roots.

```bash
DFINSTA_RUN_REAL_REPLAY=1 \
DFINSTA_REAL_REPLAY_TARGETS=340 \
DFINSTA_REAL_REPLAY_ROOT=/absolute/new/absent/root \
PYTHONPATH=tests \
.venv/bin/python -W error -m unittest -v \
  integration.test_real_replay_harness.RealReplayIntegrationTests.test_real_replay_checkpoint_activities
```

Use `340,430` or `430` for other targets. The run can take tens of minutes and several GB. Success/failure appears in the root as an exclusive marker and includes ledger/process/artifact evidence. The successful historical roots are external to Git and listed in section 5.

### Align, sign, and final release verification

The currently exercised `tools/release/finalize.py` path is for the 430 verifier and requires verified unsigned build reports plus three secret environment variables. This is a sensitive, side-effecting release operation, not a routine build. Do not commit or log secret values:

```bash
export DFINSTA_KEYSTORE=/secure/path/release.keystore
export DFINSTA_KEY_ALIAS=<alias>
export DFINSTA_KEYSTORE_PASSWORD=<secret>

.venv/bin/python tools/release/finalize.py \
  <unsigned.apk> <stock.apk> \
  --unsigned-build-report <unsigned.build.json> \
  --unsigned-verification-report <unsigned.verification.json> \
  --policy release/signing_policy.json \
  --zipalign "$ANDROID_SDK_ROOT/build-tools/36.0.0/zipalign" \
  --apksigner "$ANDROID_SDK_ROOT/build-tools/36.0.0/apksigner" \
  --aapt "$ANDROID_SDK_ROOT/build-tools/36.0.0/aapt" \
  --apktool-jar apktool_2.9.3.jar \
  --final-verifier tools/port_430/verify_apk.py \
  --output-apk <new-signed-output.apk>
```

The output parent must exist and all three final destinations must be absent. The script aligns, signs, checks package/minSdk/signature policy, invokes final verification, and hard-links each output/report without clobber. Publication is ordered verification report, release report, then APK; it is not an atomic three-file transaction, so a late link failure can leave partial reports. The password is kept off argv but remains in the child environment; this is not secret isolation. `release/signing_policy.json` requires one signer, v3, package `com.instagram.android`, and the recorded release certificate.

### Install and device validation

Use the SDK ADB path when it is not on `PATH`:

```bash
ADB="$ANDROID_SDK_ROOT/platform-tools/adb"
"$ADB" devices -l
"$ADB" install <signed.apk>
```

Use `install -r` only when the installed and candidate signer match and preserving data is intentional. Uninstalling an existing package destroys app data and must be explicitly approved.

The provenance-bound runner requires a fresh artifact directory and declared state:

```bash
.venv/bin/python tools/device_validation/runner.py \
  --adb "$ANDROID_SDK_ROOT/platform-tools/adb" \
  --serial <serial> \
  --contract dfinsta_source_430/behavior_contract.json \
  --artifact-dir <new-evidence-dir> \
  --run-id <unique-id> \
  --artifact-apk <signed.apk> \
  --artifact-commit "$(git rev-parse HEAD)" \
  --build-report <build-or-release-report.json> \
  --install-state clean_install \
  --data-state fresh \
  --account-state logged_out \
  --cache-state unknown \
  preflight
```

Run separate fresh evidence directories for `startup`, `enter-settings`, `feature-state`, and diagnostic captures. Follow `behavior_contract.json`; settings are reached by Profile, polling for the top-right `Options` node with `long-clickable=true`, then long-pressing it. Restart the app after preference changes and record cache state.

### Safe cleanup

- Prefer a new output/evidence root to deleting old state. Refuse-overwrite is an evidence guarantee.
- `work/`, `.pipeline-state/`, APKs, DEX files, JAR, keystores, and Android build products are ignored. Never stage them without an explicit provenance decision.
- Do not use `git clean`, `git reset --hard`, or checkout-based cleanup: the current worktree contains extensive pre-existing user changes and generated evidence.
- For legacy 1.3, clean-extract before every release build. Repeated overlay builds can duplicate manifest components and retain deleted source files.

## 5. Testing and validation strategy

### Automated coverage

The `tests/` suite covers strict schema/hash decoding, Phase A Temporal History behavior, executor containment and cleanup, CAS publication, SQLite append-only authority and owner claims, source/decoded-tree capture, target-neutral compilation, 340/430 fixture generation, operation application, archive composition, framework/decode/apply/build/final-verification Activities, adoption/quarantine/cancellation paths, and final Smali/archive assertions.

Tool suites separately cover 340 source policy/rebuild invocation, 430 graft/archive/source policy, release policy/prerequisite parsing, and device-runner command/XML/assertion logic.

### Fresh results at audited baseline

| Command | 2026-07-31 result | Confidence / caveat |
|---|---|---|
| `.venv/bin/python -W error -m unittest discover -s tests -v` | second full run: 440 passed in 30.990s, 1 expected skip | high for current unit/integration-synthetic suite |
| same full command, first run | 440 run: 438 passed, 1 expected skip, 1 Temporal startup error after SDK server failed to accept TCP within 5s | low-confidence flake signal; must not be erased from history |
| focused Temporal persistence test | passed in 4.586s; SDK reported CLI 1.8.1 / Server 1.31.2 | supports transient environment failure, does not prove absence of flakiness |
| `.venv/bin/python -W error -m unittest discover -s tools/reconstruction/tests -v` | 15 passed | high |
| `.venv/bin/python -W error -m unittest discover -s tools/port_430/tests -v` | 19 passed | high |
| `.venv/bin/python -W error -m unittest discover -s tools/release/tests -v` | 6 passed | high; no real signing performed |
| `.venv/bin/python -W error -m unittest discover -s tools/device_validation/tests -v` | 22 passed | high for command/contract logic; no device connected |
| `.venv/bin/python -W error -m unittest discover -v` | 0 tests | invalid root-discovery command; always supply `-s tests` |

### Real APK evidence

| Target | Evidence | Result | Scope |
|---|---|---|---|
| 340 | `/home/arnav/AI/dfinsta-real-final-verify-340-3/success.json`, SHA-256 `de879d41f8bab0537ee85343e4f1d427c9c31a35b988ceb20291c268c8c4e1da`, source `a07c8d46d2504db41eeb94dfad755af8a8f270db` | final APK `998850606965a4b167d859469a35583da3a7756717b07cc94401ec33c8c55aa2`; verification receipt `c45ce3476bcc387d629ff96ed07487caad967506c104e188527f789bc5037f36`; 65 assertions/59 operation proofs | historical mechanical proof; generic `verifier.py` changed at `609bacf`, so 340 has not been rerun against current executable HEAD |
| 430 | `/home/arnav/AI/dfinsta-real-final-verify-430-2/success.json`, SHA-256 `9179519362038c0f410dacbfdffc670a8987447022f522c4eb17fd818b729a0e`, source `609bacfa35808947644cde904d2cd6b91d83f076` | final APK `c18ed84e091e40863f020ad4781e06bd9df10741b22af200445169c7412c3d27`; verification receipt `bedc94f9652b11bdcd768ff64c968733f921548bc3879c245c75868411b712d6`; 15 assertions/7 operation proofs | same scope |

Both evidence hashes were rechecked during this handover. Each report says adoption returned the same receipt, called the production receipt validator once, launched zero processes, and left no retry workspace. The 430 proof matches current executable code because only documentation changed after `609bacf`; the 340 proof predates the 430-driven generic verifier update. Current unit fixtures support 340 compatibility, but a new real 340 run is needed for an exact current-HEAD proof. This evidence is self-issued by the local harness; it is not authenticated production authority.

### Manual/device coverage and gaps

Historical device evidence supports startup, settings entry, five default switches, same-key update, and restart-bounded Feed/Explore/Reels/Stories observations. `dfinsta_source_430/behavior_contract.json` points to exact `work/device-runner/` evidence. Controlled contrasts remain incomplete because cache state and preference state were not always independently measured, and profile-ad absence is not proof without eligible inventory.

Minimum checks before a code commit:

1. Run focused tests for touched modules with `-W error`.
2. Run `.venv/bin/python -W error -m unittest discover -s tests -v` for contract/Activity changes.
3. Run the affected tool suite for patch/build/release/device changes.
4. Run `git diff --check` and review generated spec hashes when applicable.
5. For Smali/spec changes, require generator byte identity, apply fixture tests, final verifier tests, and a new real target run before claiming APK success.
6. For release/runtime claims, verify alignment/signature, installed base APK hash, process/crash state, settings selector, restart on both contrast sides, and declared cache protocol.

**Validation conclusion:** automated tests strongly cover deterministic contracts and fail-closed mechanics, but do not prove ART loadability, signing secrecy, authenticated human authority, or feature behavior.

- **Evidence:** test inventory above, behavior-contract `verified_contrast` fields, and documented proof boundaries in `docs/ADK_PIPELINE_PLAN.md`.
- **Confidence:** high.
- **Uncertainty / disproof:** a checked-in signed/device test or authenticated deployment test could close individual gaps; a current device failure could invalidate historical behavior assumptions.
- **Fresh review:** yes before release or behavior claims.

## 6. Current state and continuation point

### Dated Git snapshot

| Field | State at 2026-07-31 audit |
|---|---|
| Branch | `port-430` |
| Audited development HEAD | `b68256ec64f50eeaecd64ea4ee3e5cc026f55022` |
| Remote relation | `port-430` was 25 commits ahead of `origin/port-430`; those commits may not exist on another clone until pushed |
| Working tree | dirty before handover; no production file was changed by this handover |
| Tracked modifications | extensive pre-existing changes, all observed under `dfinsta_source_1.3/` including scripts, Smali, resources, website, and UI Automator files; provenance/purpose not established during this audit |
| Untracked | `TESTING-PLAYGROUND/` full decoded tree, `docs/FINDINGS.md`, `docs/adk_pipeline_design.md`, `docs/claude-opus.txt`, and `.$pipeline-flowchart.drawio.bkp` |
| Handover change | `HANDOVER.md` only |

Do not stage, revert, normalize line endings, or “clean up” those pre-existing paths. `TESTING-PLAYGROUND/instagram_340-src/` is generated evidence but is not covered by the root `.gitignore`, so accidental staging is possible. The untracked docs may contain useful research, but their lack of Git history lowers provenance confidence. This status was observed with `git status --short --branch`; confidence is high only for the audit instant, state may change at any time, and fresh review is mandatory before staging, reverting, or attributing any path.

### Confirmed milestones

| Milestone | Evidence | State |
|---|---|---|
| Maintainable privacy-hardened 340 source | `dfinsta_source_1.4.1/`, reconstruction docs/tests | complete |
| Minimal resource-free 430 port | `dfinsta_source_430/`, port docs/tests/device contract | complete for current approved feature subset |
| Phase A durable gate and executor foundation | commits/history through `docs/ADK_PIPELINE_PLAN.md`, Phase A tests | implemented; production deployment gaps remain |
| Target-neutral Phase B compiler/apply/backend/verifier | `pipeline_specs/`, `src/dfinsta_pipeline/`, 440-test suite | implemented |
| Ledger-owned framework/decode/apply/build/final verification | commits `7bc5e09` through `609bacf`, real evidence above | mechanically proven; ~~intentionally unregistered~~ **registered 2026-07-31** |
| Handoff/evidence documentation | `b68256e` plus this file | current as of audit |

### Exact continuation point

No production implementation was in progress when this handover was prepared. The next task is pending AgentMemory action `act_ms96b1ul_1fa4b8b2303f`: design, independently review, then register the proven replay chain in the durable Temporal Workflow and worker without changing existing receipt/Activity identities or Phase A History behavior.

Already attempted for this continuation:

- The five replay checkpoint Activities have Temporal metadata but tests explicitly assert exclusion from `worker.py` and `workflow.py`.
- The direct-Activity harness proved exact stage order and adoption for 340/430.
- A standalone replay CLI was previously designed, independently rejected, deleted, and never executed because it self-asserted capability hashes and bypassed ledger admission.
- No reviewed Workflow-registration implementation has been committed. Treat the orchestration design as fresh work, not a nearly finished patch.

Current blocker/uncertainty is design risk rather than a failing implementation: how to evolve the Workflow while preserving existing Phase A Histories, place two separate gate decisions in deterministic History, bind stage receipts compactly, choose Activity retry/timeouts/cancellation, and test replay across worker deployment versions.

Next concrete action: write a small design/test slice for the Workflow contract before registering any Activity. Define the new Workflow input/result schema and History compatibility strategy; add a failing test that proves the intended stage sequence and that old saved Phase A History still replays; obtain independent review; only then edit `workflow.py`/`worker.py`.

Priority order:

1. Workflow registration design and replay corpus/tests.
2. Authenticated trusted-client/actor authority and abrupt worker/process-loss evidence.
3. Signing/device-secret isolation and non-CAS publication fencing.
4. Controlled device contrasts with installed identity, preference state, restart success on both sides, and cache protocol.
5. Eligible profile-ad validation and future Instagram target resolution.

**Continuation conclusion:** Workflow registration is the correct next engineering milestone, but the exact design deserves fresh review.

- **Evidence:** `worker.py:34`, `workflow.py`, exclusion tests, `docs/SESSION_HANDOFF.md` Immediate Actions, AgentMemory action `act_ms96b1ul_1fa4b8b2303f`.
- **Confidence:** high that registration is absent; medium that it should precede every other open milestone.
- **Uncertainty / disproof:** a release/runtime priority change from the project owner, or discovery that registration requires unresolved authority redesign, could reorder work.
- **Fresh review:** yes, mandatory.

## 7. Decision history and project-specific lessons

| Decision | Motivation and alternatives | Evidence | Confidence / uncertainty / revisit |
|---|---|---|---|
| Use reconstructed 340, not legacy 1.3, as the port baseline | 1.3 whole-host overlays are brittle; 340 oracle provides a cleaner semantic delta | `AGENTS.md`, `docs/FINDINGS.md`, `docs/DFINSTA_1.4.1_DELTA.md` | high; revisit only if a newer trusted modified oracle provides better delta evidence; fresh review for new target |
| Separate target-neutral intent from target resolutions | One shared behavior contract must permit target-specific omission, strategy, and symbols | `pipeline_specs/intent_v2.json`, resolutions, `compile_port`, fixture tests | high; disprove with an intent that cannot be expressed without target literals in generic code |
| Use full rebuild for 340 and stock DEX graft for 430 | 340 resources reconstruct; 430 apktool resource rebuild is lossy | backend specs and archive-preservation assertions; `tools/port_430/README.md` | high for exact targets; revisit if a non-lossy 430 resource packager is independently proven |
| Keep 430 settings resource-free | Avoid fixed app IDs, manifest/resource changes, and lossy resource packaging | exactly four custom classes/source-policy tests; behavior contract | high; revisit only with byte-preserving resource evidence |
| Use URI blocking, not old response rewrite/Proxygen | Request blocking is robust and retained in 340 oracle; old response paths are dropped/inactive | intent forbidden fallbacks, hardened byte-absence assertions, delta docs | high; revisit if Instagram removes/relocates Tigon and a new verified request boundary is found |
| Use append-only ledger events plus current owner claims | At-least-once Activities need history and single-owner fencing; append-only history alone allowed concurrent interpretation | `ledger.py`, `docs/BLOG_NOTES.md`, concurrency/adoption tests | high within same-host SQLite model; fresh security review for distributed storage |
| Bind capabilities through recorded authority | Caller-supplied executable hashes and self-issued receipts do not prove admission | `AdmittedReplayV3`, AgentMemory lesson `lsn_e4b0b9e054c3d8ed` | high; revisit only with an equally strong external attestation model |
| Authorize final APK decode separately | Original replay capability admits only stock APK; reusing it would silently widen authority and invalidate receipts | `AdmittedReplayVerificationGrantV1`, commits `7bc5e09`/`23a8419`, grant tests | high; fresh review before merging gates or changing receipt versions |
| Preserve exact Linux decoded paths | Real APKs contain `AUX.smali` and case-distinct obfuscated names | lessons `lsn_6789430846a3a572`, `lsn_8cbbc9b1f4f59fd7`; decoded artifact tests | high on Linux; unresolved cross-platform execution remains |
| Normalize semantic Smali round trips, not raw labels/order | Baksmali renames/merges labels, removes unused labels, and reorders methods | commits `a07c8d4`, `609bacf`; failed and successful final runs | high for apktool 2.9.3; revisit on tool upgrade or changed switch/handler semantics |
| Keep signing/publication/runtime outside mechanical replay proof | Different authorities, secrets, side effects, and evidence are required | release finalizer, behavior contracts, ADK plan proof boundaries | high; do not collapse gates merely for convenience |

Durable engineering practice: make the smallest target-neutral change; encode every accepted behavior as a strict contract/test; run hostile/tamper/cancellation/adoption tests; request independent NO-GO/GO review before registering new side effects; preserve failed evidence roots; make one narrow commit after warning-strict tests; update `docs/SESSION_HANDOFF.md` and AgentMemory when a milestone changes.

## 8. Failed and abandoned approaches

| Objective | Attempt and observed failure | Evidence | Root cause / lesson | Assessment |
|---|---|---|---|---|
| Port 1.3 directly to newer Instagram | Whole obfuscated host classes and hard-coded fields/types would be copied forward | `AGENTS.md` Version-Porting Rules; `docs/FINDINGS.md`; 340 oracle delta | Incidental references are target-specific; apply normalized hook deltas to target hosts | high confidence invalid as default; fresh review each target; disprove with a proven symbol-identical target |
| Run replay from a standalone CLI | Prototype accepted caller-supplied tool hashes and self-issued decode receipts; review returned NO-GO and code was deleted before execution | `docs/SESSION_HANDOFF.md` replay rejection; lesson `lsn_e4b0b9e054c3d8ed`; no CLI exists in current tree | Hash equality is not admission; execution must be ledger-owned through admitted capabilities | high confidence under current authority model; fresh review required for any alternate entry point; external attestation could justify revisit |
| Rebuild all 430 resources/manifest with apktool | Sparse-resource warnings and diagnosed data loss | `tools/port_430/README.md`; archive-preservation assertions; 430 decode warnings in real evidence | Preserve stock archive and graft only changed DEX | high for apktool 2.9.3/current APK; fresh review on packager upgrade; retry only with byte/semantic preservation proof |
| Enforce Windows reserved-device rules on decoded trees | Real 340 decode contained `AUX.smali` | lessons `lsn_6789430846a3a572`; decoded-artifact tests | Exact Linux apktool outputs are not portable authored bundles | high for canonical Linux decode; fresh review for cross-platform worker design |
| Enforce case-insensitive uniqueness on decoded trees | Hundreds of legitimate case-distinct obfuscated Smali paths failed | lesson `lsn_8cbbc9b1f4f59fd7`; `test_case_distinct_paths_roundtrip_exactly` | Preserve exact Linux names; descriptor resolution handles path ambiguity | high; authored source remains casefold-strict; revisit only with proven reversible mapping |
| Assume apktool build is output-only | Real 340 probe exited zero but generated `framework/1.apk` and 15,218 `patched-tree/build` entries; operation quarantined | `/home/arnav/AI/dfinsta-real-build-probe-340-1/failure.json`; commit `d8d0187`; lesson `lsn_f898366487451d25` | Admit and clean only observed scratch prefixes, then revalidate predecessor | high for observed 2.9.3 behavior; fresh review/tool run on upgrade |
| Require byte-identical metadata for newly added 430 DEX | `classes20.dex` graft failed because Python normalized flags `0x808` to `0` and attributes to `0600` | `/home/arnav/AI/dfinsta-real-build-430-1/failure.json`; commit `5388d80`; lesson `lsn_d496789eec6a905e` | Permit explicit added-ASCII-DEX normalization only | high for current writer; fresh review if Python/archive writer changes |
| Treat no-framework final decode cache as forbidden mutation | 340 final verifier failed: `No-framework verification mutated the framework directory` | `/home/arnav/AI/dfinsta-real-final-verify-340-1/failure.json`; commit `ee70d0b` | Apktool legitimately generates isolated `framework/1.apk`; safety-scan declared scratch cache | high for observed topology; unexpected cache content would disprove policy sufficiency |
| Compare final Smali labels/method order textually | 340 failed `340.overlay.custom-code.final`; 430 failed two endpoint and custom overlay assertions | failure roots `dfinsta-real-final-verify-340-2` and `dfinsta-real-final-verify-430-1`; commits `a07c8d4`, `609bacf` | Normalize labels to instruction targets and canonicalize complete methods | high for observed round trips; fresh review for changed handlers/switches/tool versions |
| Use no-op endpoint labels as 430 idempotence markers | Baksmali dropped unused synthetic labels | 430 failure root above; `dfinsta_source_430/patches/anchored_patches.json` comment markers | Comments survive source application without pretending to be control flow | high; comments are source-only markers, not final proof |
| Reuse 340 feed-cache internals in 430 | named 340 coordinator/database classes disappeared | `docs/PORT_430_MAPPING.md` “Feed lifecycle/cache” | Redesign against public behavior/API; do not guess obfuscated fields | medium-high, unfinished; fresh mapping evidence could reopen |
| Assert legacy `Password` on startup | Current first-run UI showed `Join Instagram` / `I already have a profile` | `docs/DEVICE_VALIDATION_1.4.1.md`; both behavior contracts | Assert process/crash plus versioned accepted anchor sets | high for observed screens; fresh device UI may add another accepted set |
| Use legacy Linux `build.sh` as equivalent to PowerShell | Script sources `~/.zshrc`, lacks shebang/aapt1 parity | `AGENTS.md`; `dfinsta_source_1.3/build.sh` | Prefer reconstructed Python tools or Windows PowerShell for legacy 1.3 | high for current script; retry only after explicit Linux repair/tests |
| Run root unittest discovery | `.venv/bin/python -W error -m unittest discover -v` found zero tests | fresh handover command result | Explicitly use `-s tests` | high and reproducible for current layout; no fresh review needed unless test packaging changes |

The first full handover test run also hit a Temporal ephemeral-server TCP startup timeout. Focused and second full runs passed. Root cause is not established; treat it as an environment-sensitive flake hypothesis, not a proven code defect. Repeated failure, server stderr, or a deterministic resource/port condition would disprove the transient hypothesis.

## 9. Git and change-management conventions

- Current work is on `port-430`; do not create/rewrite branches, rebase, amend, or force-push without explicit instruction.
- Commits are small, imperative, and milestone-scoped: examples `609bacf Normalize final 430 smali verification`, `a07c8d4 Normalize final smali control-flow labels`, `6bbe173 Extend real replay through final verification`.
- Before committing, inspect `git status`, `git diff`, `git diff --check`, and recent log; stage explicit paths only. Run focused tests, then warning-strict full tests for pipeline changes.
- Never stage the current broad `dfinsta_source_1.3/` modifications unless the owner identifies and approves their provenance. They predate this handover.
- Never commit APKs, DEX, keystores, `.pipeline-state/`, `work/`, generated decodes, Android build output, or `uv.lock` unless policy changes explicitly. `.gitignore` is authoritative for normal generated state.
- `pipeline_specs/` is tracked generated authority. Commit changes only with generator byte-identity tests and reviewed input/source changes.
- Preserve failed run roots and create a new initially absent root for retries. Evidence filenames (`success.json`/`failure.json`) and hashes are part of the audit trail.
- Experimental code should remain attempt-local or on an explicitly requested branch. Rejected prototypes are deleted before commit; do not leave a second unofficial execution path.
- Never use destructive reset/checkout/clean commands in this dirty worktree. Revert only your own isolated commit with normal Git after reviewing impact; do not erase evidence or unrelated user changes.

## 10. Agent and delegation workflow

Sub-agents have been useful for independent code review, threat-model review, fixture/spec audit, and narrow failure-root-cause analysis. Good parallel units are: contracts/ledger review; Activity cancellation/adoption review; Smali/target mapping; test-gap audit; and documentation/evidence reconciliation. One agent should remain integration owner and avoid duplicating delegated work.

| Material delegated review | Outcome and durable evidence | Confidence / fresh review |
|---|---|---|
| Standalone replay CLI authority review | NO-GO; design deleted; rationale encoded in `docs/SESSION_HANDOFF.md` and lesson `lsn_e4b0b9e054c3d8ed` | high that current architecture rejects it; original review transcript is not durable, so freshly review any replacement |
| Framework/decode/apply/build/final-verification slices | Historical GO statements recorded in `docs/SESSION_HANDOFF.md`; resulting invariants are encoded in commits/tests and real evidence | high for encoded behavior, medium for unretained reviewer rationale; fresh review required at Workflow composition boundary |
| Smali normalization fixes | GO reflected by commits `a07c8d4`/`609bacf`, targeted tests, and succeeding real runs | high for observed tool/targets; fresh review on target/tool change |

Historical agent identities and chat transcripts are not durable project evidence. The commits, tests, reports, and AgentMemory entries above are the retained outputs.

Use a skeptical reviewer after each authority or side-effect slice. Ask for findings ordered by severity and an explicit GO/NO-GO against stated invariants. Reconcile conflicts against executable contracts/tests and evidence; do not vote or merge incompatible suggestions. A reviewer conclusion is advisory until encoded by code/tests/docs.

Durable delegated output belongs in a commit, a report under the relevant evidence root, `docs/SESSION_HANDOFF.md`, or a precise AgentMemory entry. Chat-only conclusions are not authority.

Poor delegation patterns to avoid:

- asking an agent to “port the feature” without exact target, source authority, proof boundary, and no-whole-class rule;
- allowing multiple agents to edit the same contract/ledger file concurrently;
- accepting a security review that examines only happy paths and not retries, cancellation, tamper, owner claims, and publication;
- asking a reviewer to infer success from test names without running or reading evidence;
- treating generated decodes or old `CLAUDE.md` prose as maintained source.

## 11. MCP servers and knowledge sources

### MCP usage

| Server/capability | Relevant use | Limitations | Durable recording |
|---|---|---|---|
| AgentMemory | actions, lessons, decisions, cross-session evidence pointers | can become stale; current repository/tests override it; there are no session observations/profile/patterns in the current store | update action result and save a concise memory only after code/evidence is committed |
| `cua-driver` | available for host GUI automation if Android/desktop tooling requires it | no repository evidence in this audit depends on it; GUI observations are not reproducible proof by themselves | pair with screenshots/logs and update behavior contract/evidence path |
| `agent-browser` | available for browser research/docs | not needed for APK build or current evidence; web state is external and mutable | cite stable upstream URL/version in docs if used |
| MCP resource registry | inspected on 2026-07-31 | returned no resources or templates at that instant | none; mutable snapshot, re-query before reliance |

ADB plus `tools/device_validation/runner.py`, not a GUI MCP, is the authoritative device-validation mechanism because it binds installed APK identity and emits `evidence.json`.

### Important AgentMemory entries

| ID | Content | Current relationship |
|---|---|---|
| `act_ms96b1ul_1fa4b8b2303f` | pending Workflow registration task | current continuation action |
| `act_ms7wcg60_353da5c853e4` | completed final APK verifier checkpoint | matches repository and real evidence |
| `act_ms23c6l8_1130a2890a93` | completed broad Phase B engine action | was stale-active during audit; reconciled to done on 2026-07-31 |
| `mem_ms8zr7tp_d4fce75366e6` | canonical 340/430 final-verifier evidence summary | matches hashes rechecked in this audit |
| `lsn_e4b0b9e054c3d8ed` | no self-asserted replay/build capability | current architectural lesson |
| `lsn_6789430846a3a572` | preserve `AUX.smali` on Linux exact trees | current path-policy lesson |
| `lsn_8cbbc9b1f4f59fd7` | preserve case-distinct decoded paths | current path-policy lesson |
| `lsn_f898366487451d25` | apktool build mutation topology | current pinned-tool lesson |
| `lsn_d496789eec6a905e` | added DEX ZIP metadata normalization | current backend lesson |

Conflict found and reconciled: before this audit, AgentMemory still marked the broad Phase B engine action active with a July 26 result that predated implemented checkpoint Activities. It is now complete and replaced by the narrow registration action. On 2026-07-31 AgentMemory queries reported no sessions, profile, patterns, or synthesized insights. Confidence is high only for that query snapshot; memory is mutable, so re-run `memory_frontier`, `memory_next`, and relevant recall before relying on action status. Repository Git/tests remain primary if memory later conflicts.

### Knowledge authority index

| Subject | First authority | Secondary evidence | If disagreement occurs |
|---|---|---|---|
| Executable pipeline behavior | `src/dfinsta_pipeline/` plus passing tests | `docs/ADK_PIPELINE_PLAN.md` | code/tests win; update stale prose |
| Target intent/operations/assertions | `pipeline_specs/` generated by `tools/phase_b/generate_specs.py` | source patch manifests | regenerate/review; never hand-edit one side silently |
| 340 maintained patch | `dfinsta_source_1.4.1/` and source-policy tests | reconstruction/delta docs | maintained source wins over oracle-only prose |
| 430 maintained patch | `dfinsta_source_430/` and 430 tests | `docs/PORT_430_MAPPING.md` | source/tests win for implementation; mapping doc wins for historical rationale |
| Runtime behavior | versioned `behavior_contract.json` plus matching `work/device-runner/` evidence | device docs | do not infer from Smali alone |
| Current continuation | this handover, `docs/SESSION_HANDOFF.md`, Git, current AgentMemory action | ADK plan | Git/code plus newest dated evidence wins |
| Legacy 1.3 behavior | `AGENTS.md`, maintained patch directories, targeted source reads | `dfinsta_source_1.3/CLAUDE.md` | treat CLAUDE prose as stale unless verified |
| Research notes | tracked docs | untracked `docs/FINDINGS.md`, `docs/adk_pipeline_design.md`, `docs/claude-opus.txt` | verify and commit selectively; untracked notes are not durable authority |

## 12. Risks, unknowns, and open questions

| Risk/unknown | Impact and evidence | Confidence | Next investigation / disproof |
|---|---|---|---|
| Replay chain is not in Workflow/worker | no durable end-to-end orchestration; exclusion tests and `worker.py:34` | high | design History-compatible registration and replay tests; fresh review required |
| Trusted-client/actor is not authenticated as a separate OS principal | a self-issued approval is not production authority; ADK plan lists gap | high | run separate authenticated client/worker deployment and negative actor tests |
| Abrupt process death/descendant cleanup is unproven | direct child cleanup is tested, OS sandbox/process tree is not | high | inject hard worker loss around real subprocess, inspect descendants, add platform confinement |
| Non-CAS signing/publication fencing | duplicate or ambiguous external effects may not adopt safely | high | design separate single-owner publish/sign authority and crash tests |
| Signing key/device secret isolation | release credentials could leak or be over-broad | high | external secret provider/isolated signer; verify logs/History contain no secret |
| ART/Dalvik class loading not proven by static final verifier | structurally valid DEX may fail verifier/runtime | medium | install exact newly built artifact, launch, monitor `AndroidRuntime`, exercise hooked paths |
| Controlled feature contrasts incomplete | behavior could be cache/account dependent | high | runner evidence with exact installed hash, machine-read preference, restart both sides, declared cache protocol |
| Profile-ad runtime effect unverified | ad absence can be inventory absence | high | eligible account/inventory with request capture and enabled/disabled contrast |
| 430 has no safe cache invalidator | stale content weakens immediate contrast | high; `docs/PORT_430_MAPPING.md` | map public cache API or explicitly retain restart/cache caveat |
| 430 resource strategy depends on stock preservation | future features needing resources are constrained | high for current toolchain | independently prove non-lossy packaging before any resource/manifest change |
| Linux-only exact tree semantics | worker portability to Windows is unsafe | high | define a Linux worker requirement or build a proven escape/mapping layer; do not normalize names |
| Pinned tool behavior | apktool/Java/Python ZIP behavior can drift | high for apktool, medium for Java | hash tools and rerun real fixtures on upgrades |
| Temporal ephemeral server startup flake | CI may intermittently fail persistence test | low-to-medium; one failure followed by two passes | capture server stderr/resource state on recurrence; repeated failures disprove transient hypothesis |
| Dirty legacy 1.3 tree provenance unknown | accidental staging could mix unrelated changes into release | high that it is dirty, low on why | owner review or separate forensic diff; do not touch meanwhile |
| Untracked research docs are not durable | useful evidence can disappear or conflict | high | review provenance/content and commit only explicitly approved material |
| Released pre-workspace claim is not append-only history | claim index state cannot be reconstructed solely from events | high; documented in ADK plan | design a release event/schema migration before distributed recovery reliance |

Confirmed legacy defects include the stale `Password`-only UI test, legacy manifest references/dead artifacts described in `AGENTS.md`, and unsafe direct reuse of the 340 feed-cache cleaner for 430. Suspected defects should remain labeled hypotheses until reproduced.

## 13. New-agent onboarding path

1. Read `AGENTS.md`, this `HANDOVER.md`, `docs/SESSION_HANDOFF.md`, and the Phase B checkpoint in `docs/ADK_PIPELINE_PLAN.md`. Then read `pipeline_specs/intent_v2.json`, both target resolution headers/backends, `worker.py`, `workflow.py`, and Activity exclusion tests.
2. Confirm environment and state:

```bash
git branch --show-current
git rev-parse HEAD
git status --short --untracked-files=normal
.venv/bin/python --version
java -version
sha256sum apktool_2.9.3.jar \
  'apks/com.instagram.android_340.0.0.22.109-374010893_minAPI28(arm64-v8a)(nodpi).apk' \
  'apks/com.instagram.android_430.0.0.53.80-383611248_minAPI28(arm64-v8a)(360,400,420,480dpi).apk' \
  work/430-port/framework-res-api36.apk
```

Expected branch is `port-430`; Python must be 3.11-3.13; hashes must match section 3. Do not require a clean worktree.

3. Perform the small verification task:

```bash
PYTHONPATH=tests \
.venv/bin/python -W error -m unittest -v \
  test_phase_b_real_replay_harness.RealReplayHarnessFastTests \
  test_phase_b_verification_activity.ReplayFinalApkVerificationActivityTests.test_temporal_metadata_exists_but_worker_and_workflow_exclude_activity
```

Run it from the repository root. It should confirm exact fixture hashes/stage order and the current deliberate registration gap without running apktool.

4. Resume AgentMemory action `act_ms96b1ul_1fa4b8b2303f`. Start with a design and failing History/replay test, not edits to Activity internals. Preserve old Phase A History behavior and existing receipt identities.
5. Completion evidence for the resumed task must include: independently reviewed Workflow input/gate/stage design; worker registration of only reviewed Activities; deterministic old-History replay plus new-History replay tests; retry/cancellation/adoption tests at Workflow boundaries; warning-strict full suite; and an updated opt-in 340/430 real run showing the registered Workflow reaches the same final receipt without duplicate launches. Do not call the milestone complete based only on unit registration or a direct-Activity harness.

**Onboarding recommendation:** trust the narrow executable evidence, preserve proof boundaries, and freshly review Workflow evolution. The project is deliberately conservative because a falsely “successful” APK or duplicated side effect is more costly than a fail-closed run.

- **Evidence:** the fail-closed history and adopted-success artifacts throughout this document.
- **Confidence:** high as a safe operating principle.
- **Uncertainty / disproof:** project-owner priorities can change, but any relaxation should be explicit and supported by new tests/evidence.
- **Fresh review:** yes, at the start of the registration task.

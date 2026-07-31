# Session Handoff

Last updated: 2026-07-31

Start with [`HANDOVER.md`](../HANDOVER.md). It is the concise entry point and carries evidence caveats this document does not repeat, including the freshness boundary on the 340 final-verifier proof and a recorded Temporal ephemeral-server startup flake. This file is the detailed chronological record; earlier sections are point-in-time and are not rewritten when later work lands, so prefer the newest dated statement whenever two sections disagree.

## End Goal

Create a recurring, privacy-respecting release pipeline with Temporal as the durable outer orchestrator and Google ADK as a bounded reasoning layer. Each cycle acquires an approved future stock Instagram release, adapts it from the latest human-promoted DFInsta baseline, and runs hash-bound approval, deterministic build/static verification, authorized signing, and structured device validation. Acceptance promotes the exact reviewed bundle as the baseline for the next cycle.

Instagram 340 and 430 are proof fixtures only. They establish reconstruction, mapping, packaging, replay, and device oracles; they are not the final destination or permanently selected production baseline. Mechanical extraction, indexing, patching, building, and verification remain deterministic. Agents propose evidence-backed mappings and diagnosis but cannot approve, mutate, sign, publish, or promote their own output.

## User Working Preferences

- Make progress autonomously and do not stop between feasible tasks.
- Use subagents aggressively for bounded parallel research to preserve main context.
- Keep authoritative findings in repository Markdown/JSON; do not rely on conversation compaction.
- Commit progress as small, separable units with brief informal messages.
- Do not commit or modify unrelated dirty legacy files.

## Commits Created

- `3c8cf88 add reconstruction tools`
- `4db3c8c add 1.4.1 patch source`
- `fcf4533 write reconstruction notes`
- `8951bd9 note lazy profile menu`
- `e074910 record feature gaps`
- `8365b72 audit startup privacy`
- `5341178 audit old app residue`
- `fc777b0 add session handoff`
- `bb7ff4d add device contract runner`
- `806ec54 map first 430 hooks`
- `bcccf53 drop startup tracking` (branch `harden-1.4.1`)
- `8dcb161 refresh session handoff` (branch `harden-1.4.1`)
- `2d024e1 trim dead app residue` (branch `harden-1.4.1`)
- `122baa8 document hardened baseline` (branch `harden-1.4.1`)
- `f8cb5ff capture disabled feature state` (branch `harden-1.4.1`)
- `93ddffd add minimal 430 port` (branch `port-430`; initial resource-based prototype)
- `a128481 preserve stock 430 resources` (branch `port-430`; resource-free DEX-graft pivot)
- `94523c8 read settings title from contract` (branch `port-430`; device runner uses the selected contract's settings title)
- `44b9a26 attach 430 settings listener` (superseded 430 settings-host hypothesis)
- `a75e60d launch configured app activity` (contract launcher intent and foreground assertion)
- `cb63ded patch live 430 profile action` (current `ProfileActionBar` settings hook)
- `e617551 block 430 reels homecoming` (corrected stale URI path; insufficient alone because cached media remained)
- `47408db restore direct reels blocking` (ports the proven endpoint-emptying behavior to the central 430 Reels builder)
- `6eb2227 replace shopping with profile ads` (retires the misleading Shopping setting and exposes exact profile-ad blocking)
- `39668de support clean install startup` (supports logged-out `ModalActivity` and package-launcher fallback)
- `4d1e5c4 scope device validation evidence` (separates launch strategies and records device/artifact state)
- `6cc7b93 record validation harness identity` (hashes the exact runner and contract used for evidence)
- `94dd015 support semantic boolean selectors` (allows contract-scoped `long_clickable` matching)
- `d019450 remove ineffective profile retry` (keeps 430 recovery empty after proving the observed stall required restart)
- `99faab2 verify installed apk identity` (fails evidence before interaction when installed `base.apk` differs from the declared artifact)
- `2249342 verify final dex structure` (checks exact host methods/invocations and every retained stock payload entry)
- `3f6f2f9 verify signed apk certificate` (supports fail-closed apksigner verification and records signer identity)
- `309d08c decode stock in 430 build` (fresh stock decode and complete unsigned-build provenance)
- `7d51098 record clean 430 build` (records the exercised clean-stock build and report hashes)
- `efed004 verify other profile exclusion` (proves other-user Options remains non-long-clickable)
- `b5a1a85 tighten artifact verification` (binds exact overloads/registers and enforces an expected signer digest)
- `5440f73 record tightened verification` (records the final v7 verification evidence)
- `ca71c63 add reusable apk release finalizer` (adds public signing policy and generic release finalization)
- `1237de8 bind apk release provenance` (binds versioned prerequisite reports and hardens no-clobber publication)
- `6bbe173 Extend real replay through final verification` (separate verifier authority, evidence, and adoption harness)
- `ee70d0b Allow safe generated verification frameworks` (admits safety-scanned apktool scratch cache output)
- `a07c8d4 Normalize final smali control-flow labels` (semantic final-decode label verification)
- `609bacf Normalize final 430 smali verification` (removes no-op 430 anchors and verifies reordered methods)

Current branch: `port-430`. It is based on `master` commit `6f1efa7` and contains the implementation/validation commits listed above. The validated `harden-1.4.1` branch was previously fast-forwarded into `master` through `fa90270`.

## Golden Reconstruction State

`dfinsta_source_1.4.1/` is a maintainable, delta-driven patch source for Instagram `340.0.0.22.109` / DFInsta `1.4.1`.

Inventory:

- Nine DFInsta classes
- No bundled ACRA classes
- 91 new resource files
- Eight append-resource fragments
- Two changed values resources
- One manifest activity addition
- 23 direct Instagram host classes
- 30 endpoint records plus seven anchored operations

No complete Instagram host class is retained as patch source.

Rebuild:

```bash
python3 tools/reconstruction/rebuild.py \
  work/1.4.1-reconstruction/stock-340 \
  dfinsta_source_1.4.1 \
  apktool_2.9.3.jar \
  --work-tree <new-work-tree> \
  --output-apk <new-unsigned.apk>
```

Requirements: apktool `2.9.3`, aapt1, `python3`. The command refuses overwrites and runs the DEX contract.

Verified:

- End-to-end deterministic rebuild succeeds in about two minutes.
- All 11 DEX files are present.
- Required 1.4.1 symbols are present; dropped 1.3 response/Proxygen symbols are absent.
- Semantic oracle-vs-rebuild `res/values*` delta is zero.
- Five patcher unit tests pass.
- Patch manifests apply once and report fully already-applied on rerun.

Key APK:

- `work/1.4.1-reconstruction/dfinsta-1.4.1-reconstructed-test.apk`
- SHA-256 `e35f5c6f11898599b4b197b077d5ffb1c367e025c5ac247a5b64210ca3191f81`
- Android debug signer; APK Signature Scheme v3

## Device State

Historical behavior device: Pixel 9 (`tokay`), previously authorized over ADB. Active dedicated device: Nothing Phone 1 (`A063`/`Spacewar`), serial `P3227J000775`, Android 15/API 35, ARM64, 1080x2400.

On the Pixel, the historical 340 oracle APK was pulled and confirmed byte-identical to `apks/dfinsta_1_4_1.apk`, then uninstalled with explicit user approval because its signer differed. The reconstructed 340 build and early 430 grafts preserved that device's login/preferences state. The Nothing Phone validation is separate: stock 430 and v7 were clean-installed during comparison, and the user manually logged into v7.

Current installed state:

- Installed APK on Nothing Phone: `work/430-release-v2/dfinsta_430.0.0.53.80-release-candidate.apk`.
- SHA-256: `8006a9079eee1a127ef150cef05e3b5591bb8690448650543af532c89b7c0f19`.
- Release lineage: `work/430-release-v2/dfinsta_430.0.0.53.80-release-candidate.release.json`, SHA-256 `df680eb7a7bdd6adb4778f559aee61b8a2ea525b7b63886743f4fcc7429d4809`.
- Signed structural verification: `work/430-release-v2/dfinsta_430.0.0.53.80-release-candidate.verification.json`, SHA-256 `6a6f3fc738adee4b195e54717f3d83625e5d66ab8f827623984f0f33ff709afa`.
- Signature: APK Signature Scheme v3, DFInsta Release certificate SHA-256 `798cda9135bed36ff7d0e3ba8eaf021e883c7dfe55559186c9fdce480345e877`.
- Clean install, device-side `base.apk` identity, logged-out and logged-in startup, settings entry, five checked defaults, and same-key `adb install -r` update all pass.
- Package/version preflight passes for `com.instagram.android` / `430.0.0.53.80` (`383611248`).
- Package MAIN/LAUNCHER reaches foreground alias `com.instagram.android/.activity.MainTabActivity`; the runner requires a contract-approved foreground state and reports no AndroidRuntime fatal or resource crash.
- Profile `Options` appears immediately in 430. Long-press opens the framework dialog on the first attempt without a swipe.
- Production v7 shows Feed, Explore, Reels, Stories, and Profile ads in that order; all five defaults were observed checked after login.
- A normal Options tap still enters Instagram's stock options/settings surface.
- Feed, Explore, Stories, and Reels have same-device, restart-bounded v7 observations on the Nothing Phone. These are not yet strict verified contrasts: cache was unknown, feature state was declared rather than read from preferences, and only the enabled side has a dedicated successful restart record.
- All five privacy settings were restored checked and a final package-launch startup passed.
- Public other-user profile `cookwithhenry` exposes stock clickable `Options` with `long_clickable=false`; the DFInsta long-click action remains scoped to the current-user `LX/077N` model.
- Do not clear app data, uninstall, or otherwise destroy the preserved login/preferences state without explicit user approval.
- Do not enable Hardcore Mode if returning to a 340 build on this data; it is not reversible through the 340 UI.

## Confirmed 340 Device Contract

- Startup succeeds without filtered AndroidRuntime/ACRA fatal errors.
- Current logged-out anchors are `Join Instagram` and `I already have a profile`; legacy `Password` assertion is stale.
- One-time welcome dialog appears after login and its Settings action works.
- Settings activity is not exported.
- Settings route: Home > current-user Profile > ensure profile header is rendered > long-press top-right `Options`.
- The profile action bar is lazy. Home > Profile plus an up/down or downward swipe toward the header and polling may be required before `Options` appears.
- Five switch order: Feed, Explore, Reels, Shopping, Stories. Defaults are checked.
- Preference changes require process restart for clean behavior evaluation.
- Feed: enabled session loads posts; disabled plus restart removes posts.
- Explore: enabled plus restart shows grid; disabled shows search shell only.
- Reels: enabled plays content; disabled shows handled error and process remains alive.
- Stories: enabled plus restart showed three other unseen entries; disabled left only current user's own story. No story was opened.
- Enabled Reels can prevent UI Automator from reaching idle; use screenshot/process evidence.

Machine-readable selectors and outcomes: `dfinsta_source_1.4.1/behavior_contract.json`.

Detailed device record: `docs/DEVICE_VALIDATION_1.4.1.md`.

## Important Static Findings

### Shopping decision

Instagram 430 has no standalone Shopping tab. Commerce is distributed across merchant profiles, product tags, ads, collections, deep links, and seller tools, with Bloks identifiers often outside URI paths. The inherited rule was both ineffective and misleading. The user approved retiring `Disable Shopping`; production v7 removes its UI row and broad `minishop` rule.

### 430 profile ads

The live endpoint `profile_ads/get_profile_ads/` supplies sponsored media inserted into profile-related and contextual feeds. Production v7 exposes `Disable profile ads`, key `disable_adds`, checked by default, and blocks the exact URI suffix in Tigon before native transmission. Runtime contrast may remain inconclusive on accounts without eligible ad inventory.

### 340 profile ads

Hidden key `disable_adds` defaults true, has no UI, no listener, and no Tigon-specific rule. Two request builders substitute the endpoint with an empty string.

### Cache

Cache clearing runs only when a cached-feature switch changes to true. It is asynchronous, has no completion signal, deletes only direct files under two cache directories, and may silently skip the database path if the captured coordinator is null.

### Hardcore Mode

Defaults false. Once enabled, its listener blocks false changes for itself and the disable switches, so it is effectively irreversible through the UI. Recovery requires clearing/uninstalling app data. Test only on disposable state.

### 340 Lazy Options cause

DFInsta only replaces Instagram's existing long-click listener. Instagram reads `UserDetailFragment.A1b` before a valid app-bar offset callback, applies `LX/2QV.A0W(false)`, and hides the bar. `RefreshableAppBarLayoutBehavior.DHe()` is the only discovered writer that later updates visibility after scroll.

Preferred future fix: deliver the actual initial app-bar offset state after the fragment is registered with the behavior. Do not permanently force `A1b=true`. Full investigation and acceptance criteria: `docs/FUTURE_WORK.md`.

## Privacy Policy and Implementation

Amplitude is unconditionally started from `InstagramAppShell` and sends Android ID, event `dfinsta_start`, and version to `https://api2.amplitude.com/2/httpapi` using an embedded API key. No consent or opt-out exists. Recommendation: remove from hardened baseline and do not port to 430.

ACRA installs an uncaught-exception handler and prepares an external email to `bugs@distractionfreeapps.com` with model/brand/version/stack trace fields. No visible opt-out exists. Recommendation: remove inherited ACRA; add a maintained explicit opt-in flow later only if needed.

The user approved the recommended hardened policy on 2026-07-25: remove Amplitude, ACRA, and the proven-safe dead residue while deferring broad resource pruning.

Commit `bcccf53` implements the privacy portion:

- Removed the two Amplitude classes.
- Removed all 79 bundled ACRA classes.
- Removed the `ReportsCrashes` annotation patch and ACRA/Amplitude startup payload.
- Retained only `startapp.setContext()` after `Application.onCreate()`.
- Added hardened APK verification that rejects Amplitude, any `Lcom/acra/` descriptor, and `ReportsCrashes`; default oracle verification remains available.
- Reconstruction unit suite now has ten passing tests.

The final clean build and safe live regression passed.

- APK: `work/1.4.1-reconstruction/dfinsta-1.4.1-hardened-final.apk`
- Signed SHA-256: `61d7cf895c7f460faaf454f52ee2af3378e827a5f0cf20a886c3378a25ab1cd5`
- v3 signature verified.
- In-place install preserved login and preferences.
- Cold startup stayed alive with no fatal trace.
- Settings route succeeded through the lazy-menu recovery sequence.
- Exactly five switches remained checked.
- Non-mutating disabled-state capture passed for Home/feed+Stories, Explore, and Reels. Required Explore/Reels anchors passed; Home evidence showed own story and no feed-item anchors. Process stayed alive with no fatal trace.

Full historical audit: `docs/PRIVACY_1.4.1.md`.

## Cleanup Findings

Proven safe-removal boundary:

- Nonexistent `IconChoose` manifest activity
- Uninstantiated `Preference$1` and `dfinstagram$1`
- Follower calls after unconditional return
- Unreachable backup cases using absent helper
- Proven unused private members
- Three dead comment helper methods
- Suggested-post UI/cache residue with no 1.4.1 host hook

Large inherited resource groups are only statically unreferenced and require full settings traversal before pruning. Full conservative audit: `docs/CLEANUP_1.4.1.md`.

Commit `2d024e1` applies the safe dead-code boundary and adds source-policy tests. Broad resource pruning remains deferred.

## Phase A Pipeline State

Atomic commits from `3e91eb5` through `7498dfd` add, review, harden, and persistence-test the initial Python package and pin `temporalio==1.30.0`. `PortRunWorkflow` is explicitly `PINNED`; each production Worker requires an immutable `--build-id`, while synthetic pre-deployment tests target `phase-a-v1` with `PinnedVersioningOverride`.

The current Workflow runs admission, prepare, validated approval Update, decision recording, apply, and final result. Compact artifacts live in a filesystem CAS. SQLite stores decisions plus append-only operation events for pending, effect, completed, and quarantined states. Apply identity includes the canonical run, admission artifact, prepared artifact, and accepted decision hash.

The executor capability layer binds the request and capability to the admitted `RunSpec`, verifies executable SHA-256 before launch, exact argv placeholders, resolved workspace/cwd containment, input/output artifact kinds, a replacement environment built from admitted keys, declared mutation auditing, bounded direct-child timeout/cancellation cleanup (including cancellation during launch), and monolithic APK composition. It uses `asyncio.create_subprocess_exec` with no shell. These checks do not constitute an OS sandbox, eliminate pathname replacement races, or clean descendant processes; future tools must be immutable worker-owned binaries with platform confinement.

Phase A has 35 tests. They prove Worker replacement at a waiting gate, forced History reconstruction, explicit candidate-version routing, promotion of `phase-a-v1` to Current followed by a normal start without override, Temporal CLI 1.8.1 / Server 1.31.2 restart against one SQLite state file, fresh Worker and trusted-client connections after restart, stale/hash-mismatched/unauthorized/expiry-boundary Update rejection, same-ID Temporal Update retry, ledger decision-identity collision rejection, decision persistence before apply, non-retry-safe single-owner claims, retry-safe cross-execution fencing/reclaim, concurrent legacy ledger migration, synthetic post-effect adoption, cancellation quarantine, a three-day logical timeout boundary, compact synthetic History, successful replay, deliberate nondeterminism rejection, strict envelope/result decoding, complete hash invalidation, append-only event enforcement, admitted `RunSpec` capability binding, and executor denial/cleanup/late-launch supervision paths. The 97-test repository total recorded here was correct when this Phase A section was written and is historical; the current warning-strict total appears in the Phase B section below.

Not yet proven: a separately launched and authenticated trusted-client OS process, authenticated actor submission, abrupt Worker/process death around a real subprocess, destination fencing for non-idempotent effects, a checked-in replay corpus, OS-level tool confinement/process-tree cleanup, and later signing/device secret boundaries. Graceful persistent Temporal-server restart and fresh SDK connections are proven. No APK, Google ADK, signing, or device operation is part of Phase A.

## Phase B Pipeline State

Commits `1a30252` through `b45fc0d` implement the reviewed deterministic core and admitted execution bridge without changing Phase A History contracts:

- Strict target-independent intent and version-scoped resolution contracts, including exhaustive per-target implemented/omitted status with rationale.
- Generated 340 and 430 fixtures plus source manifests. The 340 fixture has 59 operations, including 45 exact method/register-scoped smali edits. The 430 fixture has its six proven host edits plus one four-file custom-code overlay.
- A pure compiler that binds hashes, intent coverage, backend topology, operation ordering, destination collisions, preservation policy, and generated operation postconditions without target constants.
- A target-neutral decoded-tree applier. Provisioned mini-tree tests apply all 59 340 operations and all seven 430 operations, then classify every second-run operation as already applied.
- Full-rebuild and stock-DEX-graft archive backends with no-clobber staging, exact DEX topology, signature stripping, and retained stock entry byte/order/metadata preservation.
- A receipt-bound verifier for operation proofs, descriptor placement, DEX strings, archive preservation, resources, manifest components, and backend policy. The decoded tree is accepted only through an output/tree/tool-capability receipt that the admitted executor and CAS must own.
- Immutable source and tool bytes plus exact producer lineage are bound into recorded replay authority. `ToolchainProfileV3` role plans bind tool identity, logical paths, timeout, and executor capability shape. Replay-v3 admission resolves and validates exact concrete executor capabilities only after the recorded gate decision and artifact relationships pass. The ledger records canonical admitted replay-v3 authority append-only and returns a normalized object for execution; durable Workflow registration remains pending.
- An unregistered decode Activity requires normalized ledger authority before all external access, creates descriptor-relative attempt-private inputs, projects the selected capability into an internal Phase A `RunSpec`, and invokes unchanged `execute()` with admitted arguments and timeout. It emits `ReplayDecodedTreeReceiptV1`, binding the exact input APK, profile, plan, capability, tool, request, canonical tree manifest, semantic hash, and operation key. Receipt, manifest, and every child blob are validated before effect recording and again during adoption.
- Decode output is opened relative to the still-pinned workspace descriptor and captured by descriptor, so replacing the workspace pathname cannot redirect the authoritative scan. Active decode claims reject takeover. A pre-workspace failure may release only its exact pending claim for a later owner; cancellation and failures after workspace creation quarantine. Release persists in the mutable current-claim index but is not represented in the append-only operation event stream.
- An unregistered tree apply Activity accepts only replay-v3 authority, derives the exact completed ledger-owned decode predecessor, materializes and revalidates its complete closure, stages source through schema-2 admission, compiles/applies the target-neutral specification, and emits `ReplayPatchedTreeReceiptV1`. The receipt binds reconstructable source-admission evidence, ordered operation results, the exact `ApplyReport` hash, and the complete patched-tree manifest/blob closure. Adoption validates both input and output closures without source/workspace access.
- Apply runs in a supervised worker thread so the Temporal event loop remains responsive. Repeated cancellation waits until mutation stops, preserves mutation diagnostics, then quarantines before capture/effect publication. The Activity remains intentionally absent from worker/workflow registration until the admitted replay sequence is complete.
- Framework-bearing profiles now require an aggregate install checkpoint. It invokes the exact admitted installer sequentially for each package ID, verifies each step adds only its declared cache entry and preserves prior bytes, captures the final cache closure, and emits `ReplayFrameworkCacheReceiptV1`. Framework-aware decode requires that completed predecessor and emits `ReplayDecodedTreeReceiptV2`; no-framework decode retains its existing V1 operation and receipt identity.
- The unregistered ledger-owned build checkpoint requires the exact completed framework/decode/apply chain, executes only the admitted build capability, supports full rebuild and stock DEX graft composition, validates final bytes independently, and emits `ReplayPatchedApkReceiptV1`. Its reviewed profile policy admits only real apktool's exact transient outputs: no-framework builds allow `framework/1.apk`, `intermediate.apk`, and `patched-tree/build`; framework-bearing builds allow only `intermediate.apk` and `patched-tree/build`. Preexisting output, unsafe nodes, identity replacement, cleanup failure, declared-framework mutation, and closure drift quarantine without an effect.
- Replay-v3 source staging uses a separate strict schema-2 report and concrete ledger authority before any source, attempt, or destination access. It preserves the committed V1 callback API while reusing its Linux descriptor-relative no-follow, no-replace, fsync, and read-only publication mechanics.
- CAS blobs now pin root identity, reject path/link/type/permission substitution, publish read-only content descriptor-relatively, and handle concurrent readers/writers without accepting persistent hardlinks. Decoded trees are canonical manifest-plus-blob closures with explicit directory topology, an exact Linux-safe path policy, verifier-compatible semantic hashes, bounded capture, strict closure loading, and exclusive materialization. Real apktool output proved that exact trees must admit Windows device names such as `AUX.smali` and hundreds of case-distinct obfuscated filenames; pipeline-controlled destination roots remain restricted, while source bundles and declared operation destinations remain casefold-collision-free.

The standalone replay CLI prototype was rejected and deleted before commit or execution. Caller-supplied tool hashes are not admission, and a self-issued decoder receipt is not capability provenance. Real replay must run as ledger-owned attempt Activities through admitted executor capabilities, verify source manifests, keep output attempt-local through final verification, then complete/adopt or quarantine before exclusive publication.

The initial secure source-staging primitive is deliberately restricted to Linux workers with descriptor-relative no-follow opens, symlink-safe removal, and working `renameat2(RENAME_NOREPLACE)` on the attempt filesystem. Native Windows staging remains a separate required backend; unsafe pathname fallbacks are rejected.

The opt-in direct-Activity integration harness at commit `aa42045` was independently reviewed GO. It labels its authority as self-issued mechanical test evidence, imports every artifact through a completed ledger operation, verifies every reachable producer and CAS object, asserts exact direct-child argv/cwd/completion, and proves clean second-run adoption. The first two real 340 attempts failed closed on `AUX.smali` and case-distinct obfuscated paths; reviewed commits `9ca9aed` and `58df5ad` corrected those invalid Windows-portability assumptions without weakening exact-path or source-bundle collision controls.

The third run completed both fixtures from clean APKs in 1,466.7 seconds. Target 340 produced decoded semantic hash `92844bc3e9fcebd9e5729383feb4fed826af8b234176756da8c478ad76548a17`, applied all 59 operations, and produced patched hash `13aa6bcd21bf0788217ba9abfab69b7b05c4066207093a058a38ca1b3fc6f40e`. Target 430 installed and captured API 36 framework hash `5993389fff69b07bc98b09e69791cdf9489148ea0cd5434de9d1985716484264`, produced decoded hash `e7dcc179a9a18326d86914f22a69c9c8c629ff44fc20e0edaf7de9304c45ec0d`, applied all seven operations, and produced patched hash `0e457e9e14b3c01a94a007a2e635f37766d9f451538ef11e0267b84b20ffe8f8`. Canonical evidence is `/home/arnav/AI/dfinsta-real-replay-340-430-retry-2/success.json`, SHA-256 `840729530a3734d68e89c26c7dddf95f3911b65b427ead5a5bd7caae1aebf925`, generated from commit `58df5ad07ec2989342aeefed59d33e06319820d5`. The 9.1 GB evidence root remains mechanical direct-Activity evidence, not authenticated production authority, final APK build evidence, or runtime Instagram behavior proof.

The first real 340 build probe at `/home/arnav/AI/dfinsta-real-build-probe-340-1/failure.json` ran apktool to exit code zero and then quarantined because its generated `framework/1.apk` and 15,218 `patched-tree/build` entries were not admitted. No authoritative patched-tree file changed. The now-reviewed exact mutation policy and descriptor-relative cleanup address only those observed side effects; they do not weaken canonical closure verification or claim real build success. Focused warning-strict verification passes 175 tests, and full discovery passes 391 tests with one expected opt-in skip.

The approved 340 retry from commit `d8d018749f5b501cafda09b0c383ca4f9bcf4342` succeeded in 932.0 seconds. Canonical evidence is `/home/arnav/AI/dfinsta-real-build-retry-340-2/success.json`, SHA-256 `804299c3f94ad28e1e53fee02acc74138ced32412366a3e03138900a4c6cc100`, in a 5.5 GB evidence root. Decode/apply/build claims completed on attempt one; all 59 operations applied; the build receipt is `ba41ded768688ea7604a9a88c8d55431fdac40f72543195c289106e862ff73eb`. The full-rebuild intermediate/final APK is SHA-256 `7f84ee75c8d09ab0d18c91a1e1f152d71be1377c623eba2bb64422b6a88f6f35`, 87,860,247 bytes, with 11 DEX entries. Decode/apply/build adoption returned the same receipts with zero new launches and absent retry workspaces. Independent audit found all 129 reachable producer pairs asserted and completed. This remains self-issued mechanical evidence, not signing, final re-decode/static verification, publication, authenticated authority, or runtime behavior proof.

The first approved 430 attempt from commit `1950343` completed framework/decode/apply and apktool build, then failed closed during graft validation on `classes20.dex` metadata. Failure marker `/home/arnav/AI/dfinsta-real-build-430-1/failure.json` has SHA-256 `9f6a0fb9fcd6268a46d73f4c4790d43c8f0c818d8f0e15be324ad9528f3a865f`; build operation `f17975cffcced3fdabb78951709c3193008b2d2dc57444e91299ef9d13b9adec` quarantined with no effect. Apktool emitted the new ASCII DEX with data-descriptor/UTF flags `0x808` and zero external attributes; Python's seekable ZIP writer correctly canonicalized them to flags `0` and attributes `0600`. The reviewed fix permits only that explicit added-DEX normalization, validates local and central flags, and leaves retained/replacement metadata exact against stock. Focused tests pass 62 and full warning-strict discovery passes 394 with one expected skip.

The fresh 430 replay from commit `5388d8062cb986fd2cb455082a9cbc720e0cc940` succeeded in 1,457.7 seconds. Canonical evidence is `/home/arnav/AI/dfinsta-real-build-430-2/success.json`, SHA-256 `ba16388d7876f5c207694f8fa4e49473d9bcf2f5c479f67d97770857b3fdda78`, in a 7.5 GB root. Framework/decode-v2/apply/build completed on attempt one; all seven operations applied; build receipt is `752994ff2e0a1652a071c804cec1df2e6e6f65f25c809a98d87413ed987205f5`. The final graft is SHA-256 `e2aac4af7c08f7e7d024a9bc477929c5cc4edc447e4d0f28777c4528b66643af`, 135,648,088 bytes, with exact 20-DEX topology, four replacements, one addition, 16,396 retained stock entries, and three stripped signatures. Independent audit verified all 27 reachable producers and `classes20.dex` metadata. All four second calls adopted the same receipts with zero launches and absent retry workspaces. This remains self-issued mechanical evidence, not final re-decode/static verification, signing, publication, authenticated authority, or runtime behavior.

Final verification authority is separate from replay-v3 so the canonical build receipts remain valid. `AdmittedReplayVerificationGrantV1` embeds the exact admitted replay/build receipt and a second hash-bound gate decision for a final-APK-only decoder capability; ledger recording requires the canonical completed build claim and append-only event history. The unregistered verifier checkpoint descriptor-pins the final APK, captures final-decoded and exact-source CAS closures, runs all compiled static assertions, and emits `ReplayFinalApkVerificationReceiptV1`. Adoption rematerializes immutable CAS closures, invokes the production receipt validator exactly once, launches no subprocess, and does not access repository source. The authority, Activity, harness, and final normalization changes received independent GO; 440 warning-strict tests pass with one expected skip.

The first real 340 verifier run failed closed because apktool legitimately generated an isolated `framework/1.apk`; the reviewed fix safety-scans that declared scratch cache without treating it as framework authority. The second failed closed because baksmali renamed/merged control-flow labels and dropped unused aliases; method-scoped target-position normalization accepts those semantic round trips while rejecting changed branch targets. The fresh run from commit `a07c8d46d2504db41eeb94dfad755af8a8f270db` succeeded in 1,619.6 seconds. Canonical evidence `/home/arnav/AI/dfinsta-real-final-verify-340-3/success.json` has SHA-256 `de879d41f8bab0537ee85343e4f1d427c9c31a35b988ceb20291c268c8c4e1da`. Final APK SHA-256 is `998850606965a4b167d859469a35583da3a7756717b07cc94401ec33c8c55aa2`; verification receipt SHA-256 is `c45ce3476bcc387d629ff96ed07487caad967506c104e188527f789bc5037f36`; all 65 assertions and 59 operation proofs passed. Adoption returned the same artifact, called the production validator once, launched zero processes, and left no retry workspace.

The first real 430 verifier run failed closed on two no-op synthetic endpoint labels removed by baksmali and `SettingsWrapper` method reordering. The reviewed target-specific fix replaced those source idempotence labels with comment markers, regenerated all source/resolution hashes, and canonicalized complete smali methods before sorting them by declaration. The fresh run from commit `609bacfa35808947644cde904d2cd6b91d83f076` succeeded in 2,448.3 seconds. Canonical evidence `/home/arnav/AI/dfinsta-real-final-verify-430-2/success.json` has SHA-256 `9179519362038c0f410dacbfdffc670a8987447022f522c4eb17fd818b729a0e`. Final APK SHA-256 is `c18ed84e091e40863f020ad4781e06bd9df10741b22af200445169c7412c3d27`; verification receipt SHA-256 is `bedc94f9652b11bdcd768ff64c968733f921548bc3879c245c75868411b712d6`; all 15 assertions and seven operation proofs passed. Adoption again used one production-validator call, zero launches, the same artifact, and no retry workspace. Both evidence files include exact decisions, replay-v3 authority, separate verification grants, operation claims/events, referenced producer claims, and manifest CAS children. They remain self-issued mechanical direct-Activity evidence, not authenticated production authority, signing, publication, or runtime behavior.

## Workflow Registration Checkpoint

Registration is implemented as of 2026-07-31 on `port-430`, commits `b6feddf` through `aca304f`. Suite 440 to 498 tests, one expected skip, three consecutive green runs. Design and remaining follow-ups are in [`docs/WORKFLOW_REGISTRATION_DESIGN.md`](WORKFLOW_REGISTRATION_DESIGN.md).

`ReplayRunWorkflow` is a separate definition; `PortRunWorkflow` and `workflow.py` are byte-identical and hash-pinned by test. Only wrappers are registered: the five proven checkpoint Activities keep their signatures and bodies and are never registered directly, so their commit-bound real-run evidence stays valid. Wrappers take a 131-byte hash-pinned handle against the 102,066 bytes of specs an `AdmittedReplayV3` embeds for 340, and load the same authority from the ledger, which re-validates. Gate two is derived from recorded state by `replay_gate` after the build, independently by both the preparing and the admitting Activity, so no gate subject is ever taken from Workflow state.

Three defects were found and fixed during this work, all pre-existing or self-inflicted rather than inherent to registration:

- The Phase A History privacy assertions could never fail. `to_json()` base64-encodes payload bodies, so a plaintext search cannot see inside a payload. Proven against the stored fixture: `subject_sha256` and `run_id` are absent from the raw text yet present in five of eleven decoded blobs. Fixed in `449ec6f` with `tests/history_search.py` and mandatory positive controls.
- The "old History still replays" guarantee was untested; the test regenerated in-process the history it replayed. A stored fixture now exists and is verified to reject a realistic additive change, not only an empty stub.
- Commit `a2da2c5` regressed 15 ledger authority calls to the shadowable bound form by snapshotting a file mid-write; restored in `cb227e2` and confirmed at parity with the `130de49` baseline.

Not yet done: an opt-in real 340/430 run through the registered Workflow. Until that exists, registration is proven by unit and time-skipping tests only.

## Immediate Next Actions

1. Run the opt-in real 340/430 replay through the registered Workflow and confirm it reaches the same final receipts with no duplicate launches. Bundle follow-ups F1 and F2 from the design document into that slice: align the harness with `replay_gate.derive_verification_request`, and extract a public predecessor seam so `replay_gate` stops reaching into private `activities` helpers.
2. Complete Phase A authority/deployment evidence with an authenticated trusted-client process, hard process-loss test, and saved replay corpus. Normal Current-version rollout is proven.
3. Keep signing, publication, and runtime behavior behind their separate human gates; the new evidence proves only mechanical direct-Activity execution.
4. Add Google ADK as bounded read-only Temporal Activities only after deterministic generalization passes; specialist topology follows measured failure clusters.
5. Use strict 340/430 contrasts to validate fixture and device-executor behavior. Demonstrate promotion on an accepted future release and use that promoted bundle as the next rolling baseline.
6. Treat profile-ad runtime validation as inventory-dependent and non-blocking for pipeline implementation.

## Instagram 430 State

### Stock target

- APK: `apks/com.instagram.android_430.0.0.53.80-383611248_minAPI28(arm64-v8a)(360,400,420,480dpi).apk`.
- Version: `430.0.0.53.80`; version code `383611248`.
- SHA-256: `38ae9861b9ca89f60f41767324e1c3d54a4e3a00ed5555b92660a08e6db14754`.
- Decode: ignored `work/430-port/stock-430`, 28.36 seconds, 19 DEX trees, 179,190 descriptors.
- All old obfuscated 340 hook descriptors are gone. Stable named Tigon/startup/profile types mapped cleanly; endpoint occurrences expanded, mixed-media endpoint literals disappeared, and the feed-cache architecture changed completely.

### Build investigation and pivot

The first `93ddffd` prototype used a custom settings Activity, custom manifest entry, and custom resources. A stock apktool/aapt1 rebuild initially failed because the local framework lacked an Android API 36 attribute referenced by Instagram 430. Pulling the isolated API 36 `framework-res.apk` from the device and installing it into a dedicated apktool framework directory fixed the static build.

That successful build was not runtime-safe. Apktool's full resource decode/rebuild is lossy for this APK: the original string table contains IDs through `0x7f130231`, while the rebuilt table ends at `0x7f130220`. Existing stock code still requests IDs above that rebuilt range. The first signed, installed resource-rebuilt test was `work/430-build/dfinsta_430-test.apk`, SHA-256 `eb55f232cb6e59f4749f208b2a1123393090c42a61e0b10e9ae60fc7b80e6f5c`; it crashed during AndroidX Startup because resource `0x7f130227` was missing. The API 36 framework solved aapt1 compilation only, not the lossy resource round trip.

Commit `a128481` therefore pivoted to a resource-free build. The maintained 430 patch surface is exactly four custom classes:

- `com.dfinstagram.startapp`: stores the application context.
- `com.dfinstagram.dfinstagram`: reads the existing `com.instagram` shared preferences and returns endpoint-blocking decisions.
- `com.dfinstagram.hooks`: applies the five Tigon URI-path rules and throws `IOException` for blocked requests.
- `com.dfinstagram.SettingsWrapper`: replaces the existing profile Options long-click listener and renders a framework `AlertDialog` with five checked-by-default choices.

There is no custom Activity, manifest component, resource file, or application resource ID. Dialog text and choice labels are literals in custom DEX code. Preference changes are immediate but require an explicit process restart to evaluate behavior cleanly.

The build still uses apktool 2.9.3/aapt1 plus the isolated API 36 framework to assemble changed DEX files in an intermediate APK. It then grafts exactly `classes.dex`, `classes3.dex`, `classes4.dex`, `classes6.dex`, and new `classes20.dex` into the exact stock APK. Stock signing artifacts are removed; every other stock ZIP entry is copied. In particular, the binary `AndroidManifest.xml`, `resources.arsc`, and every `res/` entry come from stock unchanged. The five grafts correspond to the Tigon host hook, app-start context hook, direct Reels endpoints, profile settings hook, and four custom classes respectively.

### Static result

- Artifact: `work/430-graft-v7/dfinsta_430-graft-v7-test.apk`.
- SHA-256: `0aa8acf3a5bd97ad63dc5264b7fa9ddeeec373360c47f6e9ff6b37f8dc768fe4`.
- The static verifier passed the exact 20-DEX set (`classes.dex` through `classes20.dex`).
- It found exactly the four allowed custom descriptors and none of the forbidden legacy/privacy/resource-dependent symbols.
- It found the required host-hook structures: Tigon request blocking in `classes.dex`, context capture in `classes3.dex`, three direct Reels endpoint replacements in `classes4.dex`, and guarded settings installation in `classes6.dex`.
- Stock binary `AndroidManifest.xml` and `resources.arsc` are byte-identical.
- The complete set of `res/` names and every `res/` entry's bytes are identical to stock.
- Final signed verification disassembles the candidate and checks exact containing methods, invocation counts, all three Reels endpoint sequences, the guarded settings-listener sequence, and every retained non-signature stock payload entry.
- APK Signature Scheme v3 verifies; the signer is the Android Debug certificate, so release signing remains pending.
- Commit `309d08c` was exercised end to end from the stock APK into a fresh decode and unsigned graft. `work/430-clean-build/dfinsta_430-clean-unsigned.build.json` records all input/output hashes, six newly applied operations, and source commit; its SHA-256 is `8e458bee210995389a1fcab69ed6bf9ab404c643d9608459ce5d4dbba1204078`.

### Device result

- The grafted APK installed successfully on both the historical Pixel update path and the separate clean Nothing Phone validation path.
- Package/version preflight passes.
- Fail-closed installed identity passes: device `base.apk` SHA-256 equals the declared signed v7 APK.
- Stock 430's launcher is alias `com.instagram.android.activity.MainTabActivity` with MAIN/LAUNCHER; deprecated `LauncherActivity` is only a trampoline and was the cause of the earlier false startup diagnosis.
- The committed package-launch run reaches foreground alias `com.instagram.android/.activity.MainTabActivity` with a live process and no fatal or missing-resource crash.
- The live 430 Options view is built by `LX/077K` from self-profile model `LX/077N`, not by the legacy `LX/06X7` action builder.
- The guarded listener patch makes Options long-clickable; v7 opens the framework dialog on attempt one and shows all five current checked settings, including Profile ads.
- Normal Options click behavior is preserved.
- Current v7 Nothing observations: disabled Feed shows only own Story and no feed content; enabled shows other Stories and feed content.
- Current v7 Nothing observations: disabled Explore shows an empty search shell and refresh failure; enabled shows a populated grid.
- Current v7 Nothing observations: disabled Stories leaves only own Story; enabled shows other users' Stories.
- Current v7 Nothing observations: disabled Reels shows a handled error; enabled shows playable content with `Follow`. Cache was recorded as unknown.
- Historical v6 diagnostic Pixel evidence separately proved the live `clips/discover/stream/` path and exhausted retained Reels media after 50 swipes; do not attribute that cache-exhaustion procedure to v7.
- All five v7 settings were restored checked and final package-launch startup passed on the Nothing Phone.
- Other-user exclusion passed on public profile `cookwithhenry`: the exact action-bar identity was captured and its stock `Options` node was clickable but not long-clickable.
- On the clean Nothing Phone, both stock 430 and v7 explicit aliases initially self-finished to Launcher; Android's package-launcher path opened logged-out `ModalActivity`. The runner now accepts that contract-approved activity and captured `Join Instagram` / `I already have a profile`.

### Current limitations

- Implemented scope is application-context capture, exact Feed/Explore/Reels/Stories/profile-ad Tigon rules, direct central Reels endpoint blocking, and the framework settings dialog.
- Shopping is intentionally retired. No feed-cache clearing, welcome flow, custom resources, custom Activity, Amplitude, or ACRA are present. No lazy-profile repair is needed on 430 because Options renders immediately.
- Feed, Explore, Reels, and Stories have persuasive current v7/Nothing observations, but strict controlled verification remains pending; prior Pixel evidence remains separately scoped. Profile-ad transmission is statically covered but runtime inventory contrast remains pending.
- Full cache invalidation is intentionally not implemented because no safe cross-surface 430 API exists; cached content may remain temporarily after restart.
- The resource-free architecture intentionally cannot add ordinary Android resources or manifest components. Any future feature that requires them needs a proven non-lossy resource strategy, not a return to the known-broken full apktool resource rebuild.

Tracked first-pass mapping: `docs/PORT_430_MAPPING.md`.

## Worktree Warning

The repository contains many unrelated modified files under `dfinsta_source_1.3/` and untracked playground/pipeline artifacts. They predate or are unrelated to this reconstruction sequence. Do not stage, revert, normalize, or clean them without explicit user direction. Always stage explicit paths.

Known untracked unrelated paths include `TESTING-PLAYGROUND/`, pipeline diagrams, and older docs such as `docs/FINDINGS.md`/`docs/adk_pipeline_design.md`. The Phase A package and tests are tracked from commits `3e91eb5` and `ac4da5b`; do not confuse them with those unrelated artifacts.

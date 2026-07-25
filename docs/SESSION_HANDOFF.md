# Session Handoff

Last updated: 2026-07-25

## End Goal

Create a reproducible, privacy-respecting DFInsta porting system that takes a clean stock Instagram APK, applies small target-native patches, builds/signs it, and proves retained behavior with structured device evidence. Instagram 340 / DFInsta 1.4.1 is the golden reference; Instagram 430 is the first target.

Mechanical extraction, indexing, patching, building, and verification must remain deterministic. Agents should handle ambiguous class mapping, diagnosis, and explicit human gates rather than directly improvising the whole port.

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

Current branch: `port-430`. It is based on `master` commit `6f1efa7` and includes implementation commits through `47408db`. The validated `harden-1.4.1` branch was previously fast-forwarded into `master` through `fa90270`.

## Golden Reconstruction State

`dfinsta_source_1.4.1/` is a maintainable, delta-driven patch source for Instagram `340.0.0.22.109` / DFInsta `1.4.1`.

Inventory:

- 13 DFInsta classes
- 79 bundled ACRA classes
- 91 new resource files
- Eight append-resource fragments
- Two changed values resources
- Two oracle manifest additions
- 23 direct Instagram host classes
- 30 endpoint operations plus eight anchored operations

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

Device: Pixel 9 (`tokay`), authorized over ADB.

The historical 340 oracle APK was pulled and confirmed byte-identical to `apks/dfinsta_1_4_1.apk`, then uninstalled with explicit user approval because its signer differed. The reconstructed 340 debug build was installed and the user manually logged in. The signed 430 graft was subsequently installed as an in-place update, so the existing 340 login and preferences data were preserved rather than cleared. That old data may be incompatible with Instagram 430 and is a leading stock-comparison variable, not proof of a patch defect.

Current installed state:

- Installed APK: `work/430-graft-v5/dfinsta_430-graft-v5-test.apk`.
- SHA-256: `6185edd97aa17542390fd104a9dba6ec38dae43febed7dd555e217eccf08bb62`.
- Package/version preflight passes for `com.instagram.android` / `430.0.0.53.80` (`383611248`).
- Cold MAIN/LAUNCHER alias launch reaches `com.instagram.mainactivity.InstagramMainActivity`; the runner requires that foreground activity and reports no AndroidRuntime fatal or resource crash.
- Profile `Options` appears immediately in 430. Long-press opens the framework dialog on the first attempt without a swipe.
- The dialog contains exactly Feed, Explore, Reels, Stories, and Shopping, all checked from inherited 1.4.1 preferences.
- A normal Options tap still enters Instagram's stock options/settings surface.
- Feed, Explore, Stories, and Reels have restart-bounded enabled/disabled contrasts. Reels required exhausting a large retained media cache before the disabled endpoint behavior became visible.
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

### Shopping bug

The direct Shopping helper checks whether patched identifiers contain `minshop`, but all three contain `minishops`; these calls always preserve their input. Only a narrower Tigon URI-path rule containing `minishop` may block traffic. Shopping is not behavior-verified.

### Profile ads

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

## Immediate Next Actions

1. Decide and implement the Shopping policy. Current URI matching is partial and does not cover Bloks identifiers outside URI paths; do not claim Shopping parity yet.
2. Decide whether hidden profile-ad blocking remains part of the retained contract, and port its two mapped request builders if retained.
3. Redesign cache invalidation for 430. The old coordinator classes are gone, and Reels demonstrated that a process restart alone does not invalidate retained content.
4. Verify an unrelated-user profile does not receive the DFInsta long-click action.
5. Add the proven 430 build/sign/install/device sequence to the durable orchestration layer without moving ambiguous mapping into deterministic scripts.
6. Keep `docs/SESSION_HANDOFF.md` and `docs/PORT_430_MAPPING.md` current at every checkpoint.

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

- Artifact: `work/430-graft-v5/dfinsta_430-graft-v5-test.apk`.
- SHA-256: `6185edd97aa17542390fd104a9dba6ec38dae43febed7dd555e217eccf08bb62`.
- The static verifier passed the exact 20-DEX set (`classes.dex` through `classes20.dex`).
- It found exactly the four allowed custom descriptors and none of the forbidden legacy/privacy/resource-dependent symbols.
- It found all four required host-hook markers: Tigon request blocking in `classes.dex`, context capture in `classes3.dex`, direct Reels endpoint replacement in `classes4.dex`, and settings installation in `classes6.dex`.
- Stock binary `AndroidManifest.xml` and `resources.arsc` are byte-identical.
- The complete set of `res/` names and every `res/` entry's bytes are identical to stock.

### Device result

- The grafted APK installed successfully as an update, preserving the 340 login and preference data.
- Package/version preflight passes.
- Stock 430's launcher is alias `com.instagram.android.activity.MainTabActivity` with MAIN/LAUNCHER; deprecated `LauncherActivity` is only a trampoline and was the cause of the earlier false startup diagnosis.
- The contract-driven alias launch reaches foreground `InstagramMainActivity` with no fatal or missing-resource crash.
- The live 430 Options view is built by `LX/077K` from self-profile model `LX/077N`, not by the legacy `LX/06X7` action builder.
- The guarded listener patch makes Options long-clickable; the framework dialog opens on attempt one and shows all five inherited checked settings.
- Normal Options click behavior is preserved.
- Feed: disabled shows only own Story and no feed content; enabled shows other Stories and feed content.
- Explore: disabled shows an empty search shell and refresh failure; enabled shows a populated grid.
- Stories: disabled leaves only own Story; enabled shows other users' Stories.
- Reels: the URI-only v4 still allowed cached playback. Production v5 also replaces the three central `LX/05t2` endpoint selections with empty strings when disabled. A temporary endpoint-only invocation tag proved the live `clips/discover/stream/` path; after 50 swipes exhausted retained media, disabled Reels stayed on a content-free skeleton. Re-enabling Reels after cache exhaustion immediately restored fresh playable content.
- All five settings were restored checked, the non-logging v5 APK was reinstalled, and final cold startup passed.

### Current limitations

- Implemented scope is application-context capture, five-family Tigon URI blocking, and the framework settings dialog only.
- No direct endpoint substitutions, profile-ad rule, feed-cache clearing, welcome flow, custom resources, custom Activity, Amplitude, or ACRA are present. No lazy-profile repair is needed on 430 because Options renders immediately.
- Shopping coverage is partial because Bloks identifiers transported outside URI paths are not covered.
- Settings, Feed, Explore, Reels, and Stories are behavior-validated. Shopping, profile ads, and cache invalidation remain incomplete.
- The resource-free architecture intentionally cannot add ordinary Android resources or manifest components. Any future feature that requires them needs a proven non-lossy resource strategy, not a return to the known-broken full apktool resource rebuild.

Tracked first-pass mapping: `docs/PORT_430_MAPPING.md`.

## Worktree Warning

The repository contains many unrelated modified files under `dfinsta_source_1.3/` and untracked playground/pipeline artifacts. They predate or are unrelated to this reconstruction sequence. Do not stage, revert, normalize, or clean them without explicit user direction. Always stage explicit paths.

Known untracked unrelated paths include `TESTING-PLAYGROUND/`, pipeline diagrams, and older docs such as `docs/FINDINGS.md`/`docs/adk_pipeline_design.md`.

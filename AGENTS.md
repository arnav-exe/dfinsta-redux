# AGENTS.md

## Scope and Goal

- `dfinsta_source_1.4.1/` is the reconstructed, delta-driven 340 patch source; `dfinsta_source_1.3/` is the legacy 300 source. The repository root also holds APK oracles, large decoded trees, and research notes.
- This is an apktool/smali patch for package `com.instagram.android`, not a Gradle Android app. The patch source targets Instagram `300.0.0.29.110` / DFInsta `1.3.0`.
- The maintainable DFInsta `1.4.1` source was reconstructed by diffing stock Instagram 340 against `apks/dfinsta_1_4_1.apk`, built, signed, installed and behavior-validated. That was the 340 baseline, and it did its job: **430, 439, 440 and 441 have all been ported since.** Do not port the brittle 1.3 patch to anything.
- Artifact coverage: 300 and 340 are holdout/oracle fixtures; **430, 439, 440 and 441 are ported**, with `dfinsta_source_430/`, `dfinsta_source_439/`, signed artifacts and committed device evidence under `manifest/runtime_evidence/`.
- `docs/FINDINGS.md` records a successful partial 300-to-340 dry run and is higher-value porting evidence than stale class-role prose in `dfinsta_source_1.3/CLAUDE.md`. The `autopatch/` scripts/artifacts named there are not present in this checkout.

## Where the project actually is — read this first

This file predates the pipeline and its Scope section above is four months old. **The current
authority is [`docs/ROADMAP.md`](docs/ROADMAP.md)**, then
[`docs/history/IMPLEMENTATION_STATE.md`](docs/history/IMPLEMENTATION_STATE.md), then
[`docs/BOOTSTRAP.md`](docs/BOOTSTRAP.md) for what a machine needs.

There is now an agentic porting pipeline under `src/dfinsta_pipeline/`: a hook manifest
(`manifest/hooks.json`), a driver that resolves, applies, builds and verifies, an append-only
evidence ledger, four registered Temporal workflows, two human decision gates, and a release-ready
expectation that fails when a hook is lost. Instagram 441 is the newest port — seven hooks
resolved mechanically, zero agent invocations, four of seven release-ready.

**The "Version-Porting Rules" section below is still correct and still mandatory.** So is
everything about not staging `dfinsta_source_1.3/`. The Scope and Goal bullets are historical.

## Build and Generated State

- Rebuild 1.4.1 from a clean stock-340 decode with `python3 tools/reconstruction/rebuild.py <stock-decode> dfinsta_source_1.4.1 apktool_2.9.3.jar --work-tree <new-work-tree> --output-apk <new-unsigned.apk>`. The command applies **37** idempotent host operations — 30 in `endpoint_replacements.json` plus 7 in `anchored_patches.json`, counted from the files — builds with aapt1, and verifies the DEX contract. *(Said 38 until 2026-08-08. `HANDOVER.md:121` counts 59/45 for a wider scope including resource and manifest edits; `docs/RECONSTRUCTION_1.4.1.md:52` says 30+7 and is right.)*
- Run patch commands from `dfinsta_source_1.3/`. On Windows: `./extract.ps1 -ApkPath <stock.apk>`, then `./build.ps1 -Version <label>`.
- Use apktool `2.9.3` and aapt1. `build.ps1` passes `--use-aapt1`; this was also required for the 340 dry run. It invokes `python`, `zipalign`, `apksigner`, and `adb` and expects `$env:USERPROFILE/.android/dfinsta-release-key.keystore`.
- `-Version` only names `dfinsta_<label>.apk`; it does not update the displayed version/base version in `newRes/values/istrings.xml`.
- Always clean-extract before a release build. Repeated builds overlay files and `append_to_manifest.py` can duplicate activity declarations; deleted source files can also remain in `instagram_source/`.
- Treat `instagram_source/` and `TESTING-PLAYGROUND/*-src/` as generated evidence, never patch source. Edit `newCode/`, `overwriteCode/`, `newRes/`, `appendRes/`, or preprocessors instead.
- The preprocessing order is fixed: `remove_duplicate_style_tag.py`, `append_public.py`, `append_res.py`, `append_to_manifest.py`; then copy custom code to `smali_classes7`, overlay host patches/resources, and remove `assets/drawables.bin`.
- `append_public.py` uses `instander_settings` as an all-or-nothing sentinel; `append_res.py` uses each fragment's first entry as a sentinel. Neither repairs partial prior output, so regenerate after changing resource inputs.
- `build.ps1` signs before running plain `adb install` (no `-r`). A newly generated key can sign test builds but cannot update an app signed by a different key; uninstall the old package or use the same private key.
- The Bash path is not equivalent: `build.sh` sources `~/.zshrc`, omits `--use-aapt1`, and both `.sh` entry scripts lack shebangs. Prefer PowerShell unless deliberately fixing/validating Linux support.

## Patch Architecture

- `InstagramAppShell.onCreate()` calls `startapp.setContext()`; `startapp` is a static context/feed-cache holder, not an `Application` subclass.
- `hooks.throwIfBlocked(URI)` is the robust feature path. `TigonServiceLayer.startRequest()` calls it and converts its `IOException` to a failed request. Rules are path-based for feed, Explore, Reels, Stories, and Shopping.
- Suggested-post handling is a fragile Tigon `LX/1bI.onBody()` response rewrite. It is effectively always enabled because `disable_suggested_posts` defaults true but has no switch in `newRes/xml/instander_settings.xml`.
- Proxygen's `JniHandler` outgoing blocking is wired, but `nativeReadBufferRead`/`nativeReadBufferSize` have no callers; do not describe that response-rewrite path as active.
- Settings are inserted through patched `LX/5R8`/`LX/5RE` menu code and launch `com.dfinstagram.preference.Preference`. Cache capture/clearing is patched into `LX/2XJ` and depends on many obfuscated coordinator fields.
- `overwriteCode/` stores whole Instagram host classes even though the true 1.3 delta is only eight hosts and a handful of injected lines/fields. `newCode/` also contains hard-coded Instagram types, fields, constructors, and resource names; both surfaces require remapping.
- The manifest adds nonexistent `com.dfinstagram.IconChoose`; `PreferenceFragment` references absent backup/follower helper classes on hidden/dead branches. Do not assume every inherited Instander artifact is implemented.

## Version-Porting Rules

- Extract clean stock and modified APKs independently; never derive a delta from an already-overlaid `instagram_source/`.
- Port normalized hook deltas into the target version's own host classes. Never copy a complete old obfuscated class forward: incidental references number in the thousands and already have correct names in the target class.
- Normalize smali before diffing by stripping `.line` directives, comments, and blank lines. Raw diffs are dominated by disassembler noise.
- Locate a class by its in-file `.class` descriptor, then reuse apktool's exact target path. Case-colliding obfuscated names gain unstable `.N` filename suffixes on Windows, and classes move between `smali_classesN` directories.
- Prefer fingerprints in this order: stable named types/interfaces, stable strings/endpoints, superclass/structural shape, then numeric constants. Intersect multiple signals for ambiguous hosts; `FeedCacheCoordinator` plus `getRootActivity` uniquely mapped 300 `LX/2XJ` to 340 `LX/2XI`.
- The 340 oracle shows 1.4.1 dropped the old Tigon response rewrite and Proxygen subsystem, retained URL blocking/context/cache lifecycle hooks, and replaced endpoint constants across 19 hosts through `DistractionFree`. Settings use a long-click `SettingsWrapper`. See `docs/DFINSTA_1.4.1_DELTA.md`; do not assume feature parity with 1.3.
- Device evidence confirms feature changes require a process restart because Instagram caches can retain the prior state. Feed, Explore, and Reels have verified restart-bounded on/off contrasts. The user's own story remains when Stories are disabled.
- Settings are reached from the current-user profile by long-pressing top-right `Options`; the activity is not exported. The profile action bar can render lazily, so automation may need Home > Profile, a downward swipe toward the header, then polling for the long-clickable `Options` node.
- Searches under ignored decode/build directories must use `rg --no-ignore`/`-uu`; patterns beginning with `->` need `rg -e <pattern>` or `--`.

## Verification

- UI Automator must run from `dfinsta_source_1.3/ui-automator/`: `./run_test.sh`. For one method, use `./gradlew assembleDebug`, install `app/build/outputs/apk/debug/app-debug.apk`, then instrument `com.dfinstagram.startuptest.StartupTest#startMainActivityFromHomeScreen`.
- The legacy startup test requires visible text `Password`, but current Instagram 340 first-run UI shows `Join Instagram`/`I already have a profile`; that assertion is stale. It also checks only startup, not request blocking, settings, or cache clearing. See `docs/DEVICE_VALIDATION_1.4.1.md` for the observed behavioral contract.
- A successful apktool build proves assembly, not behavior. For a port, verify injected symbols in the rebuilt DEX and manually exercise every retained toggle plus feed-cache clearing on a device.

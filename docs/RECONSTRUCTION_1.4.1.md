# DFInsta 1.4.1 Reconstruction

## Goal

Recover a maintainable patch source for DFInsta 1.4.1 by comparing two APKs built on the same Instagram release:

- Stock: `apks/com.instagram.android_340.0.0.22.109-374010893_minAPI28(arm64-v8a)(nodpi).apk`
- Modified oracle: `apks/dfinsta_1_4_1.apk`

The result should describe the intentional 1.4.1 delta, not preserve complete modified Instagram classes. Once validated on Instagram 340, it becomes the baseline for the future Instagram 430 port and automation pipeline.

## Rules

- Decode both APKs independently with apktool 2.9.3.
- Keep generated trees and experiments under ignored `work/1.4.1-reconstruction/`.
- Compare classes by their in-file `.class` descriptors, not filenames or DEX directories.
- Normalize smali before textual diffs by removing `.line` directives, comments, and blank lines.
- Separate custom classes, host-class hook deltas, resources, manifest changes, and removed stock files.
- Never copy a complete modified host class into the reconstructed patch when a small anchored delta expresses the change.
- Treat build success as static validation only; retained features require device verification.

## Deliverables

- Clean stock and modified decode metadata with hashes and timings.
- A complete inventory of added, removed, and changed classes/resources/manifest entries.
- Normalized host-hook deltas and per-hook intent/fingerprints.
- Maintainable 1.4.1 patch source and clean build procedure.
- Static verification against the oracle and behavioral verification on an Android device.
- Inputs for the first version-independent hook manifest.

## Work Log

### 2026-07-25: Reconstruction started

- Confirmed that stock Instagram 340 and DFInsta 1.4.1 APKs are present.
- Confirmed apktool 2.9.3 is available as `apktool_2.9.3.jar`.
- Found Android SDK tools under `/home/arnav/Android/Sdk`: `platform-tools/adb` and build-tools 36.0.0 `zipalign`/`apksigner`.
- Chose fresh independent decodes over the retained stock decode so both comparison inputs are produced by the same command and tool version.
- Recorded SHA-256: apktool JAR `7956eb04194300ce0d0a84ad18771eebc94b89fb8d1ddcce8ea4c056818646f4`, stock APK `68f4546f8cb597a668d6033916200ef99191a9006350fcd986fd33392aea5113`, modified APK `0b7b858216d113019af4cf76d9db330ca583573c27dd59fff6cfaffbed7c7776`.
- Fresh Linux decodes completed successfully: stock in 31.79 seconds (about 1.23 GB peak RSS), modified in 33.70 seconds (about 941 MB peak RSS).
- The first inventory invocation exposed a portability issue: this Linux environment has `python3` but no `python`, while the legacy build scripts invoke `python`. Reconstruction tools use `python3` explicitly.
- Initial descriptor-based inventory found 123,190 stock classes and 123,282 modified classes: 92 added, none removed, and 188 changed after smali normalization.
- Of the 92 added classes, 13 are DFInsta classes and 79 belong to a bundled `com.acra` crash-reporting library. The strict source bootstrap caught and corrected an earlier hand-counted 12/80 split; generated inventories are authoritative. Dependency code remains distinct from DFInsta's own custom-code inventory.
- Bootstrapped `dfinsta_source_1.4.1/` with 13 custom classes, 79 third-party classes, 91 new resource files, eight append-resource fragments, two changed value entries, two manifest components, and 23 normalized oracle host diffs.
- Prepared an isolated stock-derived build tree and overlaid transitional exact versions of the 23 oracle hosts. This deliberately validates source completeness before replacing full-host overlays with the maintainable hook applier.
- A complete apktool 2.9.3/aapt1 build succeeded in 2 minutes 40.68 seconds (about 3.85 GB peak RSS). The unsigned APK is 84 MB, passes `unzip -t`, and has SHA-256 `861af997f079e8d93527251132637ff7222c781418bd449432b62e6c11cd4f1c`.
- `apksigner verify` correctly rejects this intermediate artifact because it is unsigned (`Missing META-INF/MANIFEST.MF`). Signing and device behavior remain separate verification stages.
- Found 23 Instagram host classes with direct DFInsta references. Nineteen replace endpoint string constants through the new `DistractionFree` helper; the remaining four cover Tigon URL blocking, application startup, feed-cache lifecycle/welcome UI, and settings entry.
- Zip-aligned and test-signed the reconstructed APK with the standard Android debug key. `apksigner` verifies v3 signing; test-signed SHA-256 is `ae1f2dc896b709dd013e2ca695e6710407542d850a326283080c2f84b335d1cc`.
- At this stage, `adb` was available but no device was connected; later entries below record completed installation and behavioral checks.
- Comparing oracle and delta-generated build trees semantically found zero added, removed, or changed `res/values*` entries.
- Replaced the complete oracle host overlays with 30 endpoint operations and seven significant-instruction anchored operations. Both manifests apply once and report fully already-applied on a second run.
- The delta-driven tree, containing target-native stock host classes plus only manifest operations, assembled successfully in 1 minute 56.47 seconds (about 3.17 GB peak RSS). Its unsigned SHA-256 is `a3f607d4ab2937eb7273cc2f295e1e13d92d3a42eef2a26f711dd15ac9a7a8d1` and it passes the same DEX contract as the oracle.
- Added five fast unit tests covering endpoint replacement/wrapping, significant-instruction matching across debug directives, idempotency, and anchor-count failure. All pass.
- Consolidated prepare, apply, apktool build, and DEX verification into `tools/reconstruction/rebuild.py`. It deliberately stops at an unsigned artifact and refuses to overwrite outputs.
- Executed the consolidated rebuild command successfully in 2 minutes 2.46 seconds (about 3.38 GB peak RSS), proving that the documented command works as written.
- Zip-aligned and debug-signed that reproducible output as `work/1.4.1-reconstruction/dfinsta-1.4.1-reconstructed-test.apk`. It verifies with APK Signature Scheme v3 and has SHA-256 `e35f5c6f11898599b4b197b077d5ffb1c367e025c5ac247a5b64210ca3191f81`.
- Connected a Pixel 9 and established that its installed package is byte-for-byte the historical DFInsta 1.4.1 oracle. The original and test signer certificates differ, so replacement requires an uninstall and explicit acceptance of app-data loss.
- Captured behavioral oracle evidence. Startup succeeds; the settings activity is not exported; the actual settings route is current-user Profile, then long-press top-right `Options`. Long-pressing the bottom Profile tab opens the stock account switcher instead.
- Confirmed the settings UI exposes five checked switches (feed, Explore, Reels, Shopping, Stories), plus donation, Hardcore Mode, and About. See `docs/DEVICE_VALIDATION_1.4.1.md` for the behavioral contract and pending tests.
- With explicit approval, uninstalled the historical oracle and installed the debug-signed reconstruction. It launches to the current first-run `Join Instagram` screen, remains alive, and emits no filtered AndroidRuntime/ACRA fatal startup error.
- The existing UI Automator startup check for visible text `Password` is stale; this installation shows `Get started` and `I already have a profile`. Future startup automation needs versioned accepted anchors plus a process/crash assertion.
- After manual login, the reconstructed welcome dialog appeared and its Settings action opened the correct activity. The independent Profile/Options long-press route also works.
- Confirmed preference persistence by disabling feed, restarting the process, reopening settings, observing only feed unchecked, then restoring it. The welcome dialog did not repeat.
- The profile action bar can render lazily: a small manual swipe may be needed before the top-right hamburger/`Options` node appears. UI automation must stimulate and poll for this condition rather than relying on a fixed delay.
- Confirmed that feature switches are restart-dependent because Instagram caches can preserve the prior active state. A feed post remained immediately after restoring the disable switch; after process restart, feed and Explore were empty and Reels showed a handled request-error screen while the app stayed alive.
- In the disabled state, the user's own story remains visible. Story suppression should therefore be asserted as absence of other tray entries, not absence of the tray itself.
- Confirmed restart-bounded on/off behavior for feed, Explore, and Reels. Explore switches between an empty search shell and populated grid; Reels switches between a handled error and playable content; feed posts disappear only after restoring disable and restarting.
- Active Reels can prevent UI Automator from reaching idle. Device automation needs a non-idle-compatible screenshot/process assertion for continuous media surfaces.
- Restored all five disable switches to checked and restarted the app after comparative testing.
- Added `dfinsta_source_1.4.1/behavior_contract.json` so confirmed selectors, restart requirements, feature contrasts, and pending tests are machine-readable inputs for future orchestration rather than facts an agent must infer from prose.
- Confirmed Stories contrast without opening any entry: enabling showed three other users' unseen stories; disabling left only the current user's own story. Restored all switches and restarted afterward.
- Static audit found that direct Shopping substitutions are no-ops because `minshop` does not match the patched `minishops` identifiers; only URI-path Tigon blocking may remain effective.
- Static audit confirmed Hardcore Mode is effectively irreversible through the UI and cache clearing is asynchronous, partial, and triggered only when a disable switch changes to true.
- `DistractionFree` does not rewrite responses. Its methods return the original endpoint string when a feature is enabled and an empty string when disabled; host patches replace the corresponding stock `const-string` instructions with these calls.
- The historical oracle initializes ACRA and posts an asynchronous `dfinsta_start` event containing Android ID and app version to Amplitude. The user approved removing both from the maintained baseline and future 430 port.

### 2026-07-25: Privacy hardening

- Removed the two Amplitude sender classes and all 79 bundled ACRA classes.
- Reduced the startup patch to `startapp.setContext()` only; removed the crash annotation and both reporting initializers.
- Added hardened DEX verification that rejects Amplitude, any `Lcom/acra/` descriptor, and `ReportsCrashes` while leaving default oracle verification available.
- Removed proven-safe dead residue: nonexistent `IconChoose`, two uninstantiated synthetic classes, unreachable follower/backup code, unused private members, dead comment helpers, suggested-post residue, and the newly orphaned crash-report string.
- Added source-policy tests that prove removed residue remains absent and active settings/donation resources remain present. Reconstruction tests increased from five to 15; all pass, along with nine device-runner tests.
- A clean build applied 30 endpoint and seven anchored operations, assembled all 11 DEX files, and passed the hardened contract.
- Zip-aligned and debug-signed `work/1.4.1-reconstruction/dfinsta-1.4.1-hardened-final.apk`. APK Signature Scheme v3 verifies; SHA-256 is `61d7cf895c7f460faaf454f52ee2af3378e827a5f0cf20a886c3378a25ab1cd5`.
- Installed the hardened APK in-place on the logged-in Pixel 9. Package/version preflight passed, cold startup stayed alive with no fatal trace, lazy profile settings entry succeeded on attempt two, and exactly five checked switches remained.

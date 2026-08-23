# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

DFInstagram (Distraction Free Instagram) is an Android APK patching tool. It decompiles the official Instagram APK using apktool, injects custom Dalvik bytecode (smali) and resources, then recompiles and re-signs the APK. The resulting app removes ads, suggested posts, Reels, Explore, Stories, and Shopping by intercepting Instagram's internal HTTP responses.

Currently built against Instagram v300.0.0.29.110.

## Prerequisites

- [apktool](https://ibotpeaches.github.io/Apktool/)
- Android SDK with `adb`, `zipalign`, and `apksigner`
- Python 3
- [jadx-gui](https://github.com/skylot/jadx) — for reading decompiled Instagram code as Java when updating smali

## Build Commands

**On Windows (PowerShell):**
```powershell
# One-time: decompile the Instagram APK (produces instagram_source/)
.\extract.ps1 -ApkPath Instagram_300.0.0.29.110.apk

# Build and install (pass a version label)
.\build.ps1 -Version 1.3

# Full rebuild from scratch
Remove-Item -Recurse -Force instagram_source; .\extract.ps1 -ApkPath Instagram_300.0.0.29.110.apk; .\build.ps1 -Version 1.3
```

**On macOS/Linux (bash):**
```bash
./extract.sh Instagram_300.0.0.29.110.apk
./build.sh 1.3
rm -rf instagram_source && ./extract.sh Instagram_300.0.0.29.110.apk && ./build.sh 1.3
```

Note: `build.sh` sources `~/.zshrc` on line 2; on Windows use `build.ps1` instead. Both scripts run the same Python preprocessing steps, copy files into `instagram_source/`, rebuild with apktool, align+sign with `~/.android/dfinsta-release-key.keystore` (Windows: `$env:USERPROFILE\.android\...`), and install via `adb install`.

## Running Tests

Requires a connected Android device with DFInstagram installed:

```bash
./ui-automator/run_test.sh
```

The single test (`StartupTest.kt`) launches the app and verifies the login screen appears.

## Architecture

### Build pipeline

| Step | Script | What it does |
|------|--------|--------------|
| 1 | `extract.sh` | `apktool d` → `instagram_source/` |
| 2 | `remove_duplicate_style_tag.py` | Deduplicates `<style>` entries in Instagram's `res/values/styles.xml` |
| 3 | `append_public.py` | Merges entries from `newPublic.txt` into `instagram_source/res/values/public.xml`, auto-assigning hex resource IDs |
| 4 | `append_res.py` | Appends entries from `appendRes/` into Instagram's `values/` and `values-night/` resource files |
| 5 | `append_to_manifest.py` | Inserts new `<activity>` declarations into `AndroidManifest.xml` |
| 6 | `cp newCode/ → smali_classes7/` | Copies custom smali classes into the decompiled source |
| 7 | `cp overwriteCode/ → instagram_source/` | Overwrites modified Instagram smali classes |
| 8 | `cp newRes/ → res/` | Copies custom layouts, drawables, fonts, colors |
| 9 | `apktool build` | Recompiles APK |
| 10 | `zipalign` + `apksigner` | Signs with release key |

`build.sh` also deletes `instagram_source/assets/drawables.bin` before building — apktool cannot handle this binary asset.

### Custom code (`newCode/com/dfinstagram/`)

The core logic. Key classes:

- **startapp.smali** — Application subclass; captures the `Context` into a static `ctx` field used everywhere else (e.g., `Lcom/dfinstagram/startapp;->ctx`). Also calls `hooks.handleStartActivity` on launch.
- **dfinstagram.smali** — Utility hub: `getBoolTrueEz`/`getBoolFalseEz` for reading `SharedPreferences` (namespace `com.instagram`), `getInstanderString` for looking up custom string resources by name, `startDfInstagramSettings` to launch the settings screen.
- **hooks.smali** — Main HTTP interception layer. Two distinct mechanisms:
  - `throwIfBlocked(URI)` — called on outgoing requests via the `JniHandler` hook. Throws `IOException` to abort the entire request if the URI matches a blocked feature (feed, explore, reels, stories, shopping) and the corresponding pref is enabled.
  - `maybeReadAndModifyResponse` / `modifyFeedResponse` — called when reading `NativeReadBuffer` responses. Buffers the full JSON body and rewrites `feed_items` to strip entries where `pagination_source == "feed_recs"` (suggested posts). Also strips `feed_recs` groups from `end_of_feed_demarcator`.
  - `modifyTigonBuffer` — same JSON surgery path but for the Tigon HTTP layer.
- **PreferenceFragment.smali** — Settings UI. Uses `SharedPreferences` (namespace `com.instagram`) to toggle each distraction. Toggling clears the feed cache via `FeedCacheCoordinator`. Preference keys: `disable_feed`, `disable_stories`, `disable_reels`, `disable_explore`, `disable_shopping`, `disable_suggested_posts`.
- **Preference.smali** / **SettingsActivity** — Activity shell hosting `PreferenceFragment`.
- **adv_settings.smali** / **dialog_maker.smali** — Advanced settings UI and dialog helper.

### Overwritten Instagram classes (`overwriteCode/`)

Modified copies of obfuscated Instagram smali files that add hook entry points:

- `smali_classes2/com/facebook/proxygen/NativeReadBuffer.smali` — Instagram's original class, extended with new fields (`modifiedResponse`, `modifiedResponseOffset`, `requestURI`, `incompleteResponse`) so `hooks.smali` can store per-request state on the buffer object.
- `smali_classes2/com/facebook/proxygen/JniHandler.smali` — Redirects `sendRequest`/`sendHeaders` to `hooks.jniHandlerSendRequest`, which stores the request URI onto the `NativeReadBuffer` so `throwIfBlocked` can check it later.
- `smali_classes2/X/5R8.smali` / `5RE.smali` — Obfuscated Proxygen classes that redirect `_read`/`_size` calls to `hooks.nativeReadBufferRead` / `hooks.nativeReadBufferSize`.
- `smali/com/instagram/api/tigon/TigonServiceLayer.smali` — Redirects Tigon (alternative HTTP layer) responses through `hooks.modifyTigonBuffer`.
- `smali/com/instagram/app/InstagramAppShell.smali` — Entry point patch to call `startapp` initialization.
- `smali/X/1bI.2.smali` / `2XJ.smali` — Obfuscated Instagram classes with hook call sites.

### Resources

- `newRes/` — New layouts (settings screen, etc.), drawables, colors, fonts
- `appendRes/values/` and `appendRes/values-night/` — Entries appended to Instagram's existing string/dimen/style resources
- `newPublic.txt` — New resource ID entries; input to `append_public.py`, which assigns unique hex IDs relative to Instagram's existing range

### Utility scripts (not part of build)

- `frsc_decoder.py` — Standalone decoder for Instagram's FRSC binary string resource format. Run it directly against a `.frsc` file to dump its string table; not invoked by `build.sh`.
- `normalResponse.json` / `suggestedResponse.json` — Sample API responses for manually testing the JSON filtering logic in `hooks.modifyFeedResponse`.

## Updating to a New Instagram Version

Instagram obfuscates class names per release, so every update requires manual smali work:

1. Decompile the new APK and use jadx-gui to find the new names for classes referenced in `overwriteCode/` and `newCode/`.
2. Update all class/method references in `overwriteCode/` smali files.
3. Update references in `newCode/` smali files.
4. Update `ExternalSyntheticLambda0` in `PreferenceFragment.smali` to point to the correct `FeedCacheCoordinator` method (the one calling `invoke-virtual {v0}, Ljava/util/AbstractCollection;->clear()V`).
5. Update the version string in `newRes/values/istrings.xml`.
6. Manually test that changing settings clears the feed cache.

## Smali Reference

Dalvik opcode reference: http://pallergabor.uw.hu/androidblog/dalvik_opcodes.html

# DFInsta 1.4.1 Oracle Delta

This document records the intentional differences recovered from fresh apktool 2.9.3 decodes of stock Instagram `340.0.0.22.109` and DFInsta `1.4.1`. Generated evidence lives under ignored `work/1.4.1-reconstruction/`.

## Inventory

- Stock classes: 123,190
- Modified classes: 123,282
- Added classes: 92
- Removed classes: 0
- Normalized changed classes: 188
- Direct DFInsta hook hosts: 23
- Added non-smali files: 93, including two signing files
- Removed non-smali files: `assets/drawables.bin` plus the stock RSA/SF signing files
- Added semantic entries in `res/values*`: 210
- Removed semantic entries in `res/values*`: 0
- Changed semantic entries in `res/values*`: 2

## Added Code

Thirteen classes implement DFInsta behavior:

- `AmplitudeEventsSender` and its synthetic lambda
- `DistractionFree`
- `SettingsWrapper`
- `dfinstagram` and its anonymous listener
- `dialog_maker`
- `hooks`
- `Preference`, `Preference$1`, `PreferenceFragment`, and its cache-clearing lambda
- `startapp`

The other 79 added classes are the bundled ACRA crash-reporting library under `com.acra`. ACRA is initialized from `InstagramAppShell`, reports crashes by email/toast, and must remain a separate third-party dependency in reconstructed source.

## Hook Families

### Endpoint substitution

Nineteen host classes replace stock endpoint `const-string` instructions with calls into `DistractionFree`. Each helper reads a preference that defaults to true and returns either the original endpoint or an empty string.

Recovered endpoint families:

| Feature | Helper behavior | Hosts |
|---|---|---|
| Feed | `feed/timeline/` or empty | `LX/15J`, `15R`, `186`, `1TP`, `2ao`, `N1b`, `Ni2`, `PIM` |
| Explore | `discover/topical_explore/` or empty | `LX/1TP`, `51R` |
| Reels | clips/mixed-media discover and stream endpoints or empty | `LX/2Wv`, `501`, `51R`, `G9h` |
| Stories | reels-tray and `_v1` endpoints or empty | `LX/1Gx`, `1TP`, `2Za`, `ContextualFeedFragment` |
| Shopping | preserve a string unless it contains `minshop`, then empty | `LX/51R`, `Oyz`, `Pzz` |
| Profile ads | `profile_ads/get_profile_ads/` or empty | `LX/GBY`, `GUk` |

`DistractionFree` also defines comment and Explore-stream helpers with no recovered host callers. They are present in the oracle but not active through a direct patch.

Profile-ad removal uses the misspelled key `disable_adds`, defaults true, and has no settings switch. It is effectively always enabled.

### Request blocking

`TigonServiceLayer.startRequest()` reads `LX/1Os.A09` (`URI`) and calls `hooks.throwIfBlocked()` inside the existing request failure try/catch. This retains the robust 1.3 path-based blocking mechanism.

### Application startup

`InstagramAppShell.onCreate()`:

- Captures the application context through `startapp.setContext()`.
- Initializes ACRA.
- Starts an asynchronous Amplitude event submission.

The Amplitude event sends Android ID, event name `dfinsta_start`, and the DFInsta version to `https://api2.amplitude.com/2/httpapi` using an embedded API key. This is oracle behavior, not a required distraction-blocking feature; retaining it requires an explicit decision.

### Settings entry

`LX/66Y` replaces Instagram's existing long-click listener with `SettingsWrapper`, which opens `com.dfinstagram.preference.Preference`. Device observation confirms the entry point is a long-press on the top-right `Options` control on the current user's profile. Long-pressing the bottom Profile tab instead opens Instagram's stock account switcher. The welcome dialog uses the same wrapper for its Settings button.

The preference activity is not exported, so `adb am start` cannot bypass this in-app entry path.

The UI exposes feed, Explore, Reels, Shopping, Stories, and hardcore-mode controls. It contains a suggested-post string and code key but no suggested-post switch or active rewrite hook.

### Feed-cache lifecycle

`LX/2XI` captures `FeedCacheCoordinator`, clears the static reference in `onDestroy()`, and shows the one-time welcome dialog. The preference cache clearer directly accesses 340-specific coordinator, flash-cache, session, database, and obfuscated fields.

## Manifest and Resources

- The manifest adds `com.dfinstagram.preference.Preference` and the still-nonexistent `com.dfinstagram.IconChoose`.
- Ninety-one added resource files provide settings layouts, drawables, fonts, colors, and strings; many are inherited Instander assets not used by the visible DFInsta UI.
- Existing values files gain arrays, colors, dimensions, IDs, styles, and public resource declarations.
- `EffectDarkMode` loses `igds_color_primary_background`.
- `InThreadComposerTextArea.android:textColorHint` changes from `?textColorTertiary` to `?igds_color_secondary_text`.
- `assets/drawables.bin` is removed, matching the apktool build workaround.

## Diff Noise Excluded from Patch Intent

The 165 changed classes without direct DFInsta references primarily contain DEX round-trip effects such as redundant catch directives disappearing and `const-string` becoming `const-string/jumbo`. These are not automatically part of patch intent.

Likewise, thousands of stock PNG hashes differ after APK rebuilding. Added files and semantic XML deltas are useful evidence; raw binary hash changes are not.

## Open Decisions

- Retain or remove Amplitude startup analytics.
- Retain or remove ACRA crash reporting and its email address.
- Preserve all inherited Instander assets or prune only after proving they are unreferenced.
- Preserve the nonexistent `IconChoose` declaration for oracle fidelity or remove it as dead residue.
- Decide whether profile-ad removal should remain forced or gain a visible setting.
- Decide whether suggested-post removal is intentionally absent in 1.4.1.

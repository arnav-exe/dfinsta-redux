# DFInsta 1.4.1 Device Validation

This document separates behavior observed from the historical DFInsta 1.4.1 oracle from behavior still to be proven in the reconstructed APK.

## Device and Oracle Identity

- Device: Google Pixel 9 (`tokay`), connected and authorized over ADB.
- Installed package: `com.instagram.android`.
- Installed version: `340.0.0.22.109`, version code `374010893`.
- Pulled installed APK SHA-256: `0b7b858216d113019af4cf76d9db330ca583573c27dd59fff6cfaffbed7c7776`.
- This exactly matches `apks/dfinsta_1_4_1.apk`.
- Oracle signer certificate SHA-256: `453e7b8ffc69cb76d8fe76279689efff614e3cd0ad23ffe9b76f66825c64e07d`.
- Oracle signer subject: `CN=hank scorpio, OU=Unknown, O=Unknown, L=Unknown, ST=Unknown, C=Unknown`.
- Reconstructed test signer is the Android debug certificate, SHA-256 `d36892747bf6bafc848f78939746bb856290f9d2cca50dd34adc0c7e133064f1`.

The reconstructed APK cannot update the oracle because the signatures differ. Uninstalling the oracle before installing the test build removes its app data and logged-in session.

## Confirmed Oracle Behavior

### Startup

`com.instagram.mainactivity.LauncherActivity` launches successfully into the logged-in home UI. The captured accessibility tree includes Home, Search and explore, Reels, Create, Activity, and Profile navigation.

### Settings entry

`com.dfinstagram.preference.Preference` is not exported. Direct ADB launch fails with `SecurityException: ... not exported`. Tests must enter through the in-app hook.

The confirmed route is:

1. Open the current user's profile with the bottom `Profile` tab (`com.instagram.android:id/profile_tab`).
2. Long-press the top-right control whose accessibility description is `Options`.
3. Assert that the activity displays the title `Distraction Free settings`.

On the observed profile UI, the top-right action bar and its `Options` accessibility node may not appear immediately after navigation. A small human swipe caused it to render. Automation must poll for a visible, long-clickable `Options` node and may perform a short profile-page swipe before retrying; it must not immediately fall back to stale coordinates.

Long-pressing the bottom `Profile` tab is not the DFInsta entry point; it opens Instagram's stock account switcher.

### Settings surface

The oracle displays five `android.widget.Switch` controls using resource ID `android:id/switch_widget`:

| Order | Label | Observed state |
|---|---|---|
| 1 | Disable feed | checked |
| 2 | Disable explore | checked |
| 3 | Disable reels | checked |
| 4 | Disable shopping | checked |
| 5 | Disable stories | checked |

The screen also displays:

- `Ways to donate`, summary `Bitcoin, Ethereum`
- `Hardcore Mode`
- `About`

There is no visible suggested-post or profile-ad switch. This matches the recovered resource XML and custom-code analysis.

## Reconstruction Results

The reproducible test APK is:

`work/1.4.1-reconstruction/dfinsta-1.4.1-reconstructed-test.apk`

- Zip-aligned.
- APK Signature Scheme v3 verified.
- Test APK SHA-256: `e35f5c6f11898599b4b197b077d5ffb1c367e025c5ac247a5b64210ca3191f81`.
- DEX contract passes.
- The historical oracle was uninstalled with explicit approval, removing its app data, and the reconstruction was installed successfully.
- Android reports the expected version code/name and debug signer.
- `LauncherActivity` starts successfully; the process remained live after ten seconds.
- No `AndroidRuntime` or ACRA fatal startup error appeared in the captured logcat filter.
- The logged-out UI rendered `Join Instagram`, `Get started`, `I already have a profile`, and `English (UK)`.
- After login, the one-time welcome dialog appeared with `Settings` and `Close` actions and the instruction to long-press the top-right three-bar button.
- The welcome dialog's `Settings` action opened the expected settings activity.
- The profile `Options` long-press route also opened settings after the delayed action bar rendered.
- Turning `Disable feed` off changed only its switch, persisted across a process restart, and the welcome dialog did not repeat.
- Reopening settings showed the persisted false state. The switch was restored to true, leaving all five switches in their original checked state.
- Setting changes require an application process restart before feature behavior is evaluated. Instagram session/feed caches can continue showing the prior state immediately after a switch changes.
- This was observed directly: after restoring `Disable feed` to checked without restarting, a post remained visible. After force-stopping and restarting with all switches checked, the feed had no posts.
- In the restarted all-disabled state, Home showed only the user's own story and navigation; Explore showed only `Ask Meta AI or search` and no discovery grid; Reels showed the handled error `We're sorry, but something went wrong. Please try again.` The process remained alive.
- `Disable stories` does not remove the current user's own story entry. No other story tray entries were visible in the captured disabled state.
- Feed behavior has an enabled/disabled contrast: the process session started with `Disable feed` unchecked and loaded a post; after restoring the switch and restarting, posts were absent.
- Explore behavior has a strong contrast: unchecked plus restart rendered a populated discovery grid; checked plus restart rendered only search chrome and no grid.
- Reels behavior has a strong contrast: unchecked plus restart rendered and played reel content; checked plus restart rendered a handled request-error screen. Neither state crashed the process.
- UI Automator may fail with `could not get idle state` while an enabled Reel is continuously rendering. Reels automation must support screenshot/process assertions or another non-idle-aware capture mechanism rather than requiring an idle hierarchy dump.
- Explore and Reels were restored to checked and the app was restarted after comparison testing. Final switch state was verified as all five true before restart.
- Stories has a confirmed safe contrast. With `Disable stories` unchecked and the process restarted, three other users' unseen tray entries appeared alongside the current user's story. With the switch checked and restarted, only the current user's story remained. No story was opened or marked seen.
- Stories was restored to checked; all five switches were verified true and the app restarted afterward.

The existing UI Automator test's `Password` assertion is stale for this first-run screen. Startup tests should assert the process and accept a small versioned set of known logged-out anchors rather than one historical screen string.

## Required Reconstruction Tests

1. Enter settings through profile `Options` long-press after login.
2. Assert the five switch labels, ordering, and default checked states.
3. Toggle each switch off/on, restart the app after each state change, and verify persistence plus behavior.
4. Verify Hardcore Mode only on disposable app data; static analysis shows it prevents disabling itself through the UI.
5. Exercise Shopping and profile-ad behavior independently; feed, Explore, Reels, and Stories have confirmed on/off contrasts.
6. Isolate feed-cache clearing from the already confirmed restart behavior.
7. Attribute handled request failures to specific Tigon requests using network evidence rather than UI inference.
8. Record whether ACRA or Amplitude network activity occurs; do not infer this only from static code.

Automation should locate controls by package/resource ID/accessibility description and visible text. Pixel coordinates are capture-specific fallback evidence, not durable selectors.

`dfinsta_source_1.4.1/behavior_contract.json` mirrors confirmed portions of this document in a machine-readable form. It must not mark a feature verified until an enabled/disabled device contrast has been observed.

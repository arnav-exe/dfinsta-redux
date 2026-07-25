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

Branch: `master`.

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

The historical oracle APK was pulled and confirmed byte-identical to `apks/dfinsta_1_4_1.apk`, then uninstalled with explicit user approval because its signer differed. The reconstructed debug build is installed. The user manually logged in.

Current intended state:

- All five disable switches checked.
- App force-stopped/restarted after final restoration.
- Last observed process was alive.
- Do not enable Hardcore Mode on this logged-in installation; it is not reversible through the UI.

## Confirmed Device Contract

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

### Lazy Options cause

DFInsta only replaces Instagram's existing long-click listener. Instagram reads `UserDetailFragment.A1b` before a valid app-bar offset callback, applies `LX/2QV.A0W(false)`, and hides the bar. `RefreshableAppBarLayoutBehavior.DHe()` is the only discovered writer that later updates visibility after scroll.

Preferred future fix: deliver the actual initial app-bar offset state after the fragment is registered with the behavior. Do not permanently force `A1b=true`. Full investigation and acceptance criteria: `docs/FUTURE_WORK.md`.

## Privacy Findings and Recommended Policy

Amplitude is unconditionally started from `InstagramAppShell` and sends Android ID, event `dfinsta_start`, and version to `https://api2.amplitude.com/2/httpapi` using an embedded API key. No consent or opt-out exists. Recommendation: remove from hardened baseline and do not port to 430.

ACRA installs an uncaught-exception handler and prepares an external email to `bugs@distractionfreeapps.com` with model/brand/version/stack trace fields. No visible opt-out exists. Recommendation: remove inherited ACRA; add a maintained explicit opt-in flow later only if needed.

Full audit: `docs/PRIVACY_1.4.1.md`.

No privacy code has been removed yet because oracle fidelity and future product policy were intentionally separated.

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

No cleanup code has been applied yet.

## Immediate Next Actions

1. Obtain/record user policy choice for hardened baseline:
   - Recommended: remove Amplitude and ACRA.
   - Recommended: remove proven safe dead residue.
   - Defer broad resource pruning.
2. Apply policy changes in separate commits/branches.
3. Rebuild 1.4.1 from clean stock and rerun startup/settings plus Feed/Explore/Reels/Stories contracts.
4. Add host-side or UI Automator device runner implementing `behavior_contract.json`:
   - ADB process/crash checks
   - Restart after setting changes
   - Lazy Options stimulation/polling
   - Screenshot fallback for Reels
   - Structured JSON evidence
5. Instrument lazy action-bar state before attempting a visibility fix.
6. Begin Instagram 430 extraction and semantic host mapping only after the hardened 1.4.1 baseline is green.

## Worktree Warning

The repository contains many unrelated modified files under `dfinsta_source_1.3/` and untracked playground/pipeline artifacts. They predate or are unrelated to this reconstruction sequence. Do not stage, revert, normalize, or clean them without explicit user direction. Always stage explicit paths.

Known untracked unrelated paths include `TESTING-PLAYGROUND/`, pipeline diagrams, and older docs such as `docs/FINDINGS.md`/`docs/adk_pipeline_design.md`.

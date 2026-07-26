# Device Validation Runner

`runner.py` is a Python 3 standard-library host tool driven by `dfinsta_source_1.4.1/behavior_contract.json`. It emits one structured JSON document to stdout. Device artifacts are pulled into `--artifact-dir` or a temporary directory.

The runner only exposes package/device inspection, app force-stop/start, logcat reads, UI hierarchy capture, navigation gestures, and screenshots. It does not enter credentials, mutate settings, launch the non-exported settings activity directly, clear app data, install packages, or toggle Hardcore Mode or any preference.

Run from the repository root:

```bash
python3 tools/device_validation/runner.py --adb adb --serial SERIAL preflight
python3 tools/device_validation/runner.py --serial SERIAL --artifact-dir evidence/startup startup
python3 tools/device_validation/runner.py --serial SERIAL --artifact-dir evidence/ui dump-ui
python3 tools/device_validation/runner.py --serial SERIAL find-node --content-desc Options --long-clickable true
python3 tools/device_validation/runner.py --serial SERIAL --artifact-dir evidence/settings enter-settings
python3 tools/device_validation/runner.py --serial SERIAL --artifact-dir evidence/reels reels-capture
python3 tools/device_validation/runner.py --serial SERIAL --artifact-dir evidence/features feature-state --leave-settings
python3 tools/device_validation/runner.py --serial SERIAL --artifact-dir evidence/reels feature-state --target reels
```

`startup` accepts any complete logged-out anchor set in the contract and requires a contract-approved foreground activity, process liveness, and no fatal Android runtime logcat. A contract can enable package-launcher fallback for clean installs whose explicit launcher alias self-finishes before onboarding is initialized. `enter-settings` selects Profile, retries through Home > Profile, swipes down toward the profile header, polls fresh hierarchies for a long-clickable `Options`, and long-presses only that semantic node. `reels-capture` falls back to screenshot plus process evidence when UI Automator cannot reach idle.

`feature-state` is a non-mutating capture of the contract targets `home` (feed and Stories), `explore`, and `reels`. It only taps exact contract resource-id/content-description pairs in the bottom navigation. `--leave-settings` conditionally presses Back when the contract settings title is present; it never opens settings itself. Each target records a screenshot, hierarchy when UI Automator reaches idle, process state, and required/evidence-only disabled-state text assertions. Reels requires semantic navigation first, then accepts screenshot plus live-process evidence when its post-navigation hierarchy dump cannot idle. Required assertions affect the command exit status; evidence assertions are reported without creating false hard failures.

The command does not toggle preferences, tap a Story/Reel/feed item, enter credentials, clear app data, force-stop, start, install, or uninstall the package. Run it only after the main validation flow has established the desired process-restart-bounded preference state.

Run the device-independent tests and syntax check:

```bash
python3 -m unittest discover -s tools/device_validation/tests -v
python3 -m py_compile tools/device_validation/runner.py tools/device_validation/tests/test_runner.py
```

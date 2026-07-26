# Device Validation Runner

`runner.py` is a Python 3 standard-library host tool driven by `dfinsta_source_1.4.1/behavior_contract.json`. It emits one structured JSON document to stdout. Device artifacts are pulled into `--artifact-dir` or a temporary directory.

The runner only exposes package/device inspection, app force-stop/start, logcat reads, UI hierarchy capture, navigation gestures, and screenshots. It does not enter credentials, mutate settings, launch the non-exported settings activity directly, clear app data, install packages, or toggle Hardcore Mode or any preference.

Every run requires a fresh evidence directory, artifact provenance, and explicit state declarations. The runner hashes the APK and build reports and records device model, fingerprint, API level, locale, and display metadata. It never infers account, app-data, install, or cache state.

Run from the repository root, replacing the provenance values for the actual run:

```bash
python3 tools/device_validation/runner.py \
  --contract dfinsta_source_430/behavior_contract.json \
  --serial SERIAL \
  --artifact-dir evidence/RUN_ID \
  --run-id RUN_ID \
  --artifact-apk path/to/signed.apk \
  --artifact-commit BUILD_COMMIT \
  --build-report path/to/verification.json \
  --install-state in_place_update \
  --data-state preserved \
  --account-state logged_in \
  --cache-state unknown \
  startup
```

`startup` executes exactly one strategy: the contract default or a separate `--launch-strategy` override. It never hides one strategy's failure by retrying another. A modal foreground state can require a complete logged-out anchor set. `enter-settings` uses selectors and optional recovery actions from the selected contract; the 340 contract owns its lazy-header swipe, while the 430 contract does not request one. `reels-capture` falls back to screenshot plus process evidence when UI Automator cannot reach idle.

`feature-state` is a non-mutating capture of the contract targets `home` (feed and Stories), `explore`, and `reels`. It only taps exact contract resource-id/content-description pairs in the bottom navigation. `--leave-settings` conditionally presses Back when the contract settings title is present; it never opens settings itself. Each target records a screenshot, hierarchy when UI Automator reaches idle, process state, and required/evidence-only disabled-state text assertions. Reels requires semantic navigation first, then accepts screenshot plus live-process evidence when its post-navigation hierarchy dump cannot idle. Required assertions affect the command exit status; evidence assertions are reported without creating false hard failures.

`feature-state` does not toggle preferences, tap a Story/Reel/feed item, enter credentials, clear app data, force-stop, start, install, or uninstall the package. `startup` deliberately force-stops and starts the package. Run feature capture only after the desired process-restart-bounded preference state has been established and declared.

Each command writes `evidence.json` beside its XML/screenshots. Existing evidence files are never overwritten.

Run the device-independent tests and syntax check:

```bash
python3 -m unittest discover -s tools/device_validation/tests -v
python3 -m py_compile tools/device_validation/runner.py tools/device_validation/tests/test_runner.py
```

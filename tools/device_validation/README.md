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
```

`startup` accepts any complete logged-out anchor set in the contract but treats process liveness and absence of fatal Android runtime logcat as the required checks. `enter-settings` selects Profile, retries through Home > Profile, swipes down toward the profile header, polls fresh hierarchies for a long-clickable `Options`, and long-presses only that semantic node. `reels-capture` falls back to screenshot plus process evidence when UI Automator cannot reach idle.

Run the device-independent tests and syntax check:

```bash
python3 -m unittest discover -s tools/device_validation/tests -v
python3 -m py_compile tools/device_validation/runner.py tools/device_validation/tests/test_runner.py
```

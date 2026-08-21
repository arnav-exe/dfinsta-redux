# DFInsta Redux

automated pipeline for porting dfinsta, a distraction-free instagram build that lets you toggle off feed, reels, stories, explore and profile ads to each new instagram release.

## Requirements

- python 3.13
- java runtime (eg: jdk)
- [`apktool_2.9.3.jar`](https://apktool.org/blog/apktool-2.9.3/) placed at the repo root — version-pinned, another 2.x decodes differently
- `framework-res.apk` for API 36, required to decode and build instagram 430+. Pull it from any
  API-36 device or emulator: `adb pull /system/framework/framework-res.apk framework-res-api36.apk`
- [android studio](https://developer.android.com/studio) `platform-tools/adb` and `build-tools/36.0.0` (apksigner, zipalign, aapt). Paths are
  passed explicitly, so they need not be on `PATH`
- stock instagram apk to port, put inside `apks/`
- a signing keystore, exported as `DFINSTA_KEYSTORE`, `DFINSTA_KEY_ALIAS`,
  `DFINSTA_KEYSTORE_PASSWORD`. Use the same key every time: a build signed by a different one
  cannot upgrade-install over an installed one, and the app has to be uninstalled first
- android phone signed into instagram, connected and unlocked
- [temporal](https://temporal.io) — optional, only for the durable decision gate

## Setup

```
python3.13 -m venv .venv
.venv/bin/pip install -e .
```

Verify:

```
PYTHONPATH=src .venv/bin/python -W error -m unittest discover -s tests
java -jar apktool_2.9.3.jar --version
keytool -list -keystore "$DFINSTA_KEYSTORE" -alias "$DFINSTA_KEY_ALIAS" | grep -i 'SHA-256'
```

The keystore digest must match `expected_certificate_sha256` in `release/signing_policy.json`. If
you are not the original signer it will not: put your own digest there under a new `policy_id`,
or the release gate refuses every build you sign. Expect one uninstall-and-relogin on first
install.

## Porting instagram

```
export DFINSTA_KEYSTORE=~/.android/dfinsta-signing/dfinsta-release.keystore
export DFINSTA_KEY_ALIAS=dfinsta
export DFINSTA_KEYSTORE_PASSWORD="$(cat ~/.android/dfinsta-signing/keystore-password)"

.venv/bin/python tools/port.py --apk apks/{instagram_apk.apk} --version {version_number} --run
```

- Drop `--run` to see where the port stands without changing anything
- With `--run`: decode, index, build an observing APK, sign, install, walk the phone twice, record both corpora, then build, sign and install the real APK
- Takes ~2.5 hours (cuz it needs to walk through insta). Keep the phone unlocked throughout
- Resumable. Every step decides from artefacts on disk whether it is already done, so after any stop you re-run the identical command. Walked sessions are not repeated
- It stops rather than continuing past a failure and names the command that failed

## Potential stopping reasons:

 1.**could not read the bottom nav:** Almost always android still compiling the fresh APK; the warm-up retries three times. If it persists, check whether this version renamed the tab ids:

```
adb shell uiautomator dump /sdcard/ui.xml
adb shell cat /sdcard/ui.xml | grep -o 'resource-id="[^"]*tab[^"]*"' | sort -u
```

Five ids means it was compiling; fewer means update `TAB_IDS` in `tools/device_session.py`.

 2.**A hook did not resolve:** Stops at `observe-build`. Read `work/{version_number}-port/resolution.json` — the
entry with no `descriptor` is the one that failed. `tools/check_anchor.py` says what an anchor
picks out across every decode on disk; `tools/reanchor.py` proposes a new one and accepts by
counting.

 3.**A new endpoint needs a ruling:** `ship-build` is blocked until you rule on it. See below.

## If a new endpoint needs a ruling

<details>
<summary>Raising the gate, answering it, and applying the rulings</summary>

Start a server and a worker. **`--build-id` must match in both places** — the workflows are
PINNED, so a gate raised with the wrong one is accepted and then dispatched to nobody:

```
temporal server start-dev

PYTHONPATH=src .venv/bin/python -m dfinsta_pipeline.worker \
  --state-root .pipeline-state --task-queue dfinsta-phase-a --build-id worker-{version_number}
```

Re-run the port with five more flags and it records the assessment, raises the gate, and parks
until you answer:

```
.venv/bin/python tools/port.py --apk apks/{instagram_apk.apk} --version {version_number} --run \
  --state-root .pipeline-state --assessment-run-id feat-{version_number} \
  --actor you@example --owner-token some-token --build-id worker-{version_number}
```

The gate stays open a week. Unanswered, it ends `blocked` — never an approval.

### Answering

Needs a principal file at mode `0600`. Global flags come **before** the subcommand:

```
echo '{"schema_version": 1, "uid": '$(id -u)', "actor": "you@example"}' > ~/.dfinsta-principal
chmod 600 ~/.dfinsta-principal

PYTHONPATH=src .venv/bin/python -m dfinsta_pipeline.submission \
  --endpoint localhost:7233 --state-root .pipeline-state --principal ~/.dfinsta-principal \
  show feat-{version_number} --assessment --rulings-template --consent-test
```

That prints the assessment, a rulings template, the consent test, and a subject hash it
re-derived from the ledger — the first twelve characters are your `--confirm`.

Each candidate takes a `verdict` (`block` / `offer_toggle` / `ignore` / `defer`), a `rationale`,
and for the two acting verdicts a `consent` answer (`solicited` / `unsolicited` / `mixed`). Then:

```
… submit feat-{version_number} --verdict approve --rationale "why" \
      --rulings my-rulings.json --confirm <12 hex chars>

PYTHONPATH=src .venv/bin/python -m dfinsta_pipeline.rulings \
  --state-root .pipeline-state --run-id feat-{version_number} \
  --recorded-at {iso8601_utc_timestamp} --apply
```

`--apply` records the rulings and writes `semantic_deps`. It does **not** write the
`url_block_rules` entry that makes the app block — the match kind and the preference key are
judgements. Add that by hand in `manifest/hooks.json`, then re-run the port; it resumes at
`ship-build`.

</details>

## Check it worked

```
PYTHONPATH=src .venv/bin/python -m dfinsta_pipeline.grouping report \
  --version {version_number} --walk one-pass-v1
```

Each toggle should refuse against a baseline of zero. Read our count, not instagram's — it
under-reports.

In the dfinsta app, long press Options (hamburger menu on your profile): the settings dialog should open with five toggles

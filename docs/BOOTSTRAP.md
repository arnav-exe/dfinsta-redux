# Moving this project to another machine

`git clone` gets you the code and, crucially, **the entire durable record** —
`manifest/` is 280 KB and every byte of it is tracked. Everything under `work/`
(103 GB of decodes and builds) is a cache and is deliberately left behind.

This file is the rest: what git does not carry, why each item matters, and how to
tell when you have it. `tests/test_reproducible_from_clone.py` checks the parts of
it that can be checked, because a bootstrap list is read by people who cannot yet
run anything to find out that it has rotted.

## What can never be re-derived

Read this before deciding anything is disposable.

A hook needs three kinds of evidence after a build. **One of them can be
recomputed and two cannot.**

| kind | producer | re-derivable here? |
|---|---|---|
| `static_verified` | decode → resolve → apply → build → `verify_build` | **yes**, from APK + code |
| `runtime_probe` | a device session | **no** |
| `differential` | two `runtime_probe`s, N-1 vs N | **no** — inherits |

The device half is not merely expensive to redo. Instagram decides behaviour
server-side — a MobileConfig flag picks which settings implementation loads, and
those ids renumber every release — so reinstalling the 439 build today measures a
439 client against a *current* server. Whatever that produces, it is not
`manifest/runtime_evidence/439.jsonl`; it is a new observation wearing an old
version number.

**So `manifest/` is the only copy of an unrepeatable measurement.** Never prune
it, never age-weight it, never let a run write into it (see
`docs/IMPLEMENTATION_STATE.md` — a test did exactly that and 36 fabricated rows
were committed). A blank-slate re-run from 439 reaches `static_verified` for every
version and **0 of 7 release-ready**, permanently.

## Required, in the order you will need them

### 1. Python 3.13

```
python3.13 -m venv .venv
.venv/bin/pip install -e .
```

`pyproject.toml` pins `>=3.11,<3.14`. The system interpreter on the current
machine is 3.14 and unsupported; everything is run as `.venv/bin/python`, never
as bare `python3`. The only runtime dependency is `temporalio`. Stage 5a's
proposers are an extra (`.[proposers]`) and are deliberately optional — resolve,
apply, build, verify and every gate must run with no model available at all.

Verify: `PYTHONPATH=src .venv/bin/python -W error -m unittest discover -s tests`

### 2. The stock Instagram APKs

Gitignored (`*.apk`), ~140 MB each, and **the input that keeps the re-derivable
half re-derivable**. Keep at least the pinned series in `apks/`:

```
apks/instagram_439-0-0-37-89.apk
apks/instagram_440-0-0-19-86.apk
apks/instagram-441-0-0-43-81.apk
```

`430` is the architectural floor of the older comparisons and is worth keeping;
`300` and `340` are holdout material for proposer experiments and are not needed
to port anything. `dfinsta_1_3.apk` and `dfinsta_1_4_1.apk` are the reference
builds the payloads were reconstructed from.

### 3. `apktool_2.9.3.jar` at the repository root

Gitignored (23 MB) and **version-pinned in code** — `driver.py` defaults to
`REPOSITORY / "apktool_2.9.3.jar"`. Another 2.x will decode, and its smali output
differs in ways the anchors are matched against. Requires a JDK on `PATH`
(OpenJDK 25 works).

Verify: `java -jar apktool_2.9.3.jar --version` prints `2.9.3`.

### 4. `framework-res.apk` for API 36

**The one thing that is neither in git nor obviously anywhere else.** It is
required to decode and build Instagram 430+, it is passed as `--framework-apk`,
and on the current machine it exists at exactly one path — `work/430-port/framework-res-api36.apk`
— which is inside the 103 GB that does not travel.

Recover it from any API-36 device or emulator:

```
adb shell pm path android          # → /system/framework/framework-res.apk
adb pull /system/framework/framework-res.apk framework-res-api36.apk
```

Put it somewhere outside `work/` on the new machine and pass that path.

### 5. Android SDK command-line tools

```
$HOME/Android/Sdk/platform-tools/adb          device control, runtime probes
$HOME/Android/Sdk/build-tools/36.0.0/         apksigner, zipalign, aapt
```

`finalize.py` takes `--zipalign`, `--apksigner` and `--aapt` as explicit paths, so
they need not be on `PATH`, but they must exist. `adb` is only needed for a device
session — a port builds and verifies without one, and simply cannot become
release-ready without one.

### 6. The signing keystore

```
~/.android/dfinsta-signing/dfinsta-release.keystore     alias dfinsta
```

Read from `DFINSTA_KEYSTORE`, `DFINSTA_KEY_ALIAS`, `DFINSTA_KEYSTORE_PASSWORD`.
**Not in the repository and must never be**, so it is the item most likely to be
forgotten and the one with the least forgiving failure: without the original key,
a rebuilt DFInsta cannot upgrade-install over an installed one, and the phone must
be wiped of the app to proceed. Copy it out of band.

Verify it is the right one **without printing it** — the release gate pins the
certificate, and this is the same check:

```
keytool -list -keystore "$DFINSTA_KEYSTORE" -alias "$DFINSTA_KEY_ALIAS" \
  | grep -i 'SHA-256'
# expect ee12866dc224d4f20a4f832cfdb0b9b6824ff6f4abbb1fefbaa522445aa3262d
```

A superseded keystore on this machine went unnoticed until that pin caught it, so
run the check rather than assuming the file that is there is the file you want.

### 7. A Temporal dev server — optional

```
temporal server start-dev          # localhost:7233, UI on :8233
```

Needed only for the durable orchestration (`ReplayRunWorkflow`, the feature gate,
the registered-workflow integration harness). Every stage also runs directly
through `dfinsta_pipeline.driver` with no server at all, and that is how ports are
normally done.

### 8. A device — only for the half that cannot be recomputed

One Android phone with the target Instagram account. Nothing in this repo can
produce `runtime_probe` or `differential` evidence without it, and those are two
of the three kinds a hook needs to be release-ready. See
`docs/DEVICE_SESSION_441.md` for the checklist and the traps.

## Confirming the move worked

```
PYTHONPATH=src .venv/bin/python -W error -m unittest discover -s tests
PYTHONPATH=src .venv/bin/python -m dfinsta_pipeline.expectation
PYTHONPATH=src .venv/bin/python -m dfinsta_pipeline.history
```

The suite is the real check — `tests/test_reproducible_from_clone.py` rebuilds the
readiness numbers from `git archive HEAD` and asserts them, so it passes only if
the durable record actually travelled. `expectation` should exit 0 and report
`440 → 441` met with `439 → 440` listed under NOT CHECKED (439 predates the
`static_verified` producer and has no computable readiness).

Items 3, 4, 5 and 6 are not exercised by the suite. They are needed the first time
you port a new version, which is the worst moment to discover one is missing —
check them on arrival.

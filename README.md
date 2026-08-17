# DFInsta Redux

A pipeline for porting DFInsta patches onto new Instagram APKs.

Instagram's obfuscation changes between releases, so patch sites need to be rediscovered and validated each time. This project automates most of that work: decoding, indexing, resolving hooks, building, signing, installing, measuring, and recording the result.

Manual input is kept to the parts that require judgement.

## Requirements

* Python 3.11–3.13
* git

```bash
python3.13 -m venv .venv
.venv/bin/pip install -e .
```

Porting an APK also requires:

* the stock Instagram APK in `apks/`
* `apktool_2.9.3.jar` at the repository root
* a JDK
* `framework-res.apk` from an API 36 device
* Android SDK `platform-tools` and `build-tools/36.0.0`
* the original DFInsta signing key, configured through:

  * `DFINSTA_KEYSTORE`
  * `DFINSTA_KEY_ALIAS`
  * `DFINSTA_KEYSTORE_PASSWORD`

Runtime verification requires a physical Android device and the target account. Some evidence can only be collected while running the build against Instagram's current server behaviour.

A Temporal development server is optional. It is only used for the durable human gate around endpoint-blocking decisions.

See [`docs/BOOTSTRAP.md`](docs/BOOTSTRAP.md) for setup and machine-migration details.

## Usage

Run the test suite:

```bash
PYTHONPATH=src .venv/bin/python -W error -m unittest discover -s tests
```

Check the expectations carried forward from previous ports:

```bash
PYTHONPATH=src .venv/bin/python -m dfinsta_pipeline.expectation
```

View port history:

```bash
PYTHONPATH=src .venv/bin/python -m dfinsta_pipeline.history
```

Port a new version:

```bash
# Report current state without making changes
PYTHONPATH=src .venv/bin/python tools/port.py \
    --apk apks/<stock>.apk --version <version>

# Run the port
PYTHONPATH=src .venv/bin/python tools/port.py \
    --apk apks/<stock>.apk --version <version> --run
```

`tools/port.py` is resumable and stops when human judgement is required.

To build without device measurement:

```bash
PYTHONPATH=src .venv/bin/python -m dfinsta_pipeline.driver apks/<stock>.apk \
    --out work/<version>-port \
    --framework-apk <path>/framework-res-api36.apk \
    --version <version> \
    --recorded-at <ISO8601> \
    --stop-after build
```

## Anchors

When a patch site changes shape between versions, use `check_anchor.py` to test a candidate against the decoded versions available locally:

```bash
PYTHONPATH=src .venv/bin/python tools/check_anchor.py --anchor-file candidate.txt
PYTHONPATH=src .venv/bin/python tools/check_anchor.py --hook <id> [--form N]
```

An anchor is accepted based on how selectively it matches, not how plausible it looks.

If a host resolves but its existing anchor does not:

```bash
PYTHONPATH=src .venv/bin/python tools/reanchor.py \
    --resolution work/<version>-port/resolution.json \
    --hook <id> [--apply]
```

`reanchor.py` can try several candidate patterns, but each candidate is still validated by match count. Without `--apply`, it does not modify anything.

Two steps remain manual:

* deciding which candidate endpoints should be blocked
* writing the corresponding `url_block_rules` entry

## Documentation

* [`docs/ROADMAP.md`](docs/ROADMAP.md) — current status and next steps; authoritative when documents disagree
* [`docs/IMPLEMENTATION_STATE.md`](docs/IMPLEMENTATION_STATE.md) — detailed implementation and open-item record
* [`docs/BOOTSTRAP.md`](docs/BOOTSTRAP.md) — environment setup and machine migration

## Verification

Static success is not enough to consider a hook working. A patch can apply and build correctly without ever executing at runtime.

For that reason, release readiness is derived from committed runtime evidence rather than from successful patch application alone.

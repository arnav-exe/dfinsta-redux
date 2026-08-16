# DFInsta Redux

An agentic pipeline that turns a new stock Instagram APK into a working DFInsta —
a distraction-free Instagram build — with as little human effort as possible.

Instagram ships a new version every few weeks and obfuscates almost everything, so
the class and method names a patch attaches to are different each time. The
traditional fix is a person re-reading decompiled smali for a day. This project
does that mechanically: discover what changed, re-map each hook onto the new
obfuscated code, apply, build, sign, verify — leaving humans to decide *policy* at
durable gates rather than to do the mechanical work.

**Where it stands.** Instagram 439, 440, 441 and 442 are ported, and 442 is the
first that ran the whole sequence from one command — decode, re-map, build, sign,
install, walk the phone, record. All seven hooks resolve deterministically on all
four versions with **zero agent invocations**.

That number is worth reading carefully. 442 also **broke** mechanical resolution:
it moved a literal out of the class that builds the Reels request into a shared
string table read by integer index, so the anchor pinning it stopped matching. The
repair was a second *anchor form* keyed on a name Instagram does not obfuscate,
and a person had to design it. The pipeline spent no agent invocations; the port
was not free. See `docs/ROADMAP.md`.

The 103 GB of intermediate work is disposable; the 280 KB in `manifest/` is the
project.

## Prerequisites

**To read the record, run the test suite, and check a port's evidence** — nothing
but Python:

| | |
|---|---|
| **Python 3.13** | `python3.13 -m venv .venv && .venv/bin/pip install -e .` — `pyproject.toml` pins `>=3.11,<3.14`. Always invoked as `.venv/bin/python`. |
| **git** | Two tests rebuild the readiness numbers from `git archive HEAD` to prove a fresh clone still reproduces them. |

**To port a new Instagram version** you additionally need, none of which are in
the repository:

| | |
|---|---|
| **The stock APK** | ~140 MB, gitignored, kept in `apks/`. |
| **`apktool_2.9.3.jar`** | At the repository root. Version-pinned in `driver.py` — another 2.x produces smali the anchors are not matched against. Needs a JDK. |
| **`framework-res.apk` (API 36)** | Required to decode Instagram 430+. Pull it off any API-36 device; it is not in git and on the current machine lives only inside `work/`. |
| **Android SDK** | `platform-tools/adb`, and `build-tools/36.0.0` for `apksigner`, `zipalign`, `aapt`. |
| **The signing keystore** | `DFINSTA_KEYSTORE` / `DFINSTA_KEY_ALIAS` / `DFINSTA_KEYSTORE_PASSWORD`. Never in the repository. Without the original key a rebuild cannot upgrade-install over an installed DFInsta. |

**To prove a port actually works** you need a physical Android device with the
target account. This is not optional cleanup — two of the three evidence kinds a
hook needs after a build come from a device session, and **they cannot be
recomputed later**. Instagram decides behaviour server-side, so re-measuring an
old version today measures it against a current server.

**Optional:** a Temporal dev server (`temporal server start-dev`), needed only to
raise and answer a **durable human gate** — ruling on which endpoints to block.
That is all it carries: two registered workflows, `ReplayRunWorkflow` and
`FeatureAssessmentRunWorkflow`. A port needs no server, and the 442 port ran
without one.

Full detail, including how to verify each item and what to copy when moving
machines: **[`docs/BOOTSTRAP.md`](docs/BOOTSTRAP.md)**.

## Running it

```bash
# the suite — one expected skip, everything else must pass
PYTHONPATH=src .venv/bin/python -W error -m unittest discover -s tests

# what each port owed the last one; exit 3 if a hook was lost
PYTHONPATH=src .venv/bin/python -m dfinsta_pipeline.expectation

# the version series, 439 forward
PYTHONPATH=src .venv/bin/python -m dfinsta_pipeline.history

# port a new version — THE ENTRYPOINT. Nine mechanical steps: decode, index,
# watch, build an observing APK, sign, install, two device walks, record. It is
# resumable, and without --run it changes nothing and only reports where the port
# stands. It stops before the judgement and prints what is left for a human.
PYTHONPATH=src .venv/bin/python tools/port.py \
    --apk apks/<stock>.apk --version <version>          # report
PYTHONPATH=src .venv/bin/python tools/port.py \
    --apk apks/<stock>.apk --version <version> --run    # do it

# just a build, no device and no measurement
PYTHONPATH=src .venv/bin/python -m dfinsta_pipeline.driver apks/<stock>.apk \
    --out work/<version>-port --framework-apk <path>/framework-res-api36.apk \
    --version <version> --recorded-at <ISO8601> --stop-after build
```

```bash
# is this anchor selective enough? counts it against every decode on disk
PYTHONPATH=src .venv/bin/python tools/check_anchor.py --anchor-file candidate.txt
PYTHONPATH=src .venv/bin/python tools/check_anchor.py --hook <id> [--form N]
```

When a version changes the *shape* of a patch site — 442 moved a literal into a
shared string table — the repair is a new anchor, and the only thing that makes
one trustworthy is counting. Propose by judgement, accept by arithmetic: a
candidate that matches 114 sites and one that matches 1 read the same on the page.
It exits non-zero for an anchor that matches several classes **and** for one that
matches nothing, because silence and success look identical otherwise.

```bash
# the host resolved and its anchor did not: ask k agents for a new pattern,
# and count every answer before believing any of it
PYTHONPATH=src .venv/bin/python tools/reanchor.py \
    --resolution work/<version>-port/resolution.json --hook <id> [--apply]
```

k is attempts, not votes. Two agents agreeing on an anchor that matches four
classes are both wrong, and one agent alone whose answer matches exactly once is
right — so acceptance is by counting, never by agreement. Without `--apply` it
writes nothing.

The two steps that stay human by design: **ruling** on candidate endpoints at the
gate, and **writing** the `url_block_rules` entry afterwards — the match kind and
the preference key are both refused as underivable.

## Where to read next

| | |
|---|---|
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | **Authoritative.** What is done, what is next, and why. When another document disagrees with it, it wins. |
| [`docs/IMPLEMENTATION_STATE.md`](docs/IMPLEMENTATION_STATE.md) | The detailed resume record, including "Loose ends" and "Known open items" — both kept honest by `tests/test_open_items.py`. |
| [`pipeline_flowchart.md`](pipeline_flowchart.md) | The eleven stages and the state of each. |
| [`docs/BOOTSTRAP.md`](docs/BOOTSTRAP.md) | Moving to another machine; what can and cannot be re-derived. |

## What this deliberately does not claim

A build passing every static check is not a working app. **Every inert patch this
project has shipped passed everything up to the runtime probe** — three of them
were applied, verified, released, and never executed a single instruction. That is
why a hook is not "done" when it compiles, why each one reports its own execution
from inside the app, and why the release-ready count is four rather than seven.

The count is derived from committed evidence and asserted, never declared: there
is no expected number anywhere in this repository to adjust when it becomes
inconvenient.

# Running a port

How to take a new Instagram release to a working DFInsta build on the phone,
by hand. Written 2026-08-20, after 443 was ported this way.

Everything below is one command plus what to do when it stops. **It is designed
to stop**: each step's output is the next one's input, so a run that carried on
past a failure would measure a phone in an unknown state or record a corpus from
a build that is not the one installed.

---

## 0. Before you start

**Put the APK in `apks/`.** The name is yours; the version you pass has to match
what the app reports.

    apks/instagram-444-0-0-XX-XX.apk

**Export the signing variables.** `tools/port.py` never reads, defaults or prints
the keystore secret — it refuses by name if one is missing. Read the password
from its file so it never lands in your shell history:

    export DFINSTA_KEYSTORE=~/.android/dfinsta-signing/dfinsta-release.keystore
    export DFINSTA_KEY_ALIAS=dfinsta
    export DFINSTA_KEYSTORE_PASSWORD="$(cat ~/.android/dfinsta-signing/keystore-password)"

Same key every time, or `adb install -r` refuses and you lose the login.

**Plug the phone in and unlock it.** It must stay unlocked for about two hours of
walking. `svc power stayon true` is written but **not honoured on this device** —
a device-admin policy caps screen-off at 15 minutes — so the walk keeps it awake
by tapping, and a long pause will let it doze.

---

## 1. Ask where it stands

    ./.venv/bin/python tools/port.py --apk apks/instagram-444-….apk --version 444

Nothing runs and nothing is written. You get the thirteen steps, which are done,
and roughly how long the rest will take (~2.5 hours, almost all of it the two
device walks).

## 2. Run it

    ./.venv/bin/python tools/port.py --apk apks/instagram-444-….apk --version 444 --run

That is the whole port: decode, index, build an observing APK, sign it, install
it, warm the app, walk the phone twice, record both corpora, then build, sign and
install the APK you would actually use.

**It is resumable.** Every step decides whether it is already done from artefacts
on disk, so re-running after any stop picks up where it left off — it will not
re-walk sessions it already has.

---

## 3. When it stops

It stops rather than continuing, and says which command failed. Three things
actually happen.

### The phone dropped off USB

The walk waits three minutes for it to come back and retries **that same
session** once. If it gives up, plug the phone in and re-run the same command;
completed sessions are skipped.

### "could not read the bottom nav"

Two very different causes and the message names both. Almost always it is the
first launch after `install -r`, while Android is still compiling a 140 MB APK.
The warm-up retries three times before giving up. If it still fails, check
whether this version renamed the tab ids — 440 really did:

    adb -s <serial> shell uiautomator dump /sdcard/ui.xml
    adb -s <serial> shell cat /sdcard/ui.xml | grep -o 'resource-id="[^"]*tab[^"]*"' | sort -u

Five ids (`feed_tab`, `clips_tab`, `search_tab`, `profile_tab`, `direct_tab`)
means it was compiling — re-run. Fewer means the ids moved, and `TAB_IDS` in
`tools/device_session.py` needs updating.

### A hook did not resolve

The port stops at `observe-build`. Read `work/444-port/resolution.json`: an entry
with no `descriptor` is the one that failed. This is the interesting failure and
the tools for it are `tools/check_anchor.py` (what does this anchor pick out
across every decode on disk?) and `tools/reanchor.py` (ask k agents for a new
anchor, accept by counting). 442 needed this; 443 did not.

---

## 4. If it stops at the judgement

If this version raised endpoints nobody has ruled on, `ship-build` is **blocked**
— that build renders `url_block_rules`, so building first would ship a decision
nobody took. You will see:

    [BLOCKED] assess   this run cannot raise its own gate: --state-root, … not given

Deciding what to block is yours. Everything around it can be automatic.

### Let the run raise the gate for you

Start a server and a worker first. **The `--build-id` must be identical in both
places** — both workflows are PINNED, so a gate raised with the wrong one is
accepted by the server, dispatched to nobody, and every query times out saying
nothing useful.

    temporal server start-dev -f ~/AI/dfinsta-redux/temporal/dev.db

    PYTHONPATH=src ./.venv/bin/python -m dfinsta_pipeline.worker \
      --state-root .pipeline-state --task-queue dfinsta-phase-a --build-id worker-444

Then re-run the port with five more flags:

    ./.venv/bin/python tools/port.py --apk apks/instagram-444-….apk --version 444 --run \
      --state-root .pipeline-state --assessment-run-id feat-444 \
      --actor you@example --owner-token some-owner-token \
      --build-id worker-444

It records the assessment and raises the gate, and then **the run ends with a
Workflow parked and waiting for a week**. Nothing is lost by walking away; an
unanswered gate expires to `blocked`, which is never an implicit approval.

Nothing here is invented for you: an actor and an owner token identify a person,
and a tool that made them up would be signing a document on somebody's behalf.

### Answer it

You need a principal file, mode **0600**, owned by you:

    echo '{"schema_version": 1, "uid": '$(id -u)', "actor": "you@example"}' > ~/.dfinsta-principal
    chmod 600 ~/.dfinsta-principal

Look at what it is asking. Note the global flags come **before** the subcommand:

    PYTHONPATH=src ./.venv/bin/python -m dfinsta_pipeline.submission \
      --endpoint localhost:7233 --state-root .pipeline-state --principal ~/.dfinsta-principal \
      show feat-444 --assessment --rulings-template --consent-test

That prints the assessment you are ruling against, a rulings skeleton whose
candidate ids are the derived ones, and the consent test with Lukoff's measured
bands. It also prints a subject hash **it re-derived from the ledger itself** and
the first twelve characters you must pass back as `--confirm`.

Fill the template in. Each candidate takes a `verdict`
(`block` / `offer_toggle` / `ignore` / `defer`), a `rationale`, and — for `block`
and `offer_toggle` — a `consent` answer (`solicited` / `unsolicited` / `mixed`).
`ignore` may be silent.

    PYTHONPATH=src ./.venv/bin/python -m dfinsta_pipeline.submission \
      --endpoint localhost:7233 --state-root .pipeline-state --principal ~/.dfinsta-principal \
      submit feat-444 --verdict approve --rationale "why" \
      --rulings my-rulings.json --confirm <12 hex chars>

The client runs the admitting side's own validator over your answer before
sending it, so if it cannot be admitted you find out here rather than at a worker
where you cannot see why.

### Apply them, and write the rule

    PYTHONPATH=src ./.venv/bin/python -m dfinsta_pipeline.rulings \
      --state-root .pipeline-state --run-id feat-444 \
      --recorded-at 2026-08-20T12:00:00Z --apply

That records every ruling and writes `semantic_deps`. It does **not** write the
`url_block_rules` entry that makes the app actually block: the match kind
(`contains` vs `endsWith`) and the preference key are judgements, and the
regularity that once made the match kind look derivable has since inverted — five
of eleven literals break it. Add that entry by hand in `manifest/hooks.json`.

Then re-run the port command from step 2. It resumes at `ship-build`.

---

## 5. Check it worked

The port prints `The port is finished` and the shipped APK is on the phone. Two
things the walk cannot tell you, both worth a minute:

**Does it block?** Read the corpus you just recorded. `--walk` is required, and
`grouping` compares only within one walk, so ask it twice:

    PYTHONPATH=src ./.venv/bin/python -m dfinsta_pipeline.grouping report \
      --version 444 --walk one-pass-v1

Each toggle should show refusals against a baseline of zero. Note that
Instagram's own error events under-report ours — on 443 the explore arm refused
17 and Instagram admitted 1 — so read our count, not theirs.

**Does the settings dialog open?** The walk never long-presses Options, so this is
never covered. On your **own** profile, long-press the Options button: you should
get *"Distraction-free settings - restart required"* and five toggles. On
**someone else's** profile the same button must do nothing — that is the
own-profile guard, and it is the one safety property worth checking by hand every
port.

---

## What this does not do, on purpose

It never rules on a candidate, never writes a `url_block_rules` entry, and never
invents an actor or an owner token. Everything else between a stock APK and a
signed build on the phone is in step 2.

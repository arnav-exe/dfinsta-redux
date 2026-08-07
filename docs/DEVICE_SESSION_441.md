# The 441 device session: what to record, and what it can prove

Written **before** the session, so the prediction is falsifiable and the shape
checklist is not reconstructed afterwards from what happened to get captured.

## Why the checklist exists

**The differential's reach is bounded by what the baseline recorded, and that is
unfixable retroactively.** 439 → 440 compared only 2 of 7 hooks, entirely because
of the baseline side: 439 recorded delta and absence shapes, 440's strongest
evidence for those five hooks is *identity*, and results of different shapes are
not comparable. Nothing can repair that now.

440's baseline is much better — identity for all seven — so **the shapes 441
records decide how wide 442's comparison can be**. A shape skipped here is a
comparison the next port cannot make.

## What 440 actually holds, per hook

Measured, not remembered. Only a **passing** baseline can yield a passing
differential — "a baseline that did not pass yields `inconclusive`, never
`passed`", because with no baseline pass there is no *where* for this version to
fail.

| hook | passing shapes in 440 | so 441 can |
|---|---|---|
| `set_app_context` | absence, identity | **pass** |
| `tigon_url_block` | delta, identity | **pass** |
| `replace_reels_stream_endpoint` | identity | **pass** |
| `install_settings_long_click` | identity | **pass**, if it passes first try |
| `install_settings_long_click_actionbar` | — none — | inconclusive only |
| `replace_reels_discover_endpoint` | — none — | inconclusive only |
| `replace_reels_homecoming_endpoint` | — none — | inconclusive only |

The last three are **known** not to execute on this device and configuration —
the legacy action-bar hook is the 430 side of a pair, and the two Reels variants
are dormant. Inconclusive by nature, not by measurement failure. Do not spend the
session chasing them.

## The prediction

**441 should reach 4 of 7 release-ready, up from 2 on 440**, and the fourth is
conditional. Recorded as a number so the session can falsify it.

The condition is `install_settings_long_click`. Its 440 sequence was
`inconclusive → passed → passed`, which trips the ledger's own retry guard —
*"reached passed only after a failure (3 attempts). Re-running until green is how
a ledger gets defeated."* A hook that needs three attempts to go green does not
clear the gate however green it ends up. **It needs one clean single-shot
measurement**, which means getting the walkthrough right first time rather than
re-running until it passes.

## The checklist

Record every shape 440 holds, so 442 is not thin:

1. **identity, all seven hooks** — one walkthrough, visiting `app_launch`,
   `profile_options_long_press`, `reels_tab`, `explore_tab`.
   `python -m dfinsta_pipeline.record_runtime identity --serial P3227J000775 --out <jsonl> --visit …`
2. **absence, `set_app_context`** — `record_runtime startup`.
3. **delta, `tigon_url_block`** — `record_runtime delta`, both directions, with a
   restart between sides. Two-directional or it is not a delta.
4. Anything else cheap to capture. The cost of an extra shape is minutes; the cost
   of a missing one is a comparison 442 can never make.

## After the session — the step that turns a measurement into a gate

Recording claims is not the end of it. Once `manifest/runtime_evidence/<version>.jsonl` is
committed alongside the static evidence the driver published, run:

    python -m dfinsta_pipeline.expectation

It derives what this port owed the last one — the set of hooks that were release-ready on N-1,
minus any with a recorded retirement — and **exits 3 if one was lost**. There is no expected
count anywhere to adjust; see `manifest/RETIREMENTS.md` for the only legitimate way to lower it.

Read the reasons before the number. A `differential` verdict of `failed`/`regressed` is a real
regression in this port. `inconclusive/no_current` means the hook was never measured, and the
thing to fix is this checklist, not the hook — which is exactly why step 4 above says to record
every shape.

Newly release-ready hooks print as **UNCONFIRMED**, and that is not hedging: a hook cannot become
release-ready in the port that fixes it, because `differential` needs a passing baseline to
regress from. They become the next port's expectation, and the next port is what confirms them.

`tests/test_expectation_corpus.py` runs the same sweep in the ordinary suite, so forgetting this
step does not lose the check — it only delays it.

## Traps this session has hit before

- **A subset build is not a usable app.** It removes the settings dialog, which is
  the only route to the toggles. Install the full build, or reinstall it
  immediately afterwards.
- **440 renamed the bottom-nav resource ids**, and every surface selector silently
  stopped matching while the walkthrough still reported success. Check
  `surfaces_visited` lists what you actually visited before trusting a claim.
  Verify the same for 441 rather than assuming it inherited 440's names.
- **`runtime_identity` dedups with `putIfAbsent`**, so each hook logs at most once
  per process. An absence on a second screen says nothing.
- **The identity probe sits outside the own-profile guard**, deliberately, so
  `h_install_settings_long_click` fires on *both* profiles. For the guard, the
  screencap is the oracle, not the log line.
- `uiautomator dump` fails while Reels plays; that degrades to an unusable
  measurement rather than a crash, but it also means a silently skipped surface.

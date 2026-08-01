# Pipeline Implementation State

Resume point as of 2026-08-02, branch `port-430`. Suite: 1536 tests, one
expected skip, plus six green tool suites.
Read with [`docs/ROADMAP.md`](ROADMAP.md) (authoritative progress) and
[`pipeline_flowchart.md`](../pipeline_flowchart.md) (design). This file is the
practical "how to pick this up" record: what exists, what is next, and the
specific things that will waste a day if rediscovered.

## Where the pipeline stands

Stage numbers refer to `pipeline_flowchart.md`.

| Stage | State | Where |
|---|---|---|
| 0 Load | manifest + Decision Memory | `manifest/hooks.json`, `src/dfinsta_pipeline/decisions.py` |
| **1 Extract** | **done, in the driver** | `src/dfinsta_pipeline/driver.py` |
| **2 Index** | **done** | `tools/indexer/build_index.py` (+46 tests) |
| 3 Feature discovery | **done** — stable-string layer only | `src/dfinsta_pipeline/surface_diff.py` (+95 tests) |
| **4a Assessment** | **done** | `src/dfinsta_pipeline/assessment.py` (+92 tests) |
| **4b Gate contracts** | **done** — Activities and Workflow NOT written | `src/dfinsta_pipeline/feature_gate.py` (+54 tests) |
| Gate 1 | contracts only; nothing raises it yet | — |
| **5 Resolve** | **done** — 5/7 hooks resolve mechanically on 430 and 439, hosts included | `src/dfinsta_pipeline/resolve.py` (+61 tests), `hook_index.py` (+95) |
| **5a Proposers / 5b verifier** | **done as a stage** | `src/dfinsta_pipeline/proposals.py` |
| **5c Mechanical validator** | **done** | `tools/resolver/validate_candidates.py` (+8 mutation tests) |
| **Evidence ledger** | **done** | `src/dfinsta_pipeline/evidence.py` (+144 tests) |
| 6-7 Apply / Build | done, target-parameterized, driven | `tools/port_430/build.py` |
| **8 Static verify** | **done, target-neutral** | `tools/verify/verify_build.py` (host-hook map supplied per run) |
| **9 Runtime verify** | **done, three probe kinds + per-hook identity** | `src/dfinsta_pipeline/probes.py`, `runtime_identity.py` |
| 10-11 Manifest update / report | not started | — |
| Durable orchestration | `ReplayRunWorkflow` registered, **never run for real** | `src/dfinsta_pipeline/` |

**The driver** is `python -m dfinsta_pipeline.driver <stock.apk> --out <dir>`. It runs
extract → index → resolve → pre-apply evidence gate → compose → build → static verify,
and stops at the first stage that cannot produce what the next one needs. `--reuse-decode`
and `--reuse-index` skip the two slow read-only stages. It derives per version, rather
than being told: the free `smali_classesN` for custom code (430 → 20, 439 → 21), which host
DEX files to graft, and which DFInsta call each grafted DEX must be shown to contain. All
three were hand-edited before and each silently produces a broken APK when wrong.

**The engine that makes hooks version-independent** is `src/dfinsta_pipeline/hook_manifest.py`.
Anchors are patterns with `<name:kind>` captures; payloads template off them.
**5 of 7 hooks resolve mechanically against both 430 and 439**, reproducing the
hand-authored anchors and payloads exactly. The 2 that do not are the `ui`-tier
settings hooks, declared `kind: "by_agent"`.

## A device caveat that cost time

A build carrying only a subset of hooks is not a usable app. Installing a 5-hook test
build over the working 439 one removed the settings dialog, which is the ONLY way to
reach the toggles — so the toggles were left off and unreachable until the full build was
reinstalled. Test builds go on the phone only when they carry the settings hooks, or with
the full build reinstalled straight afterwards.

## Immediate next steps, in order

1. **A trusted submission client.** The gate contracts exist but nothing raises the gate
   and no human can answer it: `execute_update` appears only in tests. This blocks stage 4
   end to end and is the smallest thing that unblocks the most.
2. **The gate's Activities and Workflow.** Follow `docs/STAGE_4_DESIGN.md` and the pattern
   in `src/dfinsta_pipeline/replay_gate.py` / `replay_workflow.py`: a preparation Activity
   returning only the request hash, a new `@workflow.defn` class (never new fields on
   `WorkflowStatus`/`RunResult`), and an admitting Activity that re-derives independently.
3. **Differential vs N−1** — the last evidence kind with no producer.
4. **Close the proposer loop.** `proposer.py` has the sandbox, prompts and parsing, but the
   agent call is a seam a human currently fills by hand.
5. **Settle the three hooks that appear to do nothing** — see below.
## Three hooks that appear to do nothing

Found this session, all by machinery built this session. None is settled; each needs a
different next step.

**`install_settings_long_click_actionbar` on 439 — probably genuinely dead.** An
adversarial verifier refuted it on reachability: `LX/0Di2;->Ac0(LX/004C;)V` is never
invoked. Independently re-checked — exactly four `invoke-interface LX/0Pvr;->Ac0` sites and
none can hold a `0Di2`; no `A1K(LX/0Pvr;)` call site passes one; `UserDetailFragment` uses
its `A0L` only for `A02` and `LX/0DEm.A00`, which reads the `A01` View that `Ac0` would
set. The live control appears to be `ProfileActionBar` + `LX/0Dxw` bound by `LX/0DnT` —
the OTHER settings hook. Decisive test: a build carrying only this hook.

**`replace_reels_homecoming_endpoint` — dormant, NOT dead.** `clips/homecoming/` and
`clips/discover/stream/` sit in the same method `LX/04tC->A0A`, selected by a branch on
`clips_viewer_homecoming_fyp` plus MobileConfig `0x810b9a007b4194` / `0x810af400383ea5`.
This account is routed to stream. Keep it: coverage for a config flip, like the two
action-bar variants.

**`replace_reels_discover_endpoint` — never runs, probably legacy.** Different method
(`A08`), whose context carries `android_purge_26_q2_ClipsDiscoverApiUtil_createRequestTask`.
**Human decision 2026-08-01: KEEP**, with a revisit trigger that fires by itself — when
that class goes, no single class carries all three Reels literals, the `co_literals`
intersection empties and Resolve escalates.

The general rule: **"never executed on my device" is not "dead."** Instagram is heavily
server-config-driven, so a hook can be correct and dormant. Find the *selector* that routes
past it; never conclude from silence.

## Four consumption endpoints DFInsta does not block

Stage 4a's first real output, stable across 430 and 439:

    feed/timeline_stream/   feed/injected_reels_media/
    feed/reels_media/       feed/reels_media_stream/

Instagram groups these with `feed/timeline/` and `discover/topical_explore/` in one curated
array (`LX/05jj` on 430, `LX/03Ez` on 439). `feed/injected_reels_media/` is Reels injected
into the timeline. Not yet decided — this is what the stage-4 gate is for.

## Constraints that cost real time to learn

**Obfuscated names are recycled.** `LX/05t2` exists in both 430 and 439 and is a
different class in each. Never join on a descriptor across versions. Index per version.

**Drawable ids are NOT stable.** Of 11,737 drawable names in both versions, only 103
keep their hex id — 99.1% are renumbered. Anchor on the drawable *name*; re-resolve the
id from that version's index. String ids are unresolvable entirely (sparse encoding
exposes ~555 of ~19,000).

**Static verification has a ceiling.** Three separate inert-patch incidents: the 340
`minshop`/`minishops` bug, the 430 settings hook (perfect statically, dead at runtime
because a MobileConfig flag picks a different implementation), and a verifier searching
DEX bytes for a smali string form that does not exist there. **A DEX stores method refs
as separate class/name/proto indices** — only type descriptors and bare method names are
literal strings.

**Probes are per-hook and must move both ways.** Logcat block-counting proves feed,
Explore, Stories — and is structurally blind to Reels, because `replaceReelsEndpoint`
blanks the endpoint upstream of the block. Zero signal in both directions means the probe
is invalid, not that the hook passed.

**Presence is not execution, and that was four bugs pretending to be four bugs.** The 340
`minshop` substitution (which lived in the retired Shopping identifier rules, not in a
settings hook), the 430 settings hook, the 439 action-bar hook, and a verifier searching
for a string form DEX does not store were all the same failure: present, never reached.
Every payload now calls `Lcom/dfinstagram/probe;->h_<hook_id>()V` — the identity is the
METHOD NAME so the call needs no registers and can never force a `.locals` change, and
`load_manifest` refuses an uninstrumented active hook. On the first build carrying it,
`replace_reels_discover_endpoint` and `replace_reels_homecoming_endpoint` turned out never
to execute, in any toggle state, across launch/Reels/Explore/profile and four swipes.

**Do not diff Instagram versions at the class layer.** Names are recycled, so a class diff
reports "everything changed". Measured 430→439: API paths survive 93.9%, stable types
89.3%, drawable NAMES 98.8% — and drawable **ids 0.9%**. `surface_diff.py` keeps names and
ids in separate mappings and never reads the id one, so this is structural rather than a
rule to remember.

**Builds are semantically, not bitwise, reproducible.** `apktool` full rebuild stamps
every ZIP entry with the build date. Verify by assertions, never by hash equality against
a stored value. The graft preserves 16,399 stock timestamps and only stamps what it writes.

**439 needs `classes21.dex`** — it already ships `classes20`. `tools/port_430/build.py`
now takes `--custom-tree` and `--replace-dex`; defaults preserve 430.

## Operational gotchas

- **apksigner**: `--ks-pass file:` reads store *and* key passwords sequentially from the
  file. Passing `--key-pass file:` at the same single-line file hits EOF. For PKCS12 omit
  `--key-pass` entirely.
- **Hardlinked sandboxes** (`cp -al`) share inodes — writing into one corrupts the
  original decode. Verification against them must be read-only.
- **UI Automator** cannot reach idle while Reels plays or a blocked feed retries
  (`ERROR: could not get idle state`). Drive to a dialog, or use `screencap`.
- **A failed `uiautomator dump` leaves the previous file in place**, so `adb pull`
  silently returns stale data. `rm -f` first and assert freshness.
- **Never `git add` a file a background agent is writing.** A commit once shipped an
  agent's deliberate scratch mutation — 15 ledger authority calls regressed to a
  shadowable form — and the suite stayed green because no test exercised that defence.
- **Blind experiments need the answers physically removed**, not merely forbidden. `.git`
  alone makes every answer reachable, and harness context injection leaks commit subjects.

## Environment

```
python        .venv/bin/python   (3.13; system python3 is 3.14 and unsupported)
tests         PYTHONPATH=src .venv/bin/python -W error -m unittest discover -s tests
              665 tests, 1 expected skip
tool suites   tools/{indexer,resolver,port_430,reconstruction,release,device_validation}/tests
adb           $HOME/Android/Sdk/platform-tools/adb   device serial P3227J000775
build-tools   $HOME/Android/Sdk/build-tools/36.0.0
keystore      ~/.android/dfinsta-signing/dfinsta-release.keystore  alias dfinsta
              cert ee12866dc224d4f20a4f832cfdb0b9b6824ff6f4abbb1fefbaa522445aa3262d
```

Decodes (read-only, under gitignored `work/`):
`work/439-explore/stock-439`, `work/430-clean-build-v2/stock-430`.
Sandboxes outside the repo: `/home/arnav/dfinsta-holdout-clean`, `/home/arnav/dfinsta-439-map`.

Current shipped artifact: `work/439-build-v1/dfinsta_439.apk`,
SHA-256 `d3d5ebcfe79fe7b08cd2826aa8bb172ef3f3ee768fa52b9cab34c85266575637`,
installed and confirmed working on the phone.

## Known open items

- `expected_anchor_count` is fixed at 1 and now vestigial in the schema; multi-site hooks
  need one entry per site.
- An over-count of a marker reports "partially applied", which matches the applier's
  wording but is inaccurate; both counts are in the message.
- `Resolution.smali_path` is declared and never populated.
- `RESERVED_CAPTURE_NAMES` restates the regex lookahead and has no runtime consumer; a
  test binds them so they cannot drift.
- The `type` kind accepts object and primitive arrays but not every exotic descriptor.
- The registered `ReplayRunWorkflow` has never executed a real port — the real-evidence
  harness drives Activities directly.

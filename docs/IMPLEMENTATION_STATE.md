# Pipeline Implementation State

Resume point as of 2026-08-02, branch `port-430`. Suite: 2100 tests, one
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
| Gate 1 | contracts only; **nothing raises it, and nothing can** — no producer | — |
| **Answering a gate** | **done** — the trusted submission client | `src/dfinsta_pipeline/submission.py`, [`docs/SUBMISSION_CLIENT.md`](SUBMISSION_CLIENT.md) |
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

## The settings hooks, after the 2026-08-02 session

**6 of 7 hooks resolve outright on 439** (was 5). `install_settings_long_click_actionbar` was
promoted by the baksmali trailing-comment fix. The seventh,
`install_settings_long_click`, now matches its host **exactly once** on both 439 (`LX/0DnT;`)
and 430 (`LX/077K;`) — its 3-line anchor was the generic "build a listener, attach it" idiom
and matched 3 times on 439 and 2 times on 430. Two lines fixed both:

    invoke-virtual <view>, <lp>  Landroid/view/View;->setLayoutParams(...)
    return-object <view>

Only the site that builds the Options ImageView is followed by `setLayoutParams` on the same
register the listener was attached to *and* then returns it. The other-user **follow button**
(`FadeInFollowButton`) calls its own setup method instead — attaching there would put the
DFInsta dialog on a stranger's Follow button. The mode moved `insert_after` → `replace`,
because an anchor ending at `return-object` would otherwise place the payload past the return:
statically perfect, runtime-inert. The 5-line anchor is unique across the *entire* decode
(1 class, vs 87 on 439 / 110 on 430 for the 3-line form), so it now identifies the host too.

**`requires_proposal` stays `true`, and this is a real negative result.** The own-profile
guard is not expressible in the manifest: the model register binds to a *different capture
name* per version (`<b>`/p4 on 439, `<d>`/p3 on 430, because the synthetic listener's `<init>`
argument order swapped), and the self-profile type (`LX/077N;` → `LX/0Dxw;`) appears on no
line adjacent to the site. A00 builds the ImageView for every action-bar model, so an
unguarded attach hits every icon on every profile — the guard is load-bearing, not decoration.

## The last non-mechanical fact, and how far the rule for it reaches

The one thing `install_settings_long_click` still needs an agent for is the own-profile
guard's type. On **430 and 439** it is derivable: the host method tests the model against ~10
obfuscated subtypes, and exactly one of them is a class whose constructor loads the drawable
named `instagram_menu_outline_24`. 439 → 10 candidates, 1 hit (`LX/0Dxw;`); 430 → 11
candidates, 1 hit (`LX/077N;`). The drawable alone is not enough (14 classes load it per
version); the intersection is. Same shape as `co_literals`.

**Held out against 340 and 300: zero reach below 430, structurally.** No `ProfileActionBar`
exists on either, no model-subtype family, and `instagram_menu_outline_24` is absent — 340 has
only `instagram_menu_pano_outline_24`, a genuinely different asset. 340's action bar is the
legacy config-object design where self/other is decided by an `instance-of` on the *fragment*
(`LX/DA4;`, `SelfFragment`), which carries no drawable at all.

**430 and 439 are therefore not two independent confirmations** — both of the rule's keys fail
together on 340 because both are consequences of one architectural rewrite. Two versions
sharing an architecture are closer to one data point. Treat it as a **430+ rule with an
explicit precondition**: check for `com/instagram/profile/actionbar/ProfileActionBar` first.

That precondition is the more useful half: its presence is the **selector** between the two
settings hooks — which one a version can host. Ground truth agrees: DFInsta 1.4.1 targeted 340,
patched the legacy host, and shipped **no own-profile guard at all**.

Separately and positively: the *other* hook's 5-line anchor matched `LX/66Y;` on 340 — the
exact class and field the shipped 1.4.1 build patched — found mechanically ~90 versions back,
at reduced selectivity (3 candidates rather than 1). **Anchors transfer further than
fingerprints built on resource names.**

## Two things fixed before host discovery could be wired through

Both are done. Recorded because each was invisible from a green suite.

- **`evidence.agreement_claim` could not record a host agreement.** It counted a proposal as
  an answer only if it named a descriptor **and** a non-empty anchor, so a clean host
  agreement returned `not_exercised` and the hook stalled. Now `agreement_claim` takes an
  `AnswerShape` (`FULL_PROPOSAL` / `HOST_ONLY`) that names the question and does two jobs at
  once: what an answer must carry, and which fields `identity()` hashes. `EvidenceKind` was
  deliberately **not** split — `AGENT_REQUIREMENTS = frozenset(EvidenceKind)` makes any new
  kind automatically required of every agent-resolved hook, so a host-discovered hook would
  owe a whole-patch agreement it can never have: the same bug rebuilt one layer up.
- **The adversarial verifier was examining the wrong answer.** `collect` tallied by
  `fingerprint` while `assess` decides by `effect_key`, so two proposers who found the *same
  site* with different-length anchors — the exact 439 result — landed in different groups and
  the verifier got whichever was registered first. Nothing shipped wrong (`assess` re-decides)
  but the expensive part was aimed at a target that might not win. Both now select through one
  path, `proposals.plurality()`.

**A structural limit worth knowing**: evidence requirements are keyed on `provenance`, and
provenance does not record *which question* a proposer was asked. So the ledger cannot enforce
"this hook needs a whole-patch agreement, not a host one" — the fact lives only in
`detail["asked"]`, which `readiness()` never reads. If that enforcement is ever wanted it is a
fourth provenance, not an eighth `EvidenceKind`.

## A test-isolation bug, pre-existing

`tests/test_phase_a_history_corpus.py` **fails when run alone** (`RuntimeError: Failed
validating workflow PortRunWorkflow`) and passes inside the full suite: it depends on an
import another module performs first. The suite being green hides it.

## Captures can be supplied from outside the anchor

`src/dfinsta_pipeline/capture_supply.py` plus `Hook.supplied_captures`. The anchor binds what
is adjacent; a supplier binds what is not. Declared per *question* rather than per field, with
a chain of suppliers in preference order. **A decline is a returned value; a failure is an
exception** — and returning neither is refused, because silence reads as success.

Measured: 439 → `p4` / `LX/0Dxw;`; 430 → `p3` / `LX/077N;`, the values the shipped patches use.
On 340 it declines, and with both keys relaxed it runs every step and still declines — a
positive control rather than an untested absence.

**`requires_proposal` on `install_settings_long_click` was deliberately NOT flipped**, so this
path does not yet run for the shipped manifest. Flipping it moves the hook from agent
provenance (owing k-of-n agreement and adversarial verification) to mechanical provenance
(owing neither) — a real reduction in scrutiny for the one hook with a safety-critical guard,
on a rule with one architecture's worth of evidence. **The test that justifies it**: a build
with the flag flipped, checking long-press does nothing on *another user's* profile. Same
shape as the test that settled the actionbar hook.

Three limits worth knowing: the `ProfileActionBar` precondition is necessary but not
sufficient (a future version that keeps the class and reorders the dispatch is where the
supplier answers confidently and is wrong); the "one dominant register" rule has a thin margin
and will likely start declining before it starts lying; and a `params` typo degrades quietly
to "an agent ran" rather than "the manifest is wrong".

**And the strategic limit**: a supplier re-derives from scratch every version and **nothing
accumulates**. This closes the structural gap in stage 5 but does not move
agent-invocations-per-port. It is a prerequisite for stage 10's second half, not a substitute.

## 7 of 7 hooks resolve on both 430 and 439 — with one runtime check outstanding

`manifest/hooks.json` now declares `supplied_captures` for `install_settings_long_click` and
`requires_proposal` is gone. Both versions resolve all seven hooks, the deterministic supplier
winning on both with no agent fallback and every host descriptor byte-identical to before.

**The guard was device-tested and holds.** Own profile: the dialog opens (positive control).
Another user's profile: Instagram's own sheet — Restrict / Block / Report — and no DFInsta
dialog. Fresh launch: 10 canonical blocks. That test was the right one because `A00` builds the
action-bar ImageView for **every** profile model, so an unguarded attach would reach every icon
on every profile; the morning's test asked the opposite question (inert vs over-broad).

A correction worth keeping: I predicted the probe would fire on both profiles and concluded
logcat could not be the oracle. Right conclusion, wrong mechanism — `runtime_identity` dedups
with `putIfAbsent`, so **each hook logs at most once per process**, and the absence on the
second profile said nothing either way. Check an instrument's own semantics before designing a
differential around it.

**The probe now sits OUTSIDE the guard**, deliberately: `runtime_identity`'s contract is that
the site reports execution even if a later instruction throws. The consequence, which matters
for reading the test: `h_install_settings_long_click` fires on *both* profiles, because the
site executes on both. **The identity line is not the oracle here — the screencap is.**

## Agent invocations per port is now measured, and the first reading was FLAT

`python -m dfinsta_pipeline.agent_cost report <version>`. The flowchart's central claim —
agent invocations fall with every port, and a flat count means the pipeline is not learning —
had never been measured. First real reading over 340/430/439: **FLAT**, 2 on 439 and 2 on 430.
The claim's own failure state, first try. Selectivity margins are trended alongside it, so a
fingerprint narrowing toward `1 -> 1` is visible before it reaches zero.

Four honest limits: the rot signal is a differential so real coverage starts at the *third*
port; the capture-supplier margin is read back with a regex over prose and fails closed by
disappearing (fix: a typed `measured` field on `capture_supply.Supplied`); it counts hooks
that needed an agent, not model calls; and **nothing calls `record_run` yet**, so this is a
measurable claim rather than a measured one.

## Immediate next steps, in order

1. **Give the feature gate a producer.** This is the real blocker for stage 4, and it is
   not the wiring task the previous version of this list assumed. `feature_gate.py` and
   `assessment.py` are each imported by nothing but their own tests. Stage 4a computes an
   assessment in the **driver** world — a plain Python pipeline over a decode — while the
   gate expects that assessment to exist in CAS as a completed **ledger** operation in the
   Temporal world. Nothing joins those two worlds today. The missing link is an Activity
   that records a stage 4a assessment as a ledger operation, and the design question it
   raises (where the standalone driver and the durable pipeline meet) is worth answering
   deliberately. Writing the gate's Workflow first would produce a Workflow with nothing to
   gate on.
2. **Then the gate's Activities and Workflow.** Follow `docs/STAGE_4_DESIGN.md` and the
   pattern in `src/dfinsta_pipeline/replay_gate.py` / `replay_workflow.py`: a preparation
   Activity returning only the request hash, a new `@workflow.defn` class (never new fields
   on `WorkflowStatus`/`RunResult`), and an admitting Activity that re-derives
   independently. The client already has the seam for it: register a `GateKind` whose
   resolver reproduces `FeatureGateRequestV1` and whose update carries
   `FeatureGateSubmissionV1` rather than a bare `GateDecision`.
3. **Differential vs N−1** — the last evidence kind with no producer.
4. **The proposer chain has now run end to end, and the gate correctly did not open.**
   `install_settings_long_click` on 439, k=3 concurrent and blind, sandbox with the answers
   physically absent: two proposers independently reached `LX/0DnT;` — the known live host —
   and one was dropped for inventing a schema field. But **agreement by effect was 1 of 3**:
   the two surviving proposals differ in anchor length and payload, so `Proposal.effect_key`
   separates them and `assess` refuses. Two adversarial verifiers, shown the claim and never
   the rationale, each **failed to refute** it. So one required signal passed and the other
   did not, which is the design working. What is left is to raise k, or to accept that the
   host is agreed and the *patch* is what needs the manifest's shape to constrain it.
5. **Settle the three hooks that appear to do nothing** — see below.

## Answering a gate is no longer hypothetical

`python -m dfinsta_pipeline.submission show|submit <workflow-id>` is the trusted submission
client, built 2026-08-02. Design record: [`docs/SUBMISSION_CLIENT.md`](SUBMISSION_CLIENT.md).
The rule it is built from — **re-derive the subject from recorded state, and refuse to let a
human sign a hash you cannot reproduce** — is what distinguishes it from the standalone
replay CLI this project designed, reviewed and deleted for self-asserting its hashes.

Two consequences that will look surprising until you know why:

- **`PortRunWorkflow`'s `phase-a-approval` gate is deliberately unanswerable.** Its subject
  is `canonical_sha256(spec)` plus two operation outputs, and the ledger indexes operations
  by content hash rather than by run, so a client holding only a run id cannot reach them.
  It is not registered, and the client says so and stops rather than trusting the published
  hashes.
- **`Ledger(path, read_only=True)`** exists for this client: `mode=ro`, no schema
  statements, all eight mutating methods guarded. The client cannot create the state it is
  checking. `configure_runtime(..., read_only=True)` is how the production derivation
  helpers are reused rather than reimplemented.

It also uncovered that **`prepare_replay_verification_gate_activity` had never run against
real recorded state** — every existing test stubbed it. `tests/test_submission_resolver.py`
is the first test that drives that derivation over a real ledger and content store.
## Three hooks that appear to do nothing

Found this session, all by machinery built this session. None is settled; each needs a
different next step.

**`install_settings_long_click_actionbar` on 439 — SETTLED 2026-08-02: inert on 439, and
KEEP.** The decisive test ran. Two structurally symmetric single-hook builds (differing in
exactly `classes6.dex`, `classes21.dex` and the signatures): the actionbar build's long-press
opened Instagram's own Settings screen with no DFInsta dialog and **no identity line at
all**, while the control — a build carrying only `install_settings_long_click` — opened the
dialog and logged `DFInstaProbe: install_settings_long_click`. The control is what makes the
absence a measurement rather than a broken experiment.

So the adversarial verifier was right that `LX/0Di2;->Ac0` is not reached on 439. But the
verdict is **keep, not retire**: the legacy variant is the live control on **430**, so the
two hooks are a version/config-selected pair like the Reels homecoming/stream pair, and
deleting the inert one breaks the older target. Note the names are inverted from what they
suggest — `install_settings_long_click` is the *ProfileActionBar* variant and
`install_settings_long_click_actionbar` is the *legacy IgActionBar* one.

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

**baksmali annotates constants that look like floats, and it follows the NUMBER, not the
version.** The action-bar anchor line was bare `const v0, 0x7f134a0e` on 430 and
`const v0, 0x7f134a34    # 1.957818E38f` on 439 — same class, same field — so the anchor
silently stopped matching at a version bump on a line nobody changed. Anchors now match
against a quote-aware comment-stripped line and **emit the source line verbatim**: stripping
in `significant()` instead would make a hook resolve and then patch nothing, because the
applier searches a view that keeps the comment. 66,169 annotated code lines on 439; 1,152
lines carry a `#` inside a string literal.

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
- **Never `git add` a file a background agent is writing — and a stability check does not
  license it.** A test file was committed 78 lines short because the "has this file stopped
  changing?" check had been invalidated by a `SendMessage` that resumed the very agent it was
  watching. Wait for the completion notification; a timer measures the wrong thing. A commit once shipped an
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

Current shipped artifact: `work/439-full-allseven/dfinsta_439_allseven.apk`,
SHA-256 `8442b73e67dd7cdea7e199dc754777236b45b88663e0e6fdfdb8db2587ded9d7`,
installed and confirmed working on the phone 2026-08-02 — seven hooks, per-hook identity, and
a device-verified own-profile guard. It replaces `d3d5ebcf…`, which had neither the guard nor
any probe instrumentation.

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

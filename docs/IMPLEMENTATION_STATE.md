# Pipeline Implementation State

Resume point as of 2026-08-03, branch `port-430`.
Read with [`docs/ROADMAP.md`](ROADMAP.md) (authoritative progress) and
[`pipeline_flowchart.md`](../pipeline_flowchart.md) (design). This file is the
practical "how to pick this up" record: what exists, what is next, and the
specific things that will waste a day if rediscovered.

## Instagram 440 resolved 7/7 mechanically, for zero agent invocations

2026-08-03, the first port of a version that arrived *after* the fingerprints were
written. `python -m dfinsta_pipeline.driver apks/instagram_440-0-0-19-86.apk --out
work/440-port --version 440 --framework-apk …` with **no `--proposals`, no
`--full-proposals` and no `--discover-hosts`**:

- **all seven hooks resolved**, `"complete": true`, and the pre-apply evidence gate
  passed with nothing skipped (14 claims: 7 `anchor_unique`, 7 `registers_safe`);
- **`by_anchor` selected exactly one class out of 182,479, for both settings hooks**,
  on a version it had never seen — `LX/DVk;` and `LX/DHo;`. The prefilters narrowed
  to 981 and 26 candidates respectively before the anchor match;
- **an independent fingerprint agrees.** `notifications_entry_point_impression`
  appears in 3 classes on 440; intersected with `ig4a-instagram-schema` it selects
  exactly one, `LX/DHo;` — the same class the anchor found. Two mechanisms derived
  from different versions, agreeing on a third;
- **the deterministic capture supplier resolved the own-profile guard** with no
  agent fallback: precondition present, `instagram_menu_outline_24` → `0x7f082543`,
  model register `p4`, and 1 of 10 dispatch subtypes carrying the drawable
  (`LX/DdJ;`). The rule that `docs` warned rested on "roughly one data point" held
  on a third version;
- the rendered payload is correct — probe call outside the guard, `instance-of p4,
  LX/DdJ;` around the `setOnLongClickListener`, and the replace-mode anchor tail
  (`setLayoutParams`, `return-object`) preserved.

**And the fingerprint the generaliser refused turns out to be right on 440 and wrong
on 430.** `ProfileActionBarViewBinder` selects exactly one class on 440 and it *is*
`LX/DVk;`. On 430 it selected the wrong host. Refusing it was still correct, and the
lesson sharpens: cross-version verification is not asking "would this have been wrong
on the next version" but "is it wrong **anywhere** in the supported range".

## What the first genuine end-to-end run found

Two bugs, both invisible until a stock APK went in one end with nothing reused.

**1. The extract-then-build seam had never run.** `driver.extract()` installs the
API 36 framework into `<out>/framework`, and `build.py` listed `--framework-path`
among the paths it refuses to overwrite — so the build died on a directory the
driver had just created. Every previous "unattended port" passed `--reuse-decode`,
which skips extraction, so the collision never appeared. The framework path is an
apktool *cache* (installed into with `apktool if`, then read from), not an artifact
the build produces; the other seven entries in that list are all outputs and are now
pinned individually by tests, including the four whose names are *derived* from
other arguments.

**2. Instagram 440's manifest cannot be compiled by aapt1.** 440 added

    <provider android:authorities="com.facebook.pages.app.ig4work.tokenhandoff"/>

inside `<queries>`. That is legal Android — a `<queries><provider>` matches on
authority and takes no `android:name` — but aapt1 predates `<queries>` and validates
its children by the `<application>` rules, so the whole build fails with "Tag
`<provider>` missing required attribute name". 439 has 21 providers and every one is
named; this is new. `prepare_tree.sanitise_manifest_for_aapt1` removes such elements
**from inside `<queries>` only**, and prints what it removed.

That edit is safe for one specific reason, which will stop being true if the graft
ever changes: `graft_apk` copies only DEX entries out of the intermediate APK, and
`AndroidManifest.xml`, `resources.arsc` and every `res/` entry come byte-for-byte
from the stock archive. Nothing edited in the work tree can reach the shipped app. A
name-less `<provider>` *outside* `<queries>` is deliberately left alone, so a
genuinely malformed manifest still fails loudly.

## 440 on the device: it works, and the guard holds

Signed with the pinned certificate (`ee12866d…` confirmed by `apksigner verify --print-certs`)
and installed as an in-place upgrade over the 439 build, which preserved the login and left
all five toggles on. Artifact: `work/440-clean/dfinsta_440_signed.apk`, SHA-256
`c9e063e5…`; unsigned `work/440-clean/dfinsta.apk`.

- **the app starts and stays foreground**, with a capture demonstrably covering this app
  starting — `set_app_context` `passed`;
- **four hooks announced their own execution** across launch → profile → Reels → Explore →
  profile: `set_app_context`, `tigon_url_block`, `replace_reels_stream_endpoint` and
  **`install_settings_long_click`**. The three silent ones are the same three that were silent
  on 439 — the two dormant Reels variants and the legacy action-bar one — so the pattern is
  reproduced, not merely repeated;
- **the settings dialog opens** on long-pressing Options on the own profile: "Distraction-free
  settings - restart required" with all five toggles;
- **`tigon_url_block` gives a clean two-directional delta: 10 blocks with the toggles on, 0
  with them off** — the same counts as 439;
- **the own-profile guard holds.** On another user's profile the Options button is
  `long-clickable="false"` in the accessibility tree (it is `true` on the own profile), and
  long-pressing it produces no DFInsta dialog. The guard type `LX/DdJ;` was derived
  mechanically by the deterministic supplier, so this is the first time that rule has been
  device-proved on a version no human mapped. Dumps kept under `work/440-runtime/ui/`.

The toggles were restored to on and `svc power stayon` reset afterwards.

## The first differential ever taken: 439 → 440

    python -m dfinsta_pipeline.differential \
      --baseline work/evidence-439-runtime.jsonl --baseline-version 439 \
      --current work/440-runtime/evidence-440.jsonl --current-version 440 \
      --actor device:P3227J000775 --baseline-build 8442b73e… --current-build c9e063e5…

**Two hooks `passed`/`held`** — `set_app_context` (absence vs absence) and `tigon_url_block`
(delta vs delta). **Five `inconclusive`/`shapes_disjoint`**, and that result is the finding:
the 439 baseline recorded only feature-shaped claims, while 440's strongest evidence for those
five is *identity*, and a boolean "did the site run" cannot be compared with a signal count.

**The differential's reach is bounded by what the baseline recorded.** Nothing can be done
about that retroactively; what fixes it is that 440's ledger now carries identity claims, so
441 will have identity-vs-identity available — the sharpest comparison there is. The report
now also says, for each disjoint pair, whether the baseline had *any* passing result, because
"probed differently" alone implies a comparison was lost when for all five there was nothing
to regress from either way.

## The differential vs N−1 now has a producer

`src/dfinsta_pipeline/differential.py`, and

    python -m dfinsta_pipeline.differential --baseline <N-1 evidence.jsonl> \
        --baseline-version 439 --current <N evidence.jsonl> --current-version 440 \
        --actor device:P3227J000775 [--out …] [--json]

It reads two runs' `runtime_probe` claims and emits one `DIFFERENTIAL` claim per hook. The
hard half is the one the kind's own description names — *"a port regression, told apart from
a broken probe"* — because from a single capture, "the patch is inert on N" and "the log line
this probe counts was renamed in N" look identical.

**What makes a capture capable of seeing the signal** is already recorded, per shape:
a `delta` probe saw the string at all in either direction; an `absence` probe has
`control_found`; an `identity` probe has a non-empty `hooks_that_ran`. The shapes are ranked
identity → delta → absence and the most decisive pair available on both sides decides,
because `DFInstaProbe: <hook_id>` is the one signal **DFInsta emits itself** and so cannot rot
when Instagram renames something. A hook that stayed silent in a capture where *other* hooks
announced themselves is a proven silence.

Three rules worth knowing before reading a report:

- **A baseline that did not pass yields `inconclusive`, never `passed`.** With no baseline
  pass there is no *where* for this version to fail. The cost is deliberate: the first port of
  any hook cannot satisfy `DIFFERENTIAL` mechanically and needs a human waiver, which keeps
  "compared and unchanged" distinct from "never compared".
- **A current `inconclusive` carrying `attribution: shared` is not a regression.** Attribution
  was lost, not necessarily the behaviour. Five of the seven 439 claims are in exactly this
  state.
- **Comparing a version with itself is refused outright**, as is a differential between two
  runs of the same build hash. Two builds of one version are byte-identical here, so it would
  enter the ledger as evidence that a comparison happened.

**3. A failed build used to erase the cost record.** `record_run` ran at the end of
`run()`, and a `DriverError` from the build stage went straight past it — so two 440
runs that resolved all seven hooks for zero agent invocations recorded nothing at
all. What a port *cost* is settled at resolve time. `DriverError` now carries the
report, and `run()` records the cost before re-raising: a receipt, not a rescue.

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

## The cost verdict is no longer UNTESTABLE: it reads FALLING

2026-08-03, after 440. `python -m dfinsta_pipeline.agent_cost report 440`:

    agent invocations: 0   (was 2 on 439, its latest of 2 run(s))
    ROUTES  agent_proposal 0 (was 2) · deterministic_supplier 1 (was 0) · mechanical 6 (was 5)
    VERDICT: falling — 2 fewer than 439.
             Retired: install_settings_long_click, install_settings_long_click_actionbar

The selectivity trend is healthy too: the Reels `by_literal` margin went 5→1 on 439 to 7→1 on
440, i.e. *widening* — more candidates excluded, not fewer. The supplier margin held at 10→1.

**One honesty caveat that belongs next to the number.** The count fell because the manifest
gained `by_anchor`, and that change was derived by a human from what 439's agents cited — the
generaliser *proposes*, it does not commit. So the measured claim is "the learning loop
closes, with a human in it", not "the pipeline learns unattended". The section below is the
first reading, kept because it is what the claim's own failure state looked like.

## Agent invocations per port was measured before that, and the first reading was FLAT

`python -m dfinsta_pipeline.agent_cost report <version>`. The flowchart's central claim —
agent invocations fall with every port, and a flat count means the pipeline is not learning —
had never been measured. First real reading over 340/430/439: **FLAT**, 2 on 439 and 2 on 430.
The claim's own failure state, first try. Selectivity margins are trended alongside it, so a
fingerprint narrowing toward `1 -> 1` is visible before it reaches zero.

`record_run` is now called by `driver.py`, and `manifest/agent_cost.jsonl` holds real data:
**439 run 2 of 2 cost 2 agent invocations against 5 mechanical hooks.** The verdict is
`UNTESTABLE` until a second version is ported, which is correct — the claim is about a
sequence.

Three honest limits remain: the rot signal is a differential so real coverage starts at the
*third* port; the capture-supplier margin is read back with a regex over prose and fails closed
by *disappearing* (fix: a typed `measured` field on `capture_supply.Supplied`); and it counts
hooks that needed an agent, not model calls.

**A defect the first real run exposed**: `cost_report` originally aggregated a version's whole
history, so re-running 439 reported 14 hooks and 4 invocations. The claim is invocations per
*port*, and two attempts at one version are not two ports — a metric that inflates with retries
flatters or damns the project by accident. Fixed by keying a run on `(version, recorded_at)`,
which needed no schema change because `record_run` already stamps every record of a run
identically. The ledger keeps the failed attempt; only the reading changed.

## The manifest has no `by_agent` fingerprint left

`kind: "by_anchor"` — *the class whose body matches this hook's own anchor pattern* — retired
both remaining agent fingerprints. **7 of 7 hooks resolve on 430 and 439 with no `--proposals`
at all**, so the next port costs **0 agent invocations** where 439 cost 2.

Measured, not cited: each five-line anchor matches exactly one class per decode and it is the
known host every time (1 of 181,421 on 439, 1 of 179,190 on 430; the three-line form it
replaced matched 87 and 110). Cross-checked by a full unprefiltered match over every class of
both decodes with no shared code — same four hosts.

Cost: one decode walk per `by_anchor` hook, 6.1 s for both warm, with a prefilter derived from
the anchor's own longest fixed run (17-34×). Cold cache unmeasured, realistically 10-15 s. Each
hook is its own walk; fine at two, worth batching at four.

**Why this may drop `proposer_agreement` and `adversarial_verified`**: those exist because an
agent's descriptor is an assertion nothing checks, and a 430 descriptor still names a live
class in 439. Here nothing is carried between versions — the pattern is re-matched against the
target decode every port and is the same text the patch is spliced into, so a version where it
stops identifying the host is one where it stops identifying the *site*, and the hook escalates
rather than resolving somewhere wrong.

The scan also collects classes carrying the hook's **marker**, which is load-bearing rather
than incidental: one of these hooks is `replace` mode, so a decode this pipeline already
patched no longer matches its own anchor, and without the marker route a re-run would report
NOT_FOUND where every other kind reports ALREADY_APPLIED.

**What it does not prove**: the claim that agent cost *falls* still needs a real second
version. The verdict stays `UNTESTABLE` until 440.

## Stage 10's generaliser, and the poison it caught

`generalise.py` turns a proposer's cited evidence into a durable host fingerprint, verified
before proposing and **proposing rather than committing**.

**It caught a fingerprint that would have poisoned 440 on its first real run.** For
`install_settings_long_click` the proposers cited the systrace string
`ProfileActionBarViewBinder.bindUsernameTitle…`. It selects exactly one class on 439 and that
class is the right one; on 430 it also selects exactly one — and it is the **wrong** host
(`ProfileActionBar` rather than `LX/077K;`). Committed on 439 evidence alone it would have
looked immaculate. **One version is a coincidence.**

`install_settings_long_click_actionbar` does have one:
`notifications_entry_point_impression` ∩ `ig4a-instagram-schema`, exactly 1 class on each
version, right both times. Neither literal alone is selective (3 and 1466 on 439). But it does
not resolve today — `by_literal` goes through `HookIndex.descriptors_with_literal`, which holds
only API-path-shaped strings, so both are `not_indexed`. The module reports
`BLOCK_NOT_INDEXED` and refuses to call the hook mechanical rather than letting the count fall
on paper.

**The conclusion that redirected the work**: for these hooks the discriminating structure is
not a string, it is the **anchor**. Re-measured rather than cited — the five-line anchor selects
**exactly one class on 439 and 430, for both settings hooks, the right one every time** (the
three-line form it replaced matched 87 and 110). A `by_anchor` fingerprint is what takes the
next port's 2 agent invocations to 0.

## The unattended build is byte-identical to the hand-verified one

Compared `work/439-autonomous` (produced with `--discover-hosts`, no human input) against
`work/439-full-allseven` (hosts supplied by hand, guard device-verified):

- identical resolved hosts for all seven hooks
- identical `host-hooks.json`
- **every patched class byte-identical** — `X/0DnT.smali`, `X/0Di2.smali`, `X/04tC.smali`

So the device evidence gathered against `8442b73e…` — the own-profile guard holding, ten
canonical blocks — carries over: the autonomous run patched the same bytes. It also means a
differential between these two would be vacuously "identical"; the useful differential is
against a *previous version's* known-good build, not a second build of the same one.

## The suite was audited, and the count was hiding the real gap

2026-08-02, after the suite grew 1536 → 2170 in a day. **Not padded**: no duplication (largest
near-duplicate name cluster is 4), 1 test per 6.3 source statements, ~43 of them explicit
positive controls. About 5–8% could be consolidated by parameterising "family" clusters, and
that was argued against: a parameterised failure says "case 3 failed", a named test says
"already_applied produces no record", and this project's recurring failure is silently-green
tests.

**The finding**: `probes.py` — 343 statements, **zero tests**, no test file, nothing importing
it. That is the module producing `runtime_probe` evidence, the check between a build and a
release. `agent_runner.py` was at 38%, `proposer.py` at 71%. A day of adding tests to a
reporting module left the module that decides whether a hook *works* untested, and the growing
total made the suite look healthier. **Read coverage by module, not the test count.**

## Immediate next steps, in order

Reordered 2026-08-03 after 440 ported itself for zero agent invocations. Steps 1-4 of the
previous list are done; what is left is the two halves of the pipeline that still do not meet,
plus the release path.

1. **The release gate cannot consume the driver's own output.** `tools/release/finalize.py`
   invokes its `--final-verifier` with `--apktool-jar --apksigner --require-signature
   --expected-certificate-sha256`, which only `tools/port_430/verify_apk.py` accepts — and
   that verifier is 430-shaped. `tools/verify/verify_build.py` is target-neutral but checks no
   signature at all, so there is **no target-neutral post-signing verification**. The 440 build
   was therefore aligned, signed and certificate-checked by hand (the same assertions, made
   explicitly). Closing this is what makes "one command, stock APK in, signed release out"
   true rather than nearly true. Its identity envelope is already done — `schema_version`,
   `apk_sha256`, `stock_apk_sha256`, `verifier_sha256` — so what remains is the signature half.

2. ~~Nothing turns device measurements into ledger claims.~~ **Done**:
   `src/dfinsta_pipeline/record_runtime.py`, three modes matching the three probe shapes —

       python -m dfinsta_pipeline.record_runtime identity --serial … --out <jsonl> \
           --visit profile_options_long_press --visit reels_tab --visit explore_tab
       python -m dfinsta_pipeline.record_runtime startup --serial … --out <jsonl>
       python -m dfinsta_pipeline.record_runtime delta --hook tigon_url_block \
           --state enabled --measurements <json> --serial … --out <jsonl>

   The delta store is a file because the two halves of a two-directional probe are separated by
   a human moving a toggle; a half-finished probe stays visibly half-finished, and an
   **unusable** half is never stored, so it cannot be paired later as though it were a
   measurement. Shared-signal attribution is applied here rather than left to the reader.

   Building it found two live defects, both by running it against the phone rather than reading
   it. **Instagram 440 renamed the bottom-navigation resource ids** — `…:id/profile_tab`,
   `feed_tab`, `clips_tab`, `search_tab` are all gone — so every surface selector silently
   stopped matching and a whole walkthrough recorded `surfaces_visited: ["app_launch"]` while
   filing the settings hook as "its site may not have been reached". True, and completely
   misleading. The accessibility `content-desc` values survived ("Home", "Reels", "Profile"),
   so `Surface` now carries both and `navigate` tries them **separately, in order**, from one
   dump. And `AdbDevice.ui_xml` used to crash the entire recording when `uiautomator dump`
   could not reach idle — routine while Reels plays; it now raises `UiUnavailable`, which
   degrades to an unusable measurement or a skipped surface, and the note no longer says "the
   entry control was not found on screen" when nothing could be read at all.

   Re-run after the fix, on the phone: `["app_launch", "profile_options_long_press",
   "reels_tab"]` and `install_settings_long_click` **passed** — reproducing by command what had
   only been done by hand with tap coordinates.

3. **Give the feature gate a producer.** `feature_gate.py` and `assessment.py` are each
   imported by nothing but their own tests. Stage 4a computes an assessment in the **driver**
   world while the gate expects it in CAS as a completed **ledger** operation in the Temporal
   world, and nothing joins them. The missing link is an Activity that records a stage 4a
   assessment as a ledger operation — a design question about where the standalone driver and
   the durable pipeline meet, not a wiring task. Then the gate's Activities and Workflow, per
   `docs/STAGE_4_DESIGN.md`: a preparation Activity returning only the request hash, a new
   `@workflow.defn` class (never new fields on `WorkflowStatus`/`RunResult`), and an admitting
   Activity that re-derives independently. The submission client already has the seam — a
   `GateKind` whose resolver reproduces `FeatureGateRequestV1`.

4. **Make the generaliser's proposals reach the manifest.** The count fell 2 → 0 because a
   human read what the 439 agents cited and wrote `by_anchor`. Stage 10 proposes and a human
   commits, which is the right default — but the step between them is undocumented and
   unexercised, so the next fingerprint will be derived the same ad-hoc way.

5. **Re-measure the five hooks the differential could not compare.** They are inconclusive
   because 439's ledger has no identity claims, not because anything is wrong. 440's does, so
   this resolves itself at 441 — but a deliberate delta measurement for the Reels and settings
   hooks on 440 would make the next differential four-wide instead of two.

6. **Operational hardening**: an opt-in real run through the registered Workflow, a
   non-destructive cancellation path, heartbeats in the replay Activities.

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

**The canonical-counting guard is incomplete, and was being carried by the signal string.**
`probes.py` had 343 statements and zero tests until 2026-08-02; testing it found three defects.
The documented rule — an un-indented message is a live event, an indented echo or re-narration
is not — misses a third form: `aware_trace_readable` spills across continuation lines, each
prefixed `TAG: `, so a *past* block's narration arrives **un-indented** carrying
`fault_message: …`. Measured on this repo's own captures, `logcat_explore_OFF.txt` returns 2
canonical where the module's docstring claims 0. It does not bite only because the manifest
declares the signal with its `java.io.IOException: ` prefix — so the off-side reads zero
because of the signal text, not the guard. A hook declaring the bare text (as every docstring
example does) would see a phantom off-side and report a working hook as `failed`.
Also found: `StartupProbe` records `failed` rather than refusing when the screen is unusable,
and `ProbeRunner.measure` treats an *unreadable* foreground as a usable zero.

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

# Pipeline Implementation State

Resume point as of 2026-08-05, branch `port-430`. Suite: **2672 tests**, one expected skip,
plus six green tool suites (indexer 46, resolver 8, port_430 45, reconstruction 15, release 6,
device_validation 22).

**The Execute machine has no open roadmap items.** The operational-hardening thread closed on
2026-08-05, each step measured before the next was attempted: a cancelled replay stage now
*releases* its claim unless its subprocess could not be shown to have exited; the decoded-tree
walks moved off the event loop, taking query availability during a stage from 23% to 92%; and
every stage now heartbeats every 30 s, so worker loss is detected in five minutes rather than
at `start_to_close` expiry — three hours for verify. Measured worst heartbeat gap on a real
port: 30.9 s. See `docs/WORKFLOW_REGISTRATION_DESIGN.md` §3d-§3g.

**Stage 4a now finds six candidates where it found four.** `find_groupings` looked classes up
by the normalised rule while the index holds the app's own text, so `clips/discover` matched no
class where `/clips/discover` matched one holding `delivery/background_prefetch` — a surface
absent from 430 and present on 439, which `surface_diff` had independently classified
`B_inline` riding with the two Reels endpoints. An audit settled the other half of that
question: **stage 3 must not feed the feature gate** (44 of its 105 candidates are permalink
spellings for a surface already blocked, 90 cannot be given a legal candidate id, and 105
through a gate proven at 4 makes "every candidate must be ruled on" a rubber stamp). See
`docs/ROADMAP.md`.

**A cancelled replay operation is now released rather than quarantined**, unless its subprocess
could not be shown to have exited — `executor.ProcessNotReaped` is that signal and
`activities._releasable` is the rule. Quarantine is terminal, so this is what unblocks
heartbeats. The remaining prerequisite, threading the decoded-tree primitives, is designed and
benchmarked in `docs/WORKFLOW_REGISTRATION_DESIGN.md` §3f: on a real 209k-file tree the loop is
blocked 100% of a capture today, 5-7% in a thread, longest stall 59-67 s versus 0.16 s.

**The registered `ReplayRunWorkflow` has run a real port.** Both 340 and 430 completed on a
live Temporal server on 2026-08-04, driven by
`tests/integration/test_registered_replay_harness.py` — evidence at
`/home/arnav/dfinsta-runs/registered-{340,430}/success.json`. That closes the item three
follow-ups were waiting on; see `docs/WORKFLOW_REGISTRATION_DESIGN.md` §3c-measured for what it
settled and the two defects it found in its first minutes.
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

## The feature gate has a producer, and the subject re-derives from a run id

2026-08-03. `src/dfinsta_pipeline/assessment_record.py`, following option C5 of
[`docs/STAGE_4_PRODUCER_DESIGN.md`](STAGE_4_PRODUCER_DESIGN.md).

    python -m dfinsta_pipeline.assessment_record record --state-root <dir> \
        --run-id port-440 --index work/440-clean/index --actor <who> --owner-token <tok>
    python -m dfinsta_pipeline.assessment_record show --state-root <dir> --run-id port-440

**It recomputes rather than adopts.** The admitting side reads the API surface it
admitted and computes the assessment itself; a caller may pass `--expect <digest>` and have
its own copy *checked*, and a disagreement is an error rather than a warning. This is
affordable because stage 4a is a pure sub-millisecond function — **measured** at 3,696 /
3,844 / 3,831 canonical bytes on 430 / 439 / 440, four candidates each, byte-identical across
`PYTHONHASHSEED` values. Where recomputation is genuinely expensive this project does the
other thing and says so (`replay_gate.resolve_admitted_build` fetches a receipt).

**What is admitted and what is derived, kept apart.** `api_surface.json` is *admitted* —
nothing in the ledger can attest it came from a real APK, and `tools/indexer/build_index.py`
must not become an admitted capability. The assessment document is *derived* from those bytes
and recorded as a ledger operation whose output ref pins it.

**The operation key is keyed on the decode's `content_hash`, not the surface file's own
digest**, because that file embeds `generated_at` and an absolute `decode_path` — so
re-indexing the same decode would otherwise mint a second, conflicting operation for an
identical result.

**`recorded_assessments_v1`** is the run-keyed row that makes the gate answerable at all.
`operation_claims` has no `run_id` column and is indexed by content hash, which is exactly why
`PortRunWorkflow`'s `phase-a-approval` cannot be answered — you need the spec to find the
operation that would give you the spec. The row carries *coordinates only*: the caller still
reaches the `ArtifactRef` through `require_completed_operation`, so nothing is bypassed.

**Proven on real 440 data.** `record` then `show` return the same operation key
`b38ecdbb…` and the same ref `cas://sha256/c422949d…` (3,831 bytes), and feeding both through
`derive_feature_gate_request` gives the byte-identical request hash
`fc575a36…` for gate `port-440-feature-assessment-gate`. That round trip — **a run id in,
read-only, and the same subject out** — is the property the whole gate rests on, and it is the
first time `feature_gate.py` has been reachable from anything but its own tests.

**Five defects its first tests found, all fixed and re-verified on real data:**

- **A re-index of the same decode was not idempotent** — the exact property the module claims.
  The operation key was correctly keyed on the decode's `content_hash`, and then the authority
  row compared *every* column including `api_surface_sha256`, undoing it one layer up. The row
  is now **stored whole and compared by identity** (`ASSESSMENT_IDENTITY_FIELDS`): a reader
  gets everything recorded, while sameness ignores fields that move when nothing meaningful
  has. Verified: a header with a different `generated_at` re-records idempotently.
- **A refused `record` left the ledger half-written** — two CAS blobs and a completed operation
  before the authority row refused. The conflict is fully decidable from values already
  computed, so it is now checked *before* anything is written. Verified: ops 1→1, rows 1→1,
  blobs 2→2 across a refusal.
- **The schema check was a value comparison**, so `1.0` and `true` passed (`1.0 == 1`,
  `True == 1`). Now guarded the way `hook_index.load` guards the same case.
- **`manifest_sha256` hashed the JSON-quoted text**, not the file, so it was a number stored
  under a name no human could reproduce with `sha256sum`. Now the file's own digest — which is
  why the operation key above reads `b38ecdbb…` rather than the `04d2931c…` first measured.
- **`policy_revision` on a missing file raised `FileNotFoundError`**, so a caller catching
  `AssessmentError` had a handler that looked complete and was not.

## …and it can now be answered

[`docs/ANSWERING_THE_FEATURE_GATE.md`](ANSWERING_THE_FEATURE_GATE.md) is the design note;
`FEATURE_ASSESSMENT_GATE` is registered, the second kind this client has ever carried. It
joined **only once its subject became reachable from a run id** — registering it before
`recorded_assessments_v1` existed would have been the `phase-a-approval` mistake with a new
name.

The human supplies ten values for a 4-candidate gate: a verdict, a rationale, and four
`(verdict, rationale)` pairs in a `--rulings` file. The candidate ids, **their order**, the
assessment digest, the policy revision and every hash are derived and not typeable.

`DerivedSubject` did **not** change — the earlier note overstated that. What changed:

- **`Answer.detail`**, with the rule that a kind which does not understand a detail must
  *refuse* it. Dropping it would submit a bare verdict while the human believes they ruled on
  something specific, and the receipt would say `accepted`.
- **`GateKind.payload`**, defaulting to the decision itself, so the replay gate is untouched.
- **The journal now records `payload_sha256`** — the gap the earlier note missed entirely, and
  the only one that can *silently substitute a human's rulings*: a `GateDecision` says nothing
  about what rode with it, so a resubmission would pair the recorded decision with a freshly
  built payload. An entry with no payload writes **no key at all**, so pre-change journals stay
  byte-identical and an upgrade cannot strand someone mid-answer.
- **The Temporal update id now covers the payload.** It was a digest of the decision alone, so
  two different dispositions documents under one decision shared an id: Temporal returns the
  first receipt, the second document is dropped, and the client prints `accepted True`.

**The safety property, stated once**: `_feature_rulings` iterates the **derived candidates and
looks each one up in the human's file**, never the reverse. A file that renames, drops or
invents a candidate is refused *by name* before anything is signed, and the emission order is
the request's, so the digest cannot depend on how someone ordered their editor. This matters
because `validate_submission` never re-reads the assessment blob — the derivation is
load-bearing precisely because the validator is not. The client also runs that validator over
its own submission before sending: if it cannot admit its own answer it refuses here, rather
than failing at a worker where the human cannot see why.

**Proven against the real 440 gate**, no Temporal server: subject `fc575a36…`, payload
`FeatureGateSubmissionV1`, dispositions `cas://sha256/781fa762…` at 674 bytes, validator
clean, and unknown-candidate / missing-ruling / malformed-detail each refused by name.

**And a defect its tests found, in the documented workflow itself.** `feature_gate`'s
`ValueError`s escaped the client's refusal channel, so the sequence written above ended in a
*traceback* — a human who filled in the template's verdicts and left its blank rationales got
`ValueError: Disposition for … has no rationale`, exit 1, where the contract is `refused: …`,
exit 2. That is precisely the habit this module's docstring says a gate client must not teach.
Fixed at both layers: `_feature_rulings` now checks the candidate verdict against
`feature_gate.VERDICTS` and the rationale requirement **by name**, and the contract calls are
wrapped so nothing escapes as a bare `ValueError`. Verified — unedited template, blank
rationale, the *gate's* vocabulary used for a candidate, a bogus verdict and an over-long
rationale all now produce refusals naming the candidate; `ignore` with no rationale and a
properly filled file are accepted.

**The template stays invalid as emitted, deliberately.** A skeleton that submitted cleanly
unedited would let someone approve four rulings they never made. Note the two vocabularies:
a **candidate** verdict is `block / offer_toggle / ignore / defer` while the **gate's** is
`approve / reject / defer`, and both appear on the same command line — so reaching for the
wrong one is the ordinary mistake, and it is refused naming the candidate and both lists.

## Something raises the gate now, and stage 4 runs end to end

`src/dfinsta_pipeline/feature_workflow.py`. Driven against the real recorded 440 assessment
through a time-skipping Temporal environment, no hand-holding — and, on 2026-08-05, against a
**live Temporal server** (`tests/integration/test_registered_feature_gate.py`), three processes,
six candidates, subject `bc794384…` re-derived by the client from a run id alone. That run costs
seconds rather than the replay harness's hour: every Activity here is ledger and store work.
The time-skipping figures below stand as recorded:

    gate raised          port-440-feature-assessment-gate
    published subject    9f9dc0eb0076acae087f8b1b95a9e197098736b54ca604622b719b536d24560a
    client re-derived    9f9dc0eb…   MATCH
    dispositions         cas://sha256/a4d3c6f2…  (691 bytes)
    workflow state       completed
    admitted == signed   True

A separate `@workflow.defn` rather than a branch of `PortRunWorkflow`, because extending an
existing definition inserts commands into a stream saved Histories already recorded.

**The design point that shaped it: the validator is a filter, the Activity is the authority.**
An update validator runs in the sandbox — no ledger, no content store — so it can check that
the decision binds this gate, the actor is allowed, the timestamps sit in the window, nothing
was submitted twice, and the reference is a *dispositions* reference rather than the
assessment's own. It **cannot** read the document the rulings live in.
`admit_feature_dispositions_activity` therefore re-derives the request from the ledger rather
than taking the Workflow's copy, fetches the document by the reference the human signed (which
re-verifies digest and size on read), and runs `validate_submission` over the three together.
A submission can pass the validator and still be refused there, non-retryably — that is the
design, not a gap.

**And the first tests for it found that the authority checked *less* than the filter.**
`validate_submission` never compared `decision.actor` to `request.allowed_actor`, and bound
`subject_sha256` alone — so "who may answer" and two of the three published hashes rested
entirely on a sandbox validator that is explicitly not the authority. Both against this
project's own precedent: `_validate_decision` and `AdmittedReplayVerificationGrantV1` compare
the actor, and the replay gate requires all three hashes. `FeatureGateRequestV1`'s own
docstring says `allowed_actor` sits in the derived bytes *precisely* so the admitting side can
verify it independently — it was in the bytes and nothing verified it. Both closed, with a
positive control on each so a refusal cannot pass for the wrong reason.

The rule worth carrying: **when a design splits a check into a cheap filter and a real
authority, the authority must check everything the filter checks.** For each clause, ask what
still holds if that layer were bypassed entirely.

## Operational hardening: what closed without a live Temporal run

A measured pass over the four deferred follow-ups reversed the plan. See
`docs/WORKFLOW_REGISTRATION_DESIGN.md` §3b-corrected for the full list.

**The roadmap had the road backwards.** A worker *kill* delivers no cancellation, leaves the
claim `pending`, and is recoverable — the run is **wedged, not burned**. A worker *stop* that
exhausts `graceful_shutdown_timeout` is the destructive one. Until cancellation is
non-destructive, **killing is the safe way to stop a worker mid-stage**, the inverse of the
rule `worker.py` carried. No incident sits behind the old wording; it traces to prospective
reasoning, and the plan doc still says hard worker loss "remains unproven".

**The destructive path is closed for one constant.** A replay stage can act on a cancellation
only through a heartbeat response or a local `WORKER_SHUTDOWN` — and none of them heartbeat —
so the window now sits above the longest stage budget (300 → 10,800 s). Pinned by a test that
*derives* that budget: the old `> 0` assertion passed happily at 300 seconds against a
10,800-second stage.

**Nothing could release a wedged claim** until `src/dfinsta_pipeline/claims.py`. Recovery
meant hand-written SQL against an append-only ledger under pressure. It reads through a
`mode=ro` ledger, prints the owner token, requires it typed back exactly, and refuses a
quarantined row.

**F2's cheap half**: public seams in `activities.py` replace the private coupling, with one
additive hunk and every proven body byte-identical. It surfaced a subtlety worth keeping — an
alias is the same object but **not the same binding**, so a test monkeypatching the private
name silently stopped patching anything and failed loudly.

**F1a**: the real-replay harness now *derives* its verification-gate ids through `replay_gate`
instead of restating them, so harness and Workflow cannot drift. The grant and gate ids had
been restated inline and pinned by nothing at all.

**Still open, and genuinely needing the real 340/430 run**: non-destructive cancellation
*within* the window (it rewrites a reviewed invariant), heartbeats, and the F2 extraction. One
ordering constraint is now load-bearing: **heartbeats must come last**, because heartbeating is
what opens the channel for server-originated cancellation — a workflow cancel, a timeout, or a
transient network failure while recording one — and every one lands in the handler that
quarantines.

**A terminal case outside all four**: once the verification grant is admitted, its five
`UNIQUE` columns plus the gate validator's timestamp window make that gate unrepeatable, so a
crash between admission and verify leaves a run that cannot be re-driven at all. Reached by
normal progress rather than by an accident.

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
| 10 Decision memory / cost ledger | **done** | `manifest_update.py`, `agent_cost.py` (the `agent_cost report` verdict reads `falling`) |
| 11 Report | not started | — |
| Durable orchestration | `ReplayRunWorkflow` **run for real on a live server**, 340 and 430 | `src/dfinsta_pipeline/` |

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

## The generaliser can now propose the kind that actually moved the number

2026-08-03. Building the write-back path from a proposal to `manifest/hooks.json` surfaced the
sharpest finding of the day: **`generalise.Proposal` could express `by_literal` and nothing
else**, so the automation covered every promotion except the only one that has ever mattered.
The 2 → 0 agent-count fall came from two **`by_anchor`** entries a human hand-wrote.

Fixed by making `by_anchor` expressible *and* proposable. `generalise_anchor(hook, hosts)`
calls the real `resolve.scan_for_anchor` — not a reimplementation, since a proposal derived by
different code than the resolver is a claim about a scan nobody ran — once per version, and
proposes only when the anchor selects **exactly one class and it is the known host on every
version**. The two-version corroboration and per-version exactness checks now apply to both
kinds through one `_require_corroboration`.

**Measured against the real 430 and 439 decodes:**

    install_settings_long_click            by_anchor  430 → LX/077K; (1 of 1), 439 → LX/0DnT; (1 of 1)
    install_settings_long_click_actionbar  by_anchor  430 → LX/06X7; (1 of 1), 439 → LX/0Di2; (1 of 1)
    tigon_url_block                        REFUSED    5 classes on 430, 7 on 439

The stage rediscovers from measurement alone exactly what the human wrote. **The third line is
the negative control** and matters as much as the first two: `tigon_url_block` stays `named`
because its anchor identifies the *site* without identifying the *class*.

Separately, running `manifest_patch.verify` against the real decodes showed that the one
genuine `by_literal` proposal on disk resolves to **nothing on either version** — empirically
confirming what `generalise`'s static `BLOCK_NOT_INDEXED` predicted. Committing it would have
made the hook escalate and moved the agent count not at all.

## The loop is closed: proposed, verified and committed from measurement

`src/dfinsta_pipeline/manifest_patch.py` is the step between a proposal and
`manifest/hooks.json`. Run for real against a temp copy of the manifest with **both** settings
hooks wound back to `by_agent`:

    generalise_anchor → plan → verify (real 430 + 439 decodes) → apply --confirm

    install_settings_long_click            439 → LX/0DnT;   430 → LX/077K;   WRITTEN
    install_settings_long_click_actionbar  439 → LX/0Di2;   430 → LX/06X7;   WRITTEN
    tigon_url_block                        7 classes / 5    [no_fingerprint] REFUSED

    byte-identical outside the two patched entries: True
    committed kinds match the shipped manifest's: 3 of 3
    repo manifest untouched (sha256 1e5f36dd…): True

**The pipeline rediscovered from measurement alone, and committed, exactly the two `by_anchor`
fingerprints a human hand-wrote** — the ones that took the agent count 2 → 0 — and refused the
hook whose anchor is not selective.

Two things about `verify` worth keeping. Its baseline is `before or expected`, because a
`by_agent` hook resolves to **nothing** and "the same as nothing" would pass for a patch that
also resolves to nothing. And a version with no decode comes back `unchecked`, which is not
`ok`: `apply` refuses on it, so a missing decode can never read as a verified one.

`by_anchor` also forced a distinction the module now makes explicit: a `by_anchor` fingerprint
carries **no scrubbable value at all** (it is the hook's own anchor, already in the manifest),
so `VALUE_FIELDS` is a per-kind table where an empty entry must carry a stated reason, and a
kind absent from the table is *refused* rather than defaulted to "carries none". "The rules
found nothing wrong" and "there was nothing to look at" are no longer the same silence.

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

1. ~~The release gate cannot consume the driver's own output.~~ **Done, and it runs end to
   end.** `finalize.py` invokes its `--final-verifier` with `--apktool-jar --apksigner
   --require-signature --expected-certificate-sha256`; only the 430-shaped `verify_apk.py`
   accepted those, so there was **no target-neutral post-signing verification** and the 440
   build was aligned, signed and certificate-checked by hand. `verify_build.py` now takes all
   four, carries its own `signature_context` (a deliberate second implementation — the two
   verifiers are meant to be independent and are invoked as bare scripts), and ANDs `passed`
   with `verified and approved_signer`. *Verified but unexpected* is the dangerous case: a
   correctly signed APK signed by the wrong key.

   The catch that made it fail twice before it worked: the graft strips every signature
   artifact, so an **unsigned** build must carry none — but the release gate runs the same
   verifier **after** apksigner has written `META-INF/*.SF` and `*.RSA`, where those entries
   are the point. `verify(..., expect_signed=...)`, inferred from `--apksigner` being present,
   flips exactly two assertions: signature entries stop counting as `added_entries`, and
   `passed` requires at least one instead of requiring none. Everything else added is still
   rejected, so the relaxation is scoped to the files apksigner is known to write.

   `finalize.py` on the real 440 build now produces `work/440-clean/dfinsta_440_release.apk`,
   SHA-256 `c9e063e5…` — **byte-identical to the APK signed by hand earlier**, which is the
   cross-check that the gate does what the manual sequence did.

   One defect in the new code, found by its own tests and fixed: the certificate pin was
   guarded by `if args.expected_certificate_sha256:`, so an **empty** value skipped the hex
   check, reached `signature_context` as "no expectation", and reported `approved_signer: true`
   for any key at all. An unset shell variable expanding to `""` would have turned the pin off
   while the command line still said it was on. `is not None` closes it.

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

3. **Give the feature gate a producer.** Design note:
   [`docs/STAGE_4_PRODUCER_DESIGN.md`](STAGE_4_PRODUCER_DESIGN.md), written 2026-08-03 against
   the real tree with every claim measured. **It corrects the framing this file used to
   carry.** "Stage 4a computes an assessment in the driver world" was generous: *nothing*
   computes one. `driver.py` never imports `assessment` — the only importer in the tree is
   `tests/test_assessment.py` — and the `assessments`, `assess()` and `Assessment` that do
   appear in the driver are the **proposal** ones from `.proposals`, so grepping the driver for
   stage 4a finds four hits and none of them is stage 4a.

   Nor is the obstacle authority: the driver *could* write the ledger, and nothing checks for a
   Temporal context. The real obstacles are that the driver has no run identity and no state
   root, and that a second unsupervised writer gets the `owner_token` ceremony without the
   attempt-adoption property it exists for.

   The recommendation is a small admission program that admits the API surface into CAS,
   **recomputes** the assessment from it, and records it as a ledger operation — because
   recomputation here is free and deterministic (**measured**: 0.00 s, byte-identical across
   hash seeds and across 430/439/440), and adopting bytes when recomputation is free spends the
   one thing that separates this gate from a rubber stamp.

   Two traps the note pins down. **The gate is not answerable by `submission.py` today even if
   everything else lands**: `submit_answer` sends a bare `GateDecision` while this gate's payload
   carries a dispositions `ArtifactRef`, and `Answer`/`DerivedSubject` have nowhere to put
   per-candidate rulings. And **`validate_submission` never reads the assessment blob**, so the
   derivation is load-bearing precisely because the validator is not.

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
              2672 tests, 1 expected skip
tool suites   cd tools/<name>/tests && PYTHONPATH=<repo>/src:<repo> python -m unittest discover -s . -t .
              indexer 46, resolver 8, port_430 45, reconstruction 15, release 6,
              device_validation 22. Discovery from the repository root does NOT work:
              those directories are not importable packages.
registered    temporal server start-dev            (localhost:7233, UI on :8233)
replay run    .venv/bin/python -m tests.integration.test_registered_replay_harness \
                  --targets 340 --run-root /abs/path/outside/the/repo
              About 55 min for 340 and 75 for 430, and roughly 10 GB of workspace each.
              Refuses a run root that exists, and refuses to start over a workflow id
              that is still open, naming the terminate command.
assess a run  add --state-root/--assessment-run-id/--actor/--owner-token to the driver
manifest vs   .venv/bin/python -m dfinsta_pipeline.rulings --audit
the app       exit 1 if either direction disagrees
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

## Loose ends, written down so they are not rediscovered

Recorded 2026-08-04. Ordered by what would be forgotten first, not by size.

**1. ~~THE GATE'S RULINGS HAVE NO CONSUMER.~~ Closed 2026-08-04**
(`src/dfinsta_pipeline/rulings.py`, `manifest/rulings.jsonl`,
`admitted_dispositions_v1`). The chain runs: gate raised → human rules → workflow admits →
**recorded by run id** → consumer reads it back → `block`/`offer_toggle` land in
`semantic_deps` → stage 4a stops proposing them. Verified by re-running the assessment after
the edit, which is the check that matters: the ruling changes something real rather than
recording a decision that changes nothing. `ignore` suppresses through the ruling store,
scoped to the policy revision; `defer` correctly returns; a suppressed candidate is *reported*
in a `settled` array rather than silently vanishing.

Three things the build surfaced, all fixed:

- **The module's own docstring claimed a check nobody had written** — "refuses to call a ruling
  applied until the built APK can be shown to carry it", with nothing inspecting a DEX or the
  source. `unenforced_endpoints` now reads the app's `throwIfBlocked` and reports every
  declared block the code does not test. It returns nothing today and catches a planted one.
- **`suppressed_candidates` had zero callers** — the same disconnection, inside the module
  written to fix it. Wiring it required the ruling store's digest to join
  `assessment_record.operation_input`, or a store that grew since the last record would compute
  the same operation key with a different document and refuse by naming the wrong cause.
- **`_hook_for` keyed on file order**, taking the first hook with any URI-path dep — which is
  `tigon_url_block` today and would silently become an endpoint-*rewriting* hook if the
  manifest were reordered. Now keys on the manifest's own `"strategy": "url_block"`.

**What is still a human's job, and why.** The guard block is ten instructions and looks
generable. It is emitted for review because the match method is not derivable (every literal
ending `/` uses `endsWith` — 13 of 13 — but that records a judgement about whether the live
path carries a suffix, and guessing wrong means the rule never fires while everything still
passes), because no preference key is derivable from its endpoint (`/feed/reels_tray/` is
`disable_stories`), and because a *new* toggle is five coordinated edits of which the
index-to-key dispatch fails silently — a row that renders, animates and writes nothing. For the
four candidates actually on the table a new key is probably wrong anyway: they belong under the
existing `disable_feed` and `disable_reels`.

**Superseded, kept for the reasoning:** `grep -rn "offer_toggle" src/ tools/` outside
`feature_gate.py`, `assessment.py` and `submission.py` returns **nothing**. A human can now
rule "block `feed/timeline_stream/`", the Workflow admits it, the ledger records the decision —
and nothing reads the verdict. This is the same shape as the bug that consumed most of
2026-08-03 (`feature_gate.py` "imported by nothing but its own tests"), one link further along
the chain, and it is the loose end most likely to be missed **because everything looks
finished**: the workflow completes, the suite is green, the roadmap says done.

What a consumer has to do, from the tree: a blocked endpoint is a literal in the DFInsta custom
code (`dfinsta_source_1.3/newCode/com/dfinstagram/hooks.smali`, gated by a preference key) *and*
an entry in a hook's `semantic_deps` in `manifest/hooks.json` — the latter is what
`assessment.blocked_endpoints` reads, so without it stage 4a keeps proposing a candidate the
human already ruled on. `offer_toggle` additionally needs a preference and a row in the
settings dialog, and it is the *default* shape for anything judged addictive per the project's
feature policy, so a `block` that is not toggleable is a policy violation rather than a
shortcut. `manifest_patch.py` is the closest precedent for the manifest half.

**2. ~~The verification grant is single-shot.~~ Closed 2026-08-04**
(`resolve_replay_verification_grant_activity`, `Ledger.admitted_replay_verification_resumption`,
`ReplayVerificationResumptionV1`). The trap was a closed loop between two individually correct
checks: the five `UNIQUE` columns on `admitted_replay_verification_grants_v1` refuse a
*different* decision for the run, and the Workflow validator's `decision_time >= gate_time`
window refuses the journalled decision `submission.py` resubmits verbatim — which it resubmits
verbatim for a good reason, since re-assembling would re-timestamp and a decision whose
`issued_at` moved is a different decision. So after a re-drive neither door opened.

Fixed by not asking twice rather than by weakening either check. The Workflow resolves the
recorded answer before raising the gate and, on a hit, verifies against the recorded grant. The
resumption carries `decision_id` as well as the grant handle, so a resumed run's result still
names the human who authorised it instead of reporting a success nobody approved. Both doors are
now pinned: `test_phase_b_verification_grant`'s identity collision and
`test_phase_b_replay_workflow.test_a_decision_from_a_superseded_gate_is_refused`.

**2b. Two findings from the first run through the registered Workflow** (2026-08-04, and see
`docs/WORKFLOW_REGISTRATION_DESIGN.md` §3c-measured):

- **The worker CLI could not run a real stage.** `run_worker` called `configure_runtime(state_root)`
  with no `source_root` and no `executor_paths`, both of which three stages require. Fourteen
  Activities registered, none runnable, and every registration test green. Fixed with
  `--source-root`, `--executor-path SHA256=PATH` and `--attempts-root`.
- **A running stage blocks the worker's event loop**, so a query — and therefore a heartbeat —
  cannot be served while a stage runs. This contradicts §3b-corrected's argument that F4 is
  cheap. **Still open**: F4 now needs the synchronous tree capture moved off the loop first, or
  measured heartbeat gaps the size of a full capture.

**3. ~~Stage 3 has the same disconnection stage 4a had.~~ Re-derived and split, 2026-08-04.**
Both halves of the original sentence were true and they were about **different modules**. It
read: "`surface_diff.py` is a standalone CLI the driver never invokes. Nothing schedules the
thing that decides *what to assess*." The second sentence names the wrong module, and fixing
what it named would have left the real gap open while the list said it was closed.

- **The live gap, now closed.** What decides what to assess is `assessment.find_groupings`,
  reached through `assessment_record.record` — which had **zero callers** outside its own
  `main`. Every production reference to that module is `resolve_with`, the read side. The real
  440 assessment exists because a human typed the record command after the driver finished. The
  driver now has an `assess` stage between `index` and `resolve`, taking all four of
  `--state-root`, `--assessment-run-id`, `--actor`, `--owner-token` or none, and skipping
  loudly otherwise. Proven against the real 440 index: one command records an assessment whose
  gate subject the submission client re-derives from the run id alone.
- **Still open, and separate.** `surface_diff.py` is invoked by nothing at all — not the driver,
  not a script, not a documented command line; its only mention outside its own tests is prose
  in two docstrings. It is **not** on the stage 4 path: `assessment.document()` takes a
  `HookIndex` and the manifest and nothing else, and `surface_diff.Candidate.to_dict()` emits no
  `candidate_id`, which `assessment.candidate_ids` requires. The two stages are siblings on the
  same `api_surface.json`. Wiring it into the driver is optional and would need a
  `--baseline-index`; giving its output a consumer is a separate design question.
- **`claims.py` has zero importers and zero tests.** The recovery path for a wedged claim,
  marked done in the roadmap, has never been exercised.

**Already banked — do not redo it.** 440's ledger carries identity claims for all seven hooks,
four of them passing, so the 441 differential will be four comparisons wide against 439's two.
The three that stay inconclusive are the two dormant Reels variants and the legacy action-bar
hook, which are known not to execute on this device and configuration — inconclusive by nature,
and correct. There is nothing to gain from re-measuring 440 on the phone.

**Three defects in shipped source, found while mapping what a ruling must change.** None was
caused by that work. Two are closed; the third is scoped and deliberately left.

- **Still open, and confined to 1.3.** `dfinsta_source_1.3` ships a half-declared toggle:
  `disable_suggested_posts` has a public id, an istring, an `isCachedFeature` entry and a guard
  — and **no row in `instander_settings.xml` and no listener registration**. Because
  `getBoolTrueEz` defaults *true*, suggested-post filtering is permanently on and
  un-toggleable, and nothing reports it. `dfinsta_source_1.3/CLAUDE.md` lists it as one of six
  toggles. **Scoped 2026-08-04**: the string appears in no other source tree —
  `dfinsta_source_430`, `dfinsta_source_439` and `dfinsta_source_1.4.1` do not contain it — so
  the pipeline never reaches it. Fixing it means editing the CRLF-dirty 1.3 tree, which is
  never staged, so it stays as a documented inaccuracy in a legacy tree rather than a pipeline
  defect. It remains the exact failure mode a *new* toggle must avoid.
- **~~`manifest/hooks.json` under-declares `throwIfBlocked` by one literal.~~ Closed 2026-08-04.**
  `throwIfBlocked` tests six endpoints and `tigon_url_block.semantic_deps` listed five.
  `/clips/discover` was declared on `replace_reels_discover_endpoint` as `clips/discover/`, and
  containment fails both ways over the leading and trailing slashes, so `assessment.is_blocked`
  saw it under neither hook and stage 4a would have proposed blocking what the app already
  blocks. `rulings.undeclared_endpoints` is the missing direction, `rulings.audit` runs both,
  and `python -m dfinsta_pipeline.rulings --audit` is the operator entry point. The manifest now
  declares it and the audit is clean in both directions.
  Two things surfaced with it: `unenforced_endpoints` had **no production caller** (only tests),
  and `rulings.py` had a `main()` with **no `__main__` guard and no console script**, so
  `python -m dfinsta_pipeline.rulings` imported the module, ran nothing and exited 0.
- **~~`tools/port_430/verify_apk.py`'s exact-symbol check cannot pass on a probe-instrumented
  build.~~ Closed 2026-08-04.** `custom_symbols == set(REQUIRED_CUSTOM_SYMBOLS)` could not hold
  on a build that also carries `Lcom/dfinstagram/probe;` — the one kind of build that can
  attribute hook execution. Now reports `unexpected_custom_symbols` against a caller-supplied
  `--allow-custom-symbol`, defaulting to empty so an unexpected class still fails. The allowance
  is the caller's, per `verify_build.py`'s rule that every build-specific fact is supplied by
  whoever knows it.

**~~A design note for when a ruling's guard is written~~ — built 2026-08-05, as designed.**
`verify_build.py` gained `--required-strings`, a caller-supplied JSON array beside
`--host-hooks`; `rulings.required_build_strings` derives it from the url-block hook's
`semantic_deps`; the driver writes `required-strings.json` and passes it through `build.py`.
`REQUIRED_CUSTOM_SYMBOLS` is untouched, so the rule that every version-specific fact comes from
the caller still holds.

Three states kept apart, because two of them look alike: **not supplied** reports
`required_strings: null`, so a report cannot be read as "nothing was missing" when the question
was never asked; **supplied empty** is refused, being a caller asking to prove nothing — the
same shape as the empty `--expected-certificate-sha256` that silently turned the signing pin
off; **supplied** contributes to `passed`.

Searched in the *custom* DEX only. A stock DEX may legitimately carry the same literal — it is
Instagram's own API path — so searching the archive would pass a build whose custom code never
gained the guard. Confirmed against the shipped 440 release: all six manifest endpoints,
including the `/clips/discover` added the same day, are present as bytes in `classes21.dex`.
This is the only check that reaches the artifact; `unenforced_endpoints` and
`undeclared_endpoints` both read text.

**Small, batchable:**

- `work/by-anchor-proposal.json` is in a bespoke schema `read_proposals` refuses. The producer
  regenerates the same content from measurement in ~14 s, so this is a one-off data migration
  nobody needs.
- `generalise.forbidden_reason` does not refuse a path-shaped literal.
  **Measured 2026-08-04, and the note had it backwards.** `generalise` is right to allow one:
  `clips/discover/` is a path-shaped literal and is the fingerprint three shipped hooks use.
  What is actually wrong is the downstream guard being *over*-broad —
  `manifest_patch.forbidden_in_value('/feed/timeline/')` refuses it as
  `contains an absolute path. It names one machine's workspace`, which is false, and so would
  `/data/user/0/com.instagram.android/`. No live break: every fingerprint literal in the
  shipped manifest is relative (`clips/...`), `semantic_deps` is not scrubbed by that rule, and
  provenance already requires a literal to have been observed inside the host class, so a
  build-machine path cannot reach it anyway. But the first leading-slash fingerprint the
  generaliser proposes will be refused with a reason that is not true. Tightening a refusal
  rule is a safety change and was deliberately not made under time pressure; the fix is to
  require a recognisable filesystem root rather than a leading slash.
- ~~`manifest_patch.plan` reads the manifest as raw JSON rather than through `load_manifest`, so
  an unvalidated `kind` reaches the strength comparison.~~ **Examined 2026-08-04; not a defect,
  and the change was tried and reverted.** Validating through `load_manifest` turns an
  unrankable kind into a `PatchError` where it is now a `Refusal` inside the returned `Patch` —
  the operator gets an exception instead of a rendered reason, `writable` is never consulted,
  and `test_a_host_kind_this_stage_cannot_rank_is_refused_rather_than_guessed` asserts a shape
  the module would no longer produce. The kind is refused either way; as data is better. The
  reasoning is now a comment at the read site so it is not re-tried.
- `Selection` is literal-shaped (`literals`, and a `reason` that reads "the anchor" as a
  fallback) now that `by_anchor` selections carry none.

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

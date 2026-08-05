# DFInsta Redux Roadmap

**This is the single roadmap.** It supersedes the "Immediate Next Actions" list in
`docs/SESSION_HANDOFF.md`, the "Immediate Roadmap" in `docs/FUTURE_WORK.md`, and the
priority order in `HANDOVER.md` section 6. Those are retained as history; when they
disagree with this file, this file wins. Do not start a fourth list.

Last updated 2026-08-05 (sixth pass: the registered Workflow ran a real port, both targets,
on a live Temporal server — and found in its first minutes that the worker could not run one).

## End goal

Given a new stock Instagram APK, produce a working DFInsta build with minimal human
effort: discover what changed, decide what needs blocking, re-map the hooks onto the new
obfuscated code, apply, build, sign, and verify — with humans deciding policy at durable
gates rather than doing the mechanical work.

## Three machines, three very different states

| | What it does | State |
|---|---|---|
| **Execute** a port | apply/build/graft/verify/sign/orchestrate | largely complete |
| **Produce** a port | re-map hook intent onto a new obfuscated decode | **7 of 7 hooks mechanical on 430, 439 and 440** — no `by_agent` fingerprint remains, and 440 arrived after they were written |
| **Decide** what to port | find new features, judge addictiveness, present evidence at a gate | **complete end to end**: assessment recorded, gate raised, answered per candidate, and the ruling consumed into the manifest. What a `block` needs in the app's own smali is emitted for review, deliberately not generated |

The remaining work is concentrated in one place: judging a new feature and presenting it at a
durable gate. The second half of that sentence used to be "closing the loop so the agents that
propose hooks run inside the pipeline" — 440 closed it by needing no agent at all.

**The lesson that reshaped all three machines.** Four separate failures — the 340
`minshop` substitution, the 430 settings hook, the 439 action-bar hook, and a verifier
searching for a string DEX does not store — were not four bugs. They were one: *something
present and never reached*. Adding a check per incident would always have been one version
behind, so instead every hook now announces its own execution. On the first run carrying
that, two more hooks turned out never to run. Prefer instrumenting an invariant over
enumerating its violations.

## 1. Execute — largely complete

- [x] Target-neutral intent + per-target resolution split
- [x] Anchored operations with exact cardinality, idempotence, register contracts
- [x] Stock DEX graft preserving `resources.arsc`, manifest and `res/` byte-for-byte
- [x] Static verification: DEX topology, custom-descriptor set, forbidden symbols, archive preservation
- [x] Ledger authority, CAS, fail-closed adoption/quarantine
- [x] Durable Temporal Workflow with the replay chain registered and a mid-run verification gate
- [x] Release signing gate with a pinned certificate
- [x] Device validation harness bound to installed-artifact identity
- [x] 340 real replay re-proven at current HEAD (65 assertions, 59 operation proofs).
      Note the final APK hash differs from the historical proof and that is EXPECTED:
      `apktool_full_rebuild` embeds build-time ZIP timestamps so it is not bit-reproducible,
      while `stock_dex_graft` keeps 16,399 stock timestamps and only rewrites what it writes.
      Verify rebuilds semantically, never by hash equality against a stored value.
- [x] **Real 340/430 run through the registered Workflow** (2026-08-04). Both completed on a
      live server: 8 and 9 activities scheduled, History at 64,563 and 62,261 bytes of the
      256 KB budget, verification receipts reporting success. Registration had been proven by
      unit and time-skipping tests only, and the real run found in its first minutes that the
      worker CLI could supply neither `source_root` nor `executor_paths` — so all fourteen
      registered Activities hosted a Workflow that could not run one real stage.
- [x] **Follow-ups F1 and F2 folded in.** F1's identifier pins are checked against a run rather
      than against themselves; F2's extraction is done and its three stopgap aliases deleted,
      with the refactor confirmed byte-identical by re-deriving both runs' bound gate subjects.
- [x] **The destructive cancellation path is closed by construction** —
      `DEFAULT_GRACEFUL_SHUTDOWN_SECONDS` is now 10,800, above the longest stage budget, and
      derived-and-pinned by a test rather than asserted `> 0`. A replay stage can act on a
      cancellation only through a heartbeat response or a local `WORKER_SHUTDOWN` after that
      window, and **no replay stage heartbeats** — so with the window longer than any stage,
      the cancellation that quarantines cannot arrive mid-stage.
- [x] **A wedged run can be recovered** (`src/dfinsta_pipeline/claims.py`). Nothing in the repo
      could release a stale claim; recovery meant hand-written SQL against an append-only
      ledger. `python -m dfinsta_pipeline.claims --state-root <dir> [<key>] [--release <owner>]`
      reads through a `mode=ro` ledger, requires the owner token to be stated exactly, and
      refuses a quarantined row.
      **Tested 2026-08-04, and it had never been.** It was marked done here with zero
      importers *and* zero tests — a recovery path nobody had exercised. 38 tests now, 17 of
      18 mutations caught (the eighteenth provably equivalent), pinning refusals by exact
      message because four separate rules raise the same type and one of them contains the
      other's keyword. One real defect found and fixed: `--release ""` was falsy, so the whole
      branch was skipped and the tool printed the claim and exited 0 — the one wrong owner
      token that produced neither a release nor a refusal, and what
      `--release "$OWNER"` expands to when `OWNER` is unset.
- [x] **Non-destructive cancellation *within* the window** (2026-08-05). A cancelled operation
      releases its claim so a later attempt can adopt it, *unless* its subprocess could not be
      shown to have exited — `executor.ProcessNotReaped` is the signal and
      `activities._releasable` the rule. Audited first: nothing a stage writes after its
      workspace exists is shared, the release blanks the owner so a zombie's late
      `record_effect` is refused, CAS publication is atomic, and `claims.py` already released
      exactly this state by hand. Only a *cancellation* releases; an ordinary post-workspace
      failure still quarantines, because it is usually deterministic and failing closed on it
      is the point.
- [x] **Heartbeats in the replay Activities** (2026-08-05). Every stage reports every 30 s
      from the event loop; `heartbeat_timeout` is 5 minutes, so worker loss is detected in
      minutes rather than at `start_to_close` expiry — three hours for verify. Measured on a
      real 340 port: worst gap 30.9 s in decode and exactly 30.0 s in apply, build and verify,
      with the run unaffected (65 assertions, History in budget, zero activity failures).

      **Both prerequisites were closed the same day, in the order this list demanded**, and
      each was measured before the next was attempted: cancellation stopped being destructive,
      then the decoded-tree walks moved off the loop. Two claims that used to live here were
      retracted rather than quietly dropped — that a wrapper heartbeater would work "because
      every long operation inside a stage yields the event loop" (the decode stage contains no
      `asyncio.to_thread` at all), and that the graceful window "must be raised again before
      heartbeats are added" (its premise was quarantine-on-cancellation, which no longer
      happens; and the window governs `WORKER_SHUTDOWN`, not server-originated cancellation).
      See `docs/WORKFLOW_REGISTRATION_DESIGN.md` §3g.

## 2. Produce — the gap

- [x] Establish the fingerprint precedence that actually holds across versions:
      stable named types → string literals → structural shape → numeric constants;
      **never the obfuscated descriptor** (names are recycled — see `docs/PORT_439_RECON.md`)
- [x] One hook re-mapped by hand end to end, device-verified (`LX/06X7` action-bar variant)
- [x] **Blind holdout: can a mapper rediscover the anchors unaided? YES.**
      Easy/medium sites (app context, Tigon, 3 Reels literals) demonstrated.
      Hard settings site found by two provably-uncontaminated mappers from an
      isolated stock decode, including the runtime selector between the two
      action-bar variants. Verified from agent transcripts, not from denials.
      Working technique: enter at `UserDetailFragment->AP1`, follow the runtime
      branch to each delegate, pin the control by **drawable id** since string
      ids are unresolvable under sparse resources, prove own-profile via the
      `A1K -> A2W -> A15()` chain.
- [x] **Version-independent Hook Manifest + pattern resolver** (`manifest/hooks.json`,
      `src/dfinsta_pipeline/hook_manifest.py`). Anchors are patterns with `<name:kind>`
      captures; payloads template off them. **5 of 7 hooks now resolve MECHANICALLY on
      both 430 and 439**, reproducing the hand-authored anchors and payloads exactly and
      picking up `v0`->`v4` and `LX/05ez;`->`LX/03AS;` automatically. The 2 that do not
      are the `ui`-tier settings hooks, declared `kind: by_agent` — the tier taxonomy
      predicted exactly that split.
- [x] Mechanical validator for agent candidates (`tools/resolver/validate_candidates.py`),
      with mutation tests proving each check bites
- [x] **Resolve stage** (`src/dfinsta_pipeline/resolve.py`) — the first caller of the engine.
      Host search runs off the Index, so **5 of 7 hooks resolve with no human input at all on
      both 430 and 439, host discovery included**. Two ordering rules are load-bearing:
      `already_applied` outranks `resolved` (on a re-run the real host carries our marker
      while the decoys still match, so ranking resolved first patches a second, wrong class
      every time), and a marker at the wrong count is a hard stop above both.
- [x] **`by_literal` needed a real discriminator.** Measured: `clips/discover/` alone appears
      in 5 classes per version and the anchor matches cleanly in 3 of them — analytics maps
      and prefetch allowlists load the same string. Only the class building the outgoing
      request path carries all three Reels endpoints: exactly one class on each version. Hence
      `co_literals`. If a version splits them the intersection empties and the stage escalates.
- [x] **Proposal pipeline** (`src/dfinsta_pipeline/proposals.py`) — k proposers → agreement by
      content → mechanical validator → adversarial verifier, each producing evidence from a
      producer that is not the proposer. Verified end to end against the real 430 decode:
      2-of-3 agreement accepted; agreement on an answer the validator refuses is refused;
      one proposer answering three times does not manufacture consensus.
- [x] **Evidence ledger** (`src/dfinsta_pipeline/evidence.py`) — gates on absent evidence.
      Confidence is recorded and never read (a test varies it across its whole range and pins
      that no verdict moves). Evidence has phases: four kinds are derivable from the decode
      and gate the apply, three need the built APK and gate the release — collapsing them
      would make the pre-apply gate unsatisfiable. Retry-to-green is flagged, including from
      `inconclusive`, which is the Reels probe's own bad state.
- [x] **Runtime probe runner** (`src/dfinsta_pipeline/probes.py`) — the ledger now reaches the
      phone. Measured on the installed 439 build: `tigon_url_block` gives **10 canonical blocks
      with the toggles on and 0 with them off**, so `runtime_probe` is `passed` on real evidence
      rather than absent. Counting is canonical, not `grep -c`: every live block also emits a
      `NETWORK_FAILURE_REASON` field with the same text, and `aware_trace` re-narrates past
      events at a later cold start. The runner refuses to record a measurement it could not
      take — locked screen, app not foreground, surface control absent — because a zero the
      phone never had a chance to produce is not an observation.
- [x] **Measurements become claims** (`src/dfinsta_pipeline/record_runtime.py`) — stage 9's
      other half. `probes.main` writes measurements; the ledger wants claims, and the bridge was
      a throwaway script both times a version's evidence was recorded. Three modes matching the
      three probe shapes, with the two toggle states of a delta accumulating in a store because
      a human moves the toggle between them — and an *unusable* half is never stored, so it
      cannot be paired later as though it were a measurement. Running it against the phone found
      that **Instagram 440 renamed every bottom-navigation resource id**, so surface navigation
      had silently stopped working while still reporting "its site may not have been reached";
      selectors are now resource-id *then* content-desc, tried separately.
- [x] **Per-hook runtime identity** (`src/dfinsta_pipeline/runtime_identity.py`) — the
      generalisation, and the one that retires a whole class of bug rather than patching
      an instance of it. Four failures in this project were the same failure: a patch
      present and never run (340 `minshop`, 430 settings, 439 action-bar, and a verifier
      searching for a string DEX does not store). Each was found by a different ad-hoc
      investigation, none by a standing check.

      Every payload now calls a no-argument method named after its hook. The method NAME
      is the identity, so the call needs **no registers** and can never force a `.locals`
      change or clobber a live one. `load_manifest` refuses an uninstrumented active hook,
      so the next hook cannot forget.

      This fixes attribution at both ends at once. Statically, `host_hook_map` now emits a
      distinct `(Lcom/dfinstagram/probe;, h_<hook_id>)` pair per hook, so the verifier can
      tell three Reels hooks in one DEX apart. At runtime one line per hook per process
      says the site executed — independent of toggles, of navigating to a surface, and of
      the feature producing any observable effect. A hook that does not report is
      `inconclusive`, never `failed`: its site may simply not have been exercised, and
      that is a different thing from being inert.
- [x] **Differential vs N−1** (`src/dfinsta_pipeline/differential.py`) — the last evidence
      kind with a producer. Judges a hook's runtime result on version N against version
      N−1's, and takes apart the case the kind's own description names: a hook that passed
      before and shows nothing now is a *port regression* only when the current capture can
      be shown to have been able to see the signal. The three probe shapes are ranked by how
      well each answers that — identity first, because `DFInstaProbe: <hook_id>` is the one
      signal DFInsta emits itself and so cannot rot when Instagram renames a log line. A
      baseline that did not pass yields `inconclusive`, never `passed`: with no baseline pass
      there is no *where* for this version to fail, and a first port must be waived by a
      human rather than dissolving into a pass. Comparing a version with itself is refused
      outright — two builds of one version are byte-identical here, so it would enter the
      ledger as evidence that a comparison happened.
- [ ] Re-measure the five previously-inconclusive hooks now that they are individually
      attributable, and settle whether `install_settings_long_click_actionbar` is dead on 439
- [x] **Per-version Index** (`tools/indexer/build_index.py`): 181,421 classes in 3.4s,
      68 MB out, byte-identical across job counts. Confirmed API-path literals are the
      strongest fingerprint (93.9% survive 430->439 vs 89.3% of stable named types).
      **Overturned an earlier claim**: drawable *ids* are NOT stable — of 11,737 names
      present in both versions only 103 keep their id, 99.1% are renumbered. Anchor on
      the drawable NAME and re-resolve the id per version.
- [x] Feed the Index into host search so `by_literal` hooks resolve without a full rescan
      (`hook_index.py` + `resolve.py`): `descriptors_with_all_literals` intersects the
      per-version index, so a host is found by fingerprint instead of by rescanning a decode.
- [x] Teach a Resolve-stage caller about `Resolution.already_applied` (`resolve.py`).
      Outcome precedence is CONFLICT > ALREADY_APPLIED > RESOLVED > AMBIGUOUS > UNRESOLVED
      > NOT_FOUND > NEEDS_AGENT, so a normal re-run reads as applied rather than as failure.
- [x] Candidate anchors must be *proposals* only — the deterministic spine still applies and
      verifies (`proposals.py`). A proposal is compared by what it would DO, not by its text,
      and is handed to a verifier that sees the claim and never the rationale, defaulting to
      refuted. That verifier refuted a hook that had already shipped.
- [x] **Measure coverage on 439: 7/7 hook operations resolved by agents, zero human mapping.**
      Built, structurally verified, signed, installed, and CONFIRMED WORKING on device
      2026-08-01: 23 canonical block exceptions with the stack showing
      `com.dfinstagram.hooks.throwIfBlocked` <- `TigonServiceLayer.startRequest`, and the
      settings dialog opening with all five toggles. Every obfuscated host had moved
      (`LX/077K`->`LX/0DnT`, `LX/06X7`->`LX/0Di2`, `LX/05t2`->`LX/04tC`), and the custom
      code needed `classes21.dex` because 439 already ships `classes20`.
- [x] Turn the ad-hoc mapping workflow into a reusable resolver that emits candidates +
      evidence. The full chain has now run: k=3 blind proposers, agreement, the mechanical
      validator and two adversarial verifiers. On 439 for `install_settings_long_click` it gave
      2-of-3 on the *host* and 1-of-3 by *effect*, and both verifiers failed to refute — so one
      required signal passed and the other did not, and the gate correctly stayed shut. The
      diagnosis was that the prompt asked for a whole patch when only the host varies; there is
      now a narrowed host-discovery question (`host_prompt` / `HOST_SCHEMA` /
      `proposals.host_agreement`) whose agreement is over a single field.
- [x] **7 of 7 hooks resolve mechanically on 430 and 439, hosts included.** The guard was
      device-tested (own profile opens the dialog, another user's does nothing), and
      `kind: "by_anchor"` then retired the last two `by_agent` fingerprints — the anchor
      itself selects exactly one class per decode, measured on both versions and cross-checked
      by a full unprefiltered scan. The manifest now has 3 `by_literal`, 2 `named`,
      2 `by_anchor` and **no `by_agent`**, so the next port costs 0 agent invocations where
      439 cost 2. The claim that agent cost *falls* still needs a real second version.

### What per-hook identity found on its first run

Built, signed, installed on 439 and measured. Across app launch, Reels, Explore, the
profile and four Reels swipes — with the toggles blocking **and** with them all off so
Reels worked normally — exactly three hooks announced execution: `set_app_context`,
`tigon_url_block`, `replace_reels_stream_endpoint`.

**`replace_reels_discover_endpoint` and `replace_reels_homecoming_endpoint` never ran in
any state** — and reading `LX/04tC` shows they are **dormant, not dead**:

- `clips/homecoming/` and `clips/discover/stream/` are in the SAME method `A0A`, chosen by a
  runtime branch on `clips_viewer_homecoming_fyp` plus MobileConfig `0x810b9a007b4194` and
  `0x810af400383ea5`. This account is routed to stream. The class names two viewer surfaces
  (`clips_viewer_clips_tab`, `clips_viewer_homecoming_fyp`), so homecoming is a
  server-selected Reels surface — keep the hook, it is coverage for a config flip exactly
  like the two action-bar variants.
- `clips/discover/` is in a different method `A08` whose context carries
  `android_purge_26_q2_ClipsDiscoverApiUtil_createRequestTask`: Instagram has flagged that
  path for removal in 2026 Q2, so it is probably legacy.

**The general rule this establishes: "never executed on my device" is NOT "dead."** Instagram
is heavily server-config-driven, so a hook can be correct and simply dormant. Retiring one
because a single account never triggered it would drop coverage a config flip restores
overnight. A non-reporting hook stays `inconclusive`, and the useful next step is to find the
*selector* that routes past it — never to conclude from the silence.

This was invisible before because all three shared one signal with no endpoint qualifier
and one toggle governs all three, so the group moved 3 → 0 together and looked healthy —
the 340 `minshop` shape exactly.

## 3. Decide — in progress

**Stage 4a is built** (`src/dfinsta_pipeline/assessment.py`), and the premise was
tested before it. A calibration experiment fixed seven engagement signals from product
mechanics, then measured them against surfaces we already hold an opinion about with 40
random literals as control: **six were noise**, and the control landed *between* the
labelled groups, so a composite score would have measured roughly "how instrumented is
this class". Hence no addictiveness score, and a measured/judged split enforced by types.

What works is the app's own bookkeeping. The detector names no class — it finds any class
enumerating several endpoints we already block, then reads what else that class lists.
430 → `LX/05jj`, 439 → `LX/03Ez`: the same group under different obfuscated names, each
yielding **the same four unblocked consumption endpoints**, so this gap has existed since
at least 430:

    feed/timeline_stream/   feed/injected_reels_media/
    feed/reels_media/       feed/reels_media_stream/

`feed/injected_reels_media/` is Reels injected into the timeline, going straight through.

**Gate contracts are built** (`src/dfinsta_pipeline/feature_gate.py`). The response is
symmetric to the request: dispositions go to CAS, the decision binds their hash,
`assessment_sha256` pins what the human actually saw, and every candidate must carry a
disposition — one nobody ruled on blocks rather than defaulting to `ignore`.

Design and the experiment: [`docs/STAGE_4_DESIGN.md`](STAGE_4_DESIGN.md).

- [x] **Detect surfaces present in a new version but absent from the last baseline**
      (`src/dfinsta_pipeline/surface_diff.py`). Diffs the stable-string layer only —
      resource names never ids, class *counts* never descriptor sets, so no descriptor can
      cross a version boundary. Measured 430→439 and recomputed rather than quoted: api
      paths survive 93.85%, stable types 89.32%, drawable names 98.80%, drawable **ids
      0.88%**. First run surfaced 105 candidates, and two on its own: a
      `delivery/background_prefetch` path co-located with exactly the Reels endpoints we
      block, and `clips/discover/interest/stream/` spreading from 2 classes to 5 — the same
      shape as the Shopping dissolution.
      **It is invoked by nothing** (measured 2026-08-04): not the driver, not a script, not a
      documented command line; its only mention outside its own 95 tests is prose in two
      docstrings, and one artefact from one hand-run survives in `work/`. It is also **not**
      on the stage 4 path — `assessment.document()` takes a `HookIndex` and the manifest and
      nothing else, and `surface_diff.Candidate.to_dict()` emits no `candidate_id`, which
      `assessment.candidate_ids` requires. Stage 3 and stage 4 are siblings on the same
      `api_surface.json`. Wiring it in needs a `--baseline-index`; giving its output a
      consumer is the separate and larger question.

      **Answered 2026-08-05, and the answer is no: stage 3 must not feed the feature gate.**
      Audited against its one real artefact. `SurfaceDiff.candidates` applies no filter and no
      ranking — every added API-path literal is a candidate — and of the 105 from 430→439,
      **44 are Reels permalink spellings for a surface DFInsta already blocks**, 13 are build
      and test junk, 10 are creator-tools and commerce (the calibration's explicit *negative*
      class), and roughly **one row is genuinely actionable**. The self-filtering field
      `maps_to_blocked_family` fired zero times out of 105, so 42% of the report prints as
      "new area" about something already covered.

      Three further blockers, any one of them sufficient: 90 of the 105 cannot be given a legal
      `candidate_id` at all, and `candidate_ids` refuses the whole document on one bad id, so
      merging would make the *working* gate underivable; `rulings.endpoint_of` refuses a foreign
      namespace and one refusal voids the manifest additions for every other candidate in the
      batch; and 105 candidates through a gate proven at 4 turns
      `_require_every_candidate_ruled` — "the most important check in this file" — into a rubber
      stamp, which is the precise failure it exists to prevent. No filter exists in the repo
      that would cut it down, and `tests/test_assessment.py`'s `NoScoreTests` pins the
      *prohibition* on building one.

      **Its actionable content was reached by fixing stage 4a instead** — see the leading-slash
      entry below. Stage 3 remains valuable as an operator report: survival rates as the honest
      denominator, package deltas, co-location changes as a coverage-durability alarm, and the
      `B_inline` branch that found `delivery/background_prefetch` before stage 4a could.
- [x] **Stage 4a sees two surfaces it was blind to** (2026-08-05). `find_groupings` looked
      classes up by the *normalised* rule while the index holds the app's own text, so
      `clips/discover` matched no class where `/clips/discover` matched `LX/1qi;` — a class
      holding `delivery/background_prefetch`. And `is_blocked` compared by containment alone, so
      `/clips/homecoming` failed against the rule `clips/homecoming/` on a trailing slash and a
      covered endpoint counted as unknown, dropping the grouping below `min_seeds`. Fixed with
      slash-aware lookup and equality-once-stripped — **not** symmetric containment, which was
      tried and destroys `feed/timeline_stream/`, the gap the stage exists to find.
      **4 candidates to 6 on 440, no false gaps.** `delivery/background_prefetch` is absent from
      430 and present on 439, so it is a genuinely new surface, and `surface_diff` had already
      classified it `B_inline` riding with the two Reels endpoints stage 4a now finds it beside.
- [x] **Stage 4a's producer is scheduled** (`driver.STAGES` gained `assess`, between `index`
      and `resolve`). `assessment_record.record` had zero callers outside its own `main`, so
      the real 440 assessment exists only because a human typed the command after the driver
      finished — and every future port would have shipped with none while the whole chain
      downstream stayed green. Takes all four of `--state-root`, `--assessment-run-id`,
      `--actor`, `--owner-token` or none; skips loudly otherwise, because an offline port is
      a real mode. Proven against the real 440 index: one command records an assessment whose
      gate subject the submission client re-derives from the run id alone.
- [x] Assess whether a new feature is addictive, from evidence rather than assertion
      (`src/dfinsta_pipeline/assessment.py`). No composite score: a calibration experiment
      run first ruled that out, six of seven signals being noise with the random control
      landing *between* the labelled groups. What works is Instagram's own curated endpoint
      arrays, found by content and never by class name.
- [x] **Present conclusion plus grounding evidence at a durable human gate.**
      `FeatureAssessmentRunWorkflow` raises it, `submission.py` answers it with a ruling per
      candidate, and `rulings.py` consumes the answer. First proven against the real 440
      assessment in a time-skipping environment; **run against a live Temporal server on
      2026-08-05** (`tests/integration/test_registered_feature_gate.py`), which is what proves
      the update, the query and the payload survive the wire rather than only the Workflow's
      logic. Six candidates, published subject `bc794384…` matching the client's independent
      re-derivation from the run id alone, `completed`, and the admitted dispositions reachable
      afterwards by run id — the property `rulings.py` depends on.
- [x] **A ruling changes something.** `block`/`offer_toggle` reach `semantic_deps`, verified by
      re-running the assessment and watching the candidate disappear; `ignore` suppresses
      through `manifest/rulings.jsonl`, scoped to the policy revision; `defer` correctly
      returns. What the app's own smali must gain is emitted for review — the match method and
      the preference key are not derivable, and a new toggle is five coordinated edits of which
      one fails silently.
      The contracts exist and a human can now answer a gate
      ([`docs/SUBMISSION_CLIENT.md`](SUBMISSION_CLIENT.md)) — but nothing *raises* this one,
      because the assessment has no path into CAS as a recorded operation. See "Immediate
      next three".
- [ ] Handle changed features: shopping dissolved into other endpoints; decide block vs drop
- [x] Decide the agent runtime — **not** Google ADK, by measurement rather than by the plan.
      +55 packages and +143 MiB for a program one line shorter, no confinement help, and
      independence across k runs that is opt-in and fails *silently* by contaminating later
      runs. See [`docs/PROPOSER_RUNTIME.md`](PROPOSER_RUNTIME.md), which records the revisit
      trigger: ADK becomes right if this ever needs agent transfer or shared specialist state.

## 4. Runtime truth — cross-cutting

- [x] Feed, Explore, Stories, Reels contrasts verified on device (Reels needs its own probe)
- [ ] Profile ads — inconclusive; needs an ad-eligible account
- [ ] Reels deep cache-exhaustion check
- [ ] Re-verify the full contract on any new target before calling a port good

## 5. The driver — done, and proven end to end

`python -m dfinsta_pipeline.driver <stock.apk> --out <dir>` is the single entrypoint:
extract → index → resolve → pre-apply evidence gate → compose → build → static verify,
stopping at the first stage that cannot produce what the next needs.

- [x] Proven on Instagram 430 from the stock APK: 5 hooks resolved mechanically with hosts
      discovered from the index, 5/5 operations applied, apktool build, stock DEX graft, and
      **static verification passed** — exact DEX topology, custom DEX new, 16,399 stock
      entries byte-identical, every host hook present in the DEX that owns it, no forbidden
      symbol in custom code.
- [x] Derives per version what used to be hand-edited, each of which silently produces a
      broken APK when wrong: the free `smali_classesN` (430→20, 439→21), the host DEX files
      to graft (`classes.dex,classes3.dex,classes4.dex` on 430 — matching the hand-authored
      list exactly), and the host-hook map the static verifier asserts against.
- [x] Target-neutral static verifier (`tools/verify/verify_build.py`); `build.py` selects it
      with `--verifier generic --host-hooks`. The 430-specific verifier is retained, not
      loosened — they pin different things and neither is weaker.
- [x] **The release gate accepts the driver's own output.** It could not before: `finalize.py`
      passes signature flags only the 430-shaped verifier took, so there was no target-neutral
      post-signing check and 440 was signed by hand. The generic verifier now carries the
      identity envelope, the signature check (`passed` requires verified **and** the expected
      certificate), and an `expect_signed` mode — because the graft strips signatures and the
      post-signing run must instead *require* them. On the real 440 build the gate produces an
      APK byte-identical to the hand-signed one.
- [x] The run correctly reports that the APK is **not release-ready** because post-build
      evidence is absent. That message is the point of the whole exercise.
- [x] Same run with the two settings hooks — both now resolve mechanically and the
      own-profile guard is device-verified.
- [x] **Instagram 440, from the stock APK, with no proposals of any kind: 7/7 hooks
      resolved, `complete: true`, pre-apply gate passed with nothing skipped.** `by_anchor`
      selected 1 class of 182,479 for each settings hook on a version it had never seen, and
      an independently derived literal intersection agrees on one of them. See
      [`docs/IMPLEMENTATION_STATE.md`](IMPLEMENTATION_STATE.md).
- [x] The first genuine extract-to-build run found two bugs that `--reuse-decode` had been
      routing around for every previous "unattended" port: build.py refusing the framework
      cache the driver had just created, and 440's `<queries><provider>` (legal Android, and
      aapt1 will not compile it). Both fixed, both pinned by tests.

## Immediate next three

Reordered 2026-08-03. The two items that stood here are done.

- [x] **The feature gate has a producer, and can be answered.** `assessment_record.py`
      recomputes a stage 4a assessment from the API surface it admitted and records it as a
      ledger operation under a run-keyed authority row; `FEATURE_ASSESSMENT_GATE` is the
      second kind the submission client has ever registered, and it joined only once its
      subject became reachable from a run id. Both proven against the real Instagram 440
      assessment. Records: [`docs/STAGE_4_PRODUCER_DESIGN.md`](STAGE_4_PRODUCER_DESIGN.md),
      [`docs/ANSWERING_THE_FEATURE_GATE.md`](ANSWERING_THE_FEATURE_GATE.md).
- [x] **The generaliser's proposals reach the manifest** (`manifest_patch.py`). The first
      version of this write-back could express every fingerprint kind *except* `by_anchor` —
      the only one that has ever moved the agent count — so it automated only the promotions
      that had never mattered. `generalise_anchor` now measures a hook's own anchor through
      the real `resolve.scan_for_anchor`, and the loop was run for real against a manifest
      copy with both settings hooks wound back to `by_agent`: it rediscovered and committed
      exactly the two `by_anchor` entries a human wrote, and refused `tigon_url_block`, whose
      anchor selects 7 classes on 439 and 5 on 430.

- [x] **The feature gate is raised** (`src/dfinsta_pipeline/feature_workflow.py`), and stage 4
      runs end to end: driven against the real recorded 440 assessment, the workflow's
      published subject matched what the client independently re-derived, and the artifact it
      admitted was byte-identical to the one signed.

- [x] **The verification grant is no longer a dead end.** It was a closed loop between two
      correct checks: the grant table refuses a *different* decision for the run, and the
      Workflow validator refuses a decision issued before the gate it answers — which is
      exactly the journalled decision `submission.py` resubmits verbatim. So a re-driven run
      could answer neither way. `resolve_replay_verification_grant_activity` reads the recorded
      answer before the gate is raised and, on a hit, verifies against it without asking again.
      Both doors are pinned by tests.

- [x] **The real 340/430 run through the registered Workflow.** Done 2026-08-04, both targets
      completed on a live Temporal server: 8 and 9 activities scheduled, History at 64,563 and
      62,261 bytes of the 256 KB budget, verification receipts reporting success with 65 and 16
      assertions, and the mid-run gate answered by `python -m dfinsta_pipeline.submission`
      re-deriving the subject from a run id alone. It settled §3 items 1 and 3 and follow-up
      F1, and unblocked F2. It also found two things before either target finished a stage: the
      worker CLI could not supply `source_root` or `executor_paths`, so fourteen registered
      Activities hosted a Workflow that could not run a single real stage; and a running stage
      blocks the worker's event loop. See `docs/WORKFLOW_REGISTRATION_DESIGN.md` §3c-measured.

- [x] **Non-destructive cancellation within the graceful window — done 2026-08-05.** See the
   Execute section above for what landed. Two things are worth keeping from how it got there.

   **First, a smaller change came before it and was deliberately NOT this item.** Three stages quarantined unconditionally on `CancelledError` where two used the
   graduated form, with no comment, no commit message and zero tests either way — drift from
   birth, not a decision. All five now use one `except BaseException` handler, which catches
   cancellation too. But an adversarial review established, and an AST check confirms, that
   **the branch this corrects is unreachable**: there is no `await`, `async with` or
   `async for` between claiming the operation and `workspace_created = True` in any of the
   five, and an async Activity is cancelled by `task.cancel()`, delivered only at a suspension
   point. A cancellation requested in that region is latched until the launch, by which time a
   workspace exists and both shapes quarantined identically. So it changes nothing that runs
   today; it removes a trap for the day an await appears there, and it gains the release
   protection the cancel path never had. `tests/test_phase_b_cancellation.py` fails if a
   suspension point ever appears in that region.

   **Audited 2026-08-05** (`docs/WORKFLOW_REGISTRATION_DESIGN.md` §3d). Release after a
   workspace exists is safe for every invariant this pipeline owns: nothing a stage writes in
   that window is shared, the ledger fences a zombie by blanking the owner on release, CAS
   publication is atomic and content-addressed, and `claims.py` already releases exactly this
   state by hand and argues that it is safe. Quarantine-after-workspace is drift — it entered
   as a blanket default and no commit, comment or doc argues for it.

   Three prerequisites are now closed: `ToolchainProfileV3` refuses a framework-declaring
   profile whose role plans omit `framework_dir` (the apktool `$HOME` fallback was the only
   shared mutable state outside a workspace, and the sole precondition of the one counter-case);
   `record_effect` checks the owner before its idempotency shortcut; and
   `complete_operation`'s absent owner check is pinned as sound rather than left reading like an
   oversight. **The liveness guard is the change itself**: `process_not_reaped` is, in code,
   the confirmation `claims.py` asks a human for.

   **Two things it deliberately did not do, and they are now the open tail of this item.**
   Nothing removes stale attempt workspaces, and release makes retries possible where they
   previously could not happen — a build workspace holds two APKs and a decoded tree, so a
   reaper or a bounded attempts budget is wanted before this runs unattended for long.
   And `replay_workflow.py:44-52` still argues `maximum_attempts=2` is safe *from* quarantine
   being what makes attempt 2 fail closed; under release attempt 2 re-runs the stage, which is
   intended, but the comment now says the opposite of what happens.
- [x] **The two tree primitives run off the event loop** (2026-08-05). Measured on a real 340
      port: query availability 23% → **92%**, longest blocked stretch 28 samples → 3, apply
      92% → **0%** blocked, 69 expired query tasks → **0**. The port was unaffected — four
      stages, 65 assertions, History in budget. `prepare_replay_verification_gate` stayed 100%
      blocked exactly as predicted, because `load_decoded_tree`'s standalone sites were scoped
      out; that stage runs for seconds, so it is low priority, and the prediction holding is
      the evidence the diagnosis was right.

2. ~~Move the two tree primitives off the event loop~~ — done, see above. F4's other
   prerequisite.
   `decoded_artifact.materialize_decoded_tree` and `capture_decoded_tree_fd` — 15 call sites
   across the five stages — each walk, hash and write tens of thousands of files synchronously.
   Attributed from the two real runs: apply is 92%/91% blocked, decode 82%/80%, build 62%/68%,
   and even `prepare_replay_verification_gate`, which launches nothing, is 80-100%. `apply`
   already threads `apply_port` and is still the worst, so a stage's subprocess is not the
   lever. Must follow item 1, because `asyncio.to_thread` adds cancellation points and a
   cancelled `to_thread` does not stop the thread — which is why `apply_port` is already
   wrapped in the `_await_apply_mutation` supervisor.
3. **Heartbeats (F4), after both.** 66 of 86 query samples on 340 and 113 of 144 on 430 went
   unanswered, the longest unbroken stretch over nine minutes, and the worker logged 191
   expired query tasks. Until items 1 and 2 land, a wrapper heartbeater is starved exactly when
   a heartbeat matters, and a `heartbeat_timeout` sized to a working one would expire and
   deliver the very cancellation item 1 is about.
4. **Port 441 when it exists.** The cost claim is about a sequence and now has two points
   (439 → 2, 440 → 0). A third is what tells "falling" from "fell once".
5. Real k-proposer run for the two settings hooks, using the blind holdout as the prompt
   reference — now an escalation path rather than the normal one, since 440 resolved all
   seven hooks with no proposals at all.

A gate can now be answered: [`docs/SUBMISSION_CLIENT.md`](SUBMISSION_CLIENT.md). The rule
it is built from is that the client re-derives the gate subject from recorded state and
refuses to let a human sign a hash it cannot reproduce — so `PortRunWorkflow`'s
`phase-a-approval`, whose subject this client cannot reach, is deliberately *not*
registered and is refused rather than trusted.

## The decision worth making early

If an automated mapper reliably handles the sites with literals or stable types, but not the
settings hooks, then the realistic end state is **"auto-port most hooks, flag the rest with
evidence at a gate"** rather than full autonomy. That is still very useful, and it is a
cheaper target. Deciding which one we are building changes section 3's design, and it is
cheaper to decide before the ADK layer than after.

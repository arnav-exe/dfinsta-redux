# DFInsta Redux Roadmap

**This is the single roadmap.** It supersedes the "Immediate Next Actions" list in
`docs/SESSION_HANDOFF.md`, the "Immediate Roadmap" in `docs/FUTURE_WORK.md`, and the
priority order in `HANDOVER.md` section 6. Those are retained as history; when they
disagree with this file, this file wins. Do not start a fourth list.

Last updated 2026-08-03 (fourth pass: Instagram 440 ported for zero agent invocations, the
differential producer, the stage-9 recorder, and the release gate closing on the driver's
own output).

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
| **Decide** what to port | find new features, judge addictiveness, present evidence at a gate | surface diff and assessment written; a gate can be answered but **nothing computes an assessment and nothing raises this gate** — see `docs/STAGE_4_PRODUCER_DESIGN.md` |

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
- [ ] **Opt-in real 340/430 run through the registered Workflow** — registration is proven by unit and time-skipping tests only
- [ ] Fold in follow-ups F1/F2 from `docs/WORKFLOW_REGISTRATION_DESIGN.md` during that run
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
- [ ] Non-destructive cancellation *within* the window — the real fix, and it rewrites a
      reviewed invariant (release rather than quarantine after a workspace exists). Needs the
      real run to re-establish the five stages' evidence.
- [ ] Heartbeats in the replay Activities (worker loss undetected until `start_to_close`).
      **Must not land before the item above**: heartbeating is what opens the channel for
      server-originated cancellation, so shipping it first would turn a flaky thirty seconds
      of network into a burned run. Measured correction to the earlier design note — this does
      **not** require editing proven Activity bodies; a heartbeater in the unproven wrapper
      works, because every long operation inside a stage yields the event loop.

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
- [x] Assess whether a new feature is addictive, from evidence rather than assertion
      (`src/dfinsta_pipeline/assessment.py`). No composite score: a calibration experiment
      run first ruled that out, six of seven signals being noise with the random control
      landing *between* the labelled groups. What works is Instagram's own curated endpoint
      arrays, found by content and never by class name.
- [ ] Present conclusion **plus grounding evidence** at a durable human gate (gates last hours to days).
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

1. **Raise the feature gate.** Everything around it exists — the assessment is recorded, the
   subject re-derives, a human can answer with a ruling per candidate — and **nothing raises
   it**. That is the gate's own `@workflow.defn`, per
   `docs/WORKFLOW_REGISTRATION_DESIGN.md`: never new fields on `WorkflowStatus`/`RunResult`,
   a `status` query shaped exactly as `read_pending_gate` expects, and all three hash fields
   bound to the request hash.
2. **Port 441 when it exists.** The cost claim is about a sequence and now has two points
   (439 → 2, 440 → 0). A third is what tells "falling" from "fell once".
3. Real k-proposer run for the two settings hooks, using the blind holdout as the prompt
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

# DFInsta Redux Roadmap

**This is the single roadmap.** It supersedes the "Immediate Next Actions" list in
`docs/SESSION_HANDOFF.md`, the "Immediate Roadmap" in `docs/FUTURE_WORK.md`, and the
priority order in `HANDOVER.md` section 6. Those are retained as history; when they
disagree with this file, this file wins. Do not start a fourth list.

Last updated 2026-08-01 (second pass: Resolve stage, evidence ledger, driver).

## End goal

Given a new stock Instagram APK, produce a working DFInsta build with minimal human
effort: discover what changed, decide what needs blocking, re-map the hooks onto the new
obfuscated code, apply, build, sign, and verify — with humans deciding policy at durable
gates rather than doing the mechanical work.

## Three machines, three very different states

| | What it does | State |
|---|---|---|
| **Execute** a port | apply/build/graft/verify/sign/orchestrate | largely complete |
| **Produce** a port | re-map hook intent onto a new obfuscated decode | **the gap** — hand-authored today |
| **Decide** what to port | find new features, judge addictiveness, present evidence at a gate | not started |

The execute machine is strong and mostly finished. The other two are where the remaining
work lives, and they are what makes the pipeline agentic rather than merely reproducible.

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
- [ ] Non-destructive cancellation path (today a mistimed worker stop burns an admitted run)
- [ ] Heartbeats in the replay Activities (worker loss undetected until `start_to_close`)

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
- [ ] Differential vs N−1 (the last evidence kind with no producer)
- [ ] Probes for the remaining six hooks, and the Reels alternate signal
- [x] **Per-version Index** (`tools/indexer/build_index.py`): 181,421 classes in 3.4s,
      68 MB out, byte-identical across job counts. Confirmed API-path literals are the
      strongest fingerprint (93.9% survive 430->439 vs 89.3% of stable named types).
      **Overturned an earlier claim**: drawable *ids* are NOT stable — of 11,737 names
      present in both versions only 103 keep their id, 99.1% are renumbered. Anchor on
      the drawable NAME and re-resolve the id per version.
- [ ] Feed the Index into host search so `by_literal` hooks resolve without a full rescan
- [ ] Teach a Resolve-stage caller about `Resolution.already_applied` (nothing imports the
      engine yet; naive `if not resolved: fail` would treat a normal re-run as failure)
- [ ] Candidate anchors must be *proposals* only — the deterministic spine still applies and verifies
- [x] **Measure coverage on 439: 7/7 hook operations resolved by agents, zero human mapping.**
      Built, structurally verified, signed, installed, and CONFIRMED WORKING on device
      2026-08-01: 23 canonical block exceptions with the stack showing
      `com.dfinstagram.hooks.throwIfBlocked` <- `TigonServiceLayer.startRequest`, and the
      settings dialog opening with all five toggles. Every obfuscated host had moved
      (`LX/077K`->`LX/0DnT`, `LX/06X7`->`LX/0Di2`, `LX/05t2`->`LX/04tC`), and the custom
      code needed `classes21.dex` because 439 already ships `classes20`.
- [ ] Turn the ad-hoc mapping workflow into a reusable resolver that emits candidates + evidence

## 3. Decide — not started

- [ ] Detect surfaces present in a new version but absent from the last baseline
- [ ] Assess whether a new feature is addictive, from evidence rather than assertion
- [ ] Present conclusion **plus grounding evidence** at a durable human gate (gates last hours to days)
- [ ] Handle changed features: shopping dissolved into other endpoints; decide block vs drop
- [ ] Google ADK, scoped narrowly to these judgement calls — apply/verify stays deterministic

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
- [x] The run correctly reports that the APK is **not release-ready** because post-build
      evidence is absent. That message is the point of the whole exercise.
- [ ] Same run with the two settings hooks, which need real proposer agents rather than
      hand-made proposals

## Immediate next three

1. **Drive the phone.** Join `evidence.probe_claim` to `tools/device_validation/runner.py`
   so `runtime_probe` and `differential` can exist. Nothing can pass the release gate until
   they do, and this is what separates "a build" from "a build that works".
2. Real k-proposer run for the two settings hooks, using the blind holdout as the prompt
   reference, so `install_settings_long_click` stops being excluded from runs.
3. Feature discovery and the addictiveness gate (section 3), which is still untouched.

## The decision worth making early

If an automated mapper reliably handles the sites with literals or stable types, but not the
settings hooks, then the realistic end state is **"auto-port most hooks, flag the rest with
evidence at a gate"** rather than full autonomy. That is still very useful, and it is a
cheaper target. Deciding which one we are building changes section 3's design, and it is
cheaper to decide before the ADK layer than after.

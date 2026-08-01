# DFInsta Redux Roadmap

**This is the single roadmap.** It supersedes the "Immediate Next Actions" list in
`docs/SESSION_HANDOFF.md`, the "Immediate Roadmap" in `docs/FUTURE_WORK.md`, and the
priority order in `HANDOVER.md` section 6. Those are retained as history; when they
disagree with this file, this file wins. Do not start a fourth list.

Last updated 2026-08-01.

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
- [ ] Automated resolver: intent + clean decode → candidate anchors with evidence and confidence
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

## Immediate next three

1. Finish the blind settings holdout and get a trustworthy answer on whether hook mapping
   can be automated. Everything in section 2 depends on that number.
2. Real 340/430 run through the registered Workflow, with F1/F2 folded in.
3. Attempt a 439 port, measuring automated coverage against human effort.

## The decision worth making early

If an automated mapper reliably handles the sites with literals or stable types, but not the
settings hooks, then the realistic end state is **"auto-port most hooks, flag the rest with
evidence at a gate"** rather than full autonomy. That is still very useful, and it is a
cheaper target. Deciding which one we are building changes section 3's design, and it is
cheaper to decide before the ADK layer than after.

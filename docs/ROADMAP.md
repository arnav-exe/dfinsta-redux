# DFInsta Redux Roadmap

**This is the single roadmap.** Started fresh on 2026-08-08 when the project changed
approach; the previous one is at [`history/ROADMAP.md`](history/ROADMAP.md) and is not
authoritative. Read [`DESIGN_EXPLORATION_FIRST.md`](DESIGN_EXPLORATION_FIRST.md) first —
it explains the approach this tracks.

## End goal

Given a new stock Instagram APK, produce a working DFInsta build with minimal human
effort: discover what changed, **measure on the phone what the app actually asks for**,
decide from that evidence what to block, re-map the hooks onto the new obfuscated code,
apply, build, sign and verify.

## The change of approach, in one paragraph

The pipeline used to decide early. It scanned the decompiled app for strings shaped like
API paths, presented them at a human gate with static evidence only, and corrected wrong
rulings afterwards through a reversal gate. On 2026-08-08 that produced six `block`
rulings in one sitting: one was not an endpoint at all (a no-op logger's marker name), and
of the other five, **every one fired zero times on a real device**. A day's work changed
the app's behaviour by nothing. The correction machinery — nine modules, 21,000 lines with
its tests — had recorded zero retirements and zero reversals in its life, and was deleted.
It is replaced by measuring first.

## The three machines

| | What it does | State |
|---|---|---|
| **Execute** a port | apply / build / graft / verify / sign / orchestrate | **complete, and untouched by the change of approach** |
| **Produce** a port | re-map hook intent onto a new obfuscated decode | **7 of 7 hooks mechanical on 430, 439, 440 and 441**, zero agent invocations |
| **Decide** what to block | measure on device, then rule | **being rebuilt** — this is where the work is |

The Execute and Produce machines do not know gates exist. That is why changing how
decisions are made cost 12% of the source and none of the hard-won knowledge.

## Now

- [x] **Guards are generated, not hand-written.** `guards.py` renders `throwIfBlocked`
      from `url_block_rules` in `manifest/hooks.json`, proven instruction-for-instruction
      against the exact method measured firing on the phone.
- [x] **Observe mode.** A build that logs every watched path *before* any rule can throw,
      so a device session says what the app actually asked for. Never ships.
- [x] **Observation store**, version-keyed, refusing rather than answering when no session
      is evidence.
- [x] **The correction layer deleted.** Suite 3876 → 3317.
- [x] **Hook retirement rebuilt small** (`retirement.py`, ~330 lines against the 6247 that
      were deleted). `expectation`'s ratchet has exactly one release again: an append-only
      row naming who ruled and why, with `effective_from` derived so it cannot be backdated
      onto the port that exposed the drop — derived from the tree, not from a flag, after
      an adversarial pass showed the flag made the rule a formality — and `ruled_by` checked
      against an allowlist rather than one banned word. **Un-retirement is
      another row, never an edit**, so a surface Instagram brings back is expected again
      without the record losing the fact that it was once doubted — `retirement show` prints
      the whole history, and `returned()` reports a retired hook that has started passing
      probes again. It reports; it never rules.
- [x] **Record the toggle state in `ObservationSession`.** Measured 2026-08-08: with
      blocks on, `/feed/injected_reels_media/` observed 0 times; with them off, 3 — an
      honest zero and a self-inflicted one were indistinguishable in the record. The
      state now comes out of the capture, where the build states it on every checked
      request, and there is deliberately no `--toggles` flag: an operator-supplied state
      would be the formality `effective_from = --version + 1` was. A capture that cannot
      state one **refuses** rather than defaulting to "all off".
- [x] **Separate sessions in `never_observed`.** It takes the state as a required
      argument and answers over the sessions measured under exactly that one; `states()`
      lists what is on record and `observation report` answers each separately. The one
      committed 441 session predates the field: it stays readable, is named in every
      report, and answers nothing.
- [ ] **Re-take the 441 exploration session.** The committed row cannot say what was
      active, so every zero in it — including the twelve literals it used to be quoted
      for — is unusable. Nothing can be back-filled: that would be inventing the
      measurement from memory.
- [ ] **Run the exploration protocol at 439**, then carry it forward to 440 and 441.
      Owner decision 2026-08-10: starting at 439 makes two "future" versions available
      immediately instead of waiting for Instagram to ship 442, so "this survives a version
      bump" becomes checkable today. Installing 439 over a 441 build is a downgrade and
      needs `adb install -r -d`.
- [ ] **Present observation evidence at the feature gate**, and say *"never watched"* and
      *"watched, never seen"* in different words — they look identical and mean opposite
      things.

## Open ends that are nobody's bug

- **A recorded `block` on an endpoint cannot be retired.** With the reversal gate gone
  there is no mechanism. The new approach avoids *creating* bad rulings and says nothing
  about the six that exist. `delivery/background_prefetch` is the live example, and it is
  the one thing the exploration protocol does not reach backwards to fix.
- ~~**Grouping is still human judgement**, better informed. Nothing derives a toggle mapping
  from measurement automatically.~~ Closed 2026-08-10 by `dfinsta_pipeline.grouping`, which
  derives `erased by T` / `blocked by T` / `unaffected` / `never_requested` per watched path
  from the baseline and the one-toggle-on arms, with a derived noise floor and no stored
  answer. *Deciding* is still a human editing `url_block_rules`; what is no longer a
  judgement is which paths a toggle was measured to govern.

- **The 439 sessions predate the block counter.** `observation.parse` now counts the
  `IgFunctionalErrorEvent` block headers, which is the only signal that sees a path block at
  all — a block does not lower a request count, and `/feed/reels_tray/` moves 2 → 3 under
  `disable_stories`. The twelve committed rows have no `blocks` key, so `grouping report`
  returns the erasures and the never-requested and **refuses the blocked half by name**.
  Re-recording the twelve captures reproduces every existing field exactly and adds the
  counts, but it rewrites an append-only store from `work/`, which is gitignored — an owner
  decision, not a maintenance one.

- **`IgFunctionalErrorEvent` can be absent for a block that happened.** `439-reverse-explore`
  ran with `disable_explore` on, asked for `/discover/topical_explore` six times and reported
  **no block at all**; `439-isolate-explore` reported one. So `/discover/topical_explore` is
  `unclassifiable` rather than blocked, and every count taken from this signal needs its own
  replication. `DESIGN_EXPLORATION_FIRST.md` previously described this instrument as good at
  attribution on the strength of two endpoints.

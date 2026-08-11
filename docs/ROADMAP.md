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
- [x] **The exploration protocol runs end to end on 439.** Twelve sessions over six
      toggle states, every state measured twice — once forward, once back-to-front — and
      `grouping` derives from them: `/feed/timeline/` BLOCKED by `disable_feed`,
      `/feed/reels_tray/` BLOCKED by `disable_stories`, `/clips/discover` and
      `/clips/discover/stream/` ERASED upstream by `disable_reels`, ten NEVER REQUESTED
      including `delivery/background_prefetch`, two unclassifiable.
- [x] **The store is checkable against committed inputs.** `manifest/captures/` holds a
      verified redaction of each session — `tools/redact_capture.py` refuses unless the
      reduction parses identically to the original — so a clone can re-derive every count.
      Regenerating the store from them changed exactly one field.
- [ ] **Carry the protocol to 440, then 441.** Owner decision 2026-08-10: starting at 439
      makes two "future" versions available immediately instead of waiting for Instagram to
      ship 442, so "this survives a version bump" is checkable today. 440 and 441 are
      upgrades from 439 and need no re-login; going *back* to 439 later needs
      `pm uninstall -k` then install, because `install -r -d` is refused for a
      non-debuggable app.
- [x] **Record which walk produced a session.** The driving script went from one pass over
      three surfaces to three rounds on 2026-08-11 and the 440 baseline went from 11–16
      observed requests to 25. Two sessions of one state walked differently spread by 14
      for a reason no toggle caused, and `grouping` derives its noise floor from exactly
      that spread — it would have called the whole difference noise and swallowed every
      real effect under it. So a session names its `walk`, `grouping.classify` takes it as
      a **required argument** and compares only within it, and `observation.never_observed`
      deliberately does not: a negative claim only gets safer as walks are pooled into it,
      a differential does not.

      **This one the operator types**, unlike the toggle state, and the docstring says so
      rather than implying a guarantee: the walk is a property of the driving script, not
      of the phone, and nothing in a capture names it. What a capture *does* carry is
      logcat's timestamps, so `parse` measures the **span** and stores it, and
      `walk_dispute` refuses when the sessions claiming one walk split into two groups
      further apart than either group is wide. No magnitude in it — the corpus supplies its
      own scale. Measured: the 24 committed sessions span 109–153s across two builds and
      six toggle states while their request counts run 8–39, and the check passes them
      pooled and fails the moment a three-round set is filed under the same name.

      **A derived threshold still needs its precision measured.** The first version
      required two sessions on each side of a split, and an adversarial pass measured what
      that cost on this repository's own evidence: **66 of the 495 four-session subsets of
      the 439 corpus were refused**, every one of them a single walk on a single build,
      rising to 145 of 924 at six — including the smallest corpus that yields a finding at
      all. With two members a group's range is one difference and comes out at 0–1s, so any
      honest five-second variation reads as two protocols; that is the leak scan that
      flagged every fixture, arriving again. At **three** a side no subset of either
      committed corpus is refused at any size, and contamination is still caught from three
      sessions of the second walk onward. The cost, stated rather than hidden: two
      mislabelled sessions among many are now invisible.

- [ ] **Re-record the 24 committed 439 and 440 sessions with their walk.** They predate the
      field, so `grouping` refuses them by name and says so. Deliberately **not**
      back-filled by anyone else: unlike the toggle state and the block count, which were
      re-derived from `manifest/captures/` because the evidence was in the capture, the
      walk is not in a capture at all. The repair is not a re-walk, though — the captures
      are committed and

          python -m dfinsta_pipeline.observation record --version 440 \
              --capture manifest/captures/<session_id>.log --walk <what you ran> ...

      reproduces every count, block and toggle identically and adds the one thing only the
      person who ran them knows. Until then 439's and 440's groupings are unanswerable, and
      that is the honest state rather than a regression. A bucket for "unstated" that
      answered in full was considered and rejected: it would hand back exactly the property
      naming the walk buys.

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

- ~~**The 439 sessions predate the block counter.**~~ Closed 2026-08-10. The block count is
  the only signal that sees a path block at all — a block does not lower a request count, and
  `/feed/reels_tray/` moves 2 → 3 under `disable_stories`. The twelve rows were re-derived
  from `manifest/captures/`, which changed exactly one field and left the rest byte-identical.
  The objection that stopped it — rewriting a committed store from evidence a clone never
  gets — was answered by committing the redacted captures first, so the regeneration is
  reproducible by anyone.

- **`IgFunctionalErrorEvent` can be absent for a block that happened.** `439-reverse-explore`
  ran with `disable_explore` on, asked for `/discover/topical_explore` six times and reported
  **no block at all**; `439-isolate-explore` reported one. So `/discover/topical_explore` is
  `unclassifiable` rather than blocked, and every count taken from this signal needs its own
  replication. `DESIGN_EXPLORATION_FIRST.md` previously described this instrument as good at
  attribution on the strength of two endpoints.

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
      that are both sharper than the corpus's own variation and more than 5% apart.
      Measured: 439's twelve one-pass sessions span 122–153s over six toggle states while
      their request counts run 14–39, and neither they nor any subset of them disputes.

      **A derived threshold still needs its precision measured, and this one needed
      a magnitude in the end.** The first version required two sessions on each side
      of a split and compared the gap only against the groups' own ranges. An
      adversarial pass measured what the first half cost: **66 of the 495
      four-session subsets of the 439 corpus were refused**, every one a single walk
      on a single build. Three a side fixed that. The second half then failed on the
      first three-round corpus: twelve sessions walked on 440 read 271, 271, 271 and
      273 nine times — one script, one sitting — and both sides of that split are
      *zero seconds wide*, so a two-second difference over a 271-second walk was
      infinitely sharper than the variation and
      `grouping report --version 440 --walk three-round-v2` returned nothing at all.
      A scripted walk with fixed sleeps is precisely what produces near-identical
      spans, so the rule was least reliable where it is most often applied.

      A split must now also exceed **5% of the faster group**, and that term is a
      magnitude rather than a derived scale. It cannot be derived, and the argument
      is in `_MIN_SEPARATION`: `{271 x 3, 273 x 9}` and `{271 x 3, 543 x 9}` have the
      same cardinalities, ordering and group ranges, so no function of the shape
      separates them — only size does, and size needs a scale from outside the
      shape. A dimensionless fraction is the best available: it costs nothing when
      the walk gets longer, and the existing scale-invariance test holds it to that.
      What stops it being retuned is that **both ends are pinned as tests** — spans
      2s apart over 271s is 0.74% and must not dispute; three rounds against four is
      33% and must — so raising or lowering it fails rather than reading as
      maintenance. **Both of those ends are now constructions, not measurements.**
      The 440 corpus they were taken from was withdrawn on 2026-08-11 (see below),
      so they are carried as named synthetics with their provenance written down,
      and only the precision side — 439's twelve, which must never dispute — still
      rests on committed evidence. A bracket of two constructions is weaker than one
      of two measurements and `_MIN_SEPARATION` says so; re-measuring a three-round
      corpus replaces the lower end with evidence.

- [x] **The 440 corpus was withdrawn.** All 24 rows and every `440-*` capture were deleted
      on 2026-08-11. The session driver picked bottom-nav tabs by `content-desc`, `Reels`
      also matched a content node near the top of the feed, and taking the first match in
      document order sent the Reels leg of six sessions to the top of the screen for three
      taps each — silently. Those sessions do not measure what their `surface` field
      claims, and `disable_reels` is exactly the signal that came from there. 439 is
      unaffected: it ran an earlier driver with hand-verified tab coordinates.

- [ ] **Re-record 439's twelve committed sessions with their walk.** They predate the
      field, so `grouping` refuses them by name and says so. Deliberately **not**
      back-filled by anyone else: unlike the toggle state and the block count, which were
      re-derived from `manifest/captures/` because the evidence was in the capture, the
      walk is not in a capture at all. The repair is not a re-walk, though — the captures
      are committed and

          python -m dfinsta_pipeline.observation record --version 439 \
              --capture manifest/captures/<session_id>.log --walk <what you ran> ...

      reproduces every count, block and toggle identically and adds the one thing only the
      person who ran them knows. Until then 439's grouping is unanswerable, and that is
      the honest state rather than a regression. A bucket for "unstated" that answered in
      full was considered and rejected: it would hand back exactly the property naming the
      walk buys.

- [ ] **Re-walk 440 with the fixed driver**, which also restores the lower anchor of
      `_MIN_SEPARATION` to a measurement.

- [ ] **Present observation evidence at the feature gate**, and say *"never watched"* and
      *"watched, never seen"* in different words — they look identical and mean opposite
      things.

## Next: stop measuring our own block through Instagram's log

The block signal has always been `java.io.IOException: Blocked by DFInsta setting` grepped out of
logcat — a line that is there **because Instagram catches our exception and logs it**. It costs
nothing to read, so it became the signal, and an accounting identity and a subset-sum ambiguity
check were built on top to work out which path an untagged aggregate belonged to.

It under-reports, measurably and consistently. `/discover/topical_explore` under `disable_explore`:
requested 7 times and reported once; requested 6 times and reported **none**. Eight sessions,
both versions, both walks, same story — while `/feed/timeline/` reports 20/20, 23/23, 17/17 and so
on, perfectly, every time. The loss is feature-specific and stable, so no amount of inference
recovers it.

Whether the app *requests* a path is Instagram's business — that is what we measure. Whether the
block happens, and whether it is **recorded**, are ours. The second was given away for
convenience.

- [ ] **Emit the block from our own code**, immediately before the throw:
      `observe;->blocked("<rule>")` → `I DFInstaObserve: !blocked <rule>`, through the same
      `android.util.Log.i` used for the toggle line and the observed paths, which has never
      dropped a line.
- [ ] **Attribute directly and delete the inference.** The accounting identity, the subset-sum
      check and the block-count noise floor have nothing left to do.
- [ ] **Re-measure one version** and confirm the walk-sensitivity disappears for blocks.

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

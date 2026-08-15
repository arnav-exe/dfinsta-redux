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
- [x] **441 measured 2026-08-14** — 24 sessions, both walks, build `667894e15984`, 7 of 7
      hooks resolved mechanically. The old row was **withdrawn, and it is the one withdrawal
      this project cannot undo**: unlike the 439 and 440 rows retired the same day, whose
      captures stay committed and are one `observation record` from returning, its captures
      predate the toggle directive and do not parse, so `redact_capture --verify` could not
      commit them. It answered nothing — no toggle state, so every number belonged to a
      configuration nobody can name — and what it said is recorded here so it is not simply
      gone: `/feed/timeline/` 28, `/feed/reels_tray/` 20, `/clips/discover` 2,
      `/discover/topical_explore` 2, recorded 2026-08-08T18:05Z.
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
- [x] **Carried to 440**, and to 439 again under the direct signal — 48 sessions, four
      corpora, two walks each. **441 is the one left.** Owner decision 2026-08-10: starting at 439
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

- [x] **Moot 2026-08-14: those twelve rows were withdrawn**, not repaired. They came from a
      build that could not report its own refusals, and 439 was re-walked with one that can;
      `classify` refuses to compare across builds, so keeping both made the version
      unanswerable. Their captures are still committed, so the rows are one `observation
      record` away from returning — and their spans, the noisiest real group in the project at
      122–153s, still anchor `walk_dispute`'s derived term through `withdrawn_spans()`. The
      original item read: re-record them with their walk, because they predate the
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

- [x] **440 re-walked with the fixed driver** on 2026-08-12, and again on 2026-08-13 with a
      build that records its own refusals. `_MIN_SEPARATION`'s lower anchor is a measurement
      again — and the *upper* one, the noisy real group, now comes from the withdrawn 439
      captures rather than the store, because every corpus the current driver produces lands
      within 3s and none of them can play that part.

- [x] **Observation evidence reaches the feature gate**, 2026-08-14. Each candidate carries
      one item from `device_evidence.py`, and the three states are named apart because two of
      them look identical and mean opposite things: `device_unwatched` (nobody looked),
      `device_never_requested` (looked across N sessions, never seen), `device_requested`.

      **Only the third is STRONG.** A zero stays WEAK however large the corpus, because the
      argument is about what a zero can mean rather than how many sessions produced it:
      `feed/timeline_stream/` is requested zero times and blocking it is still right, since
      the routing that decides what an account sees is server-side.

      **And `block` / `offer_toggle` now require that a device looked** —
      `feature_gate._require_measurement_before_acting`, beside the completeness rule it
      mirrors. `ignore` and `defer` are unrestricted, so the gate stays answerable without a
      phone; only the two verdicts that change the shipped app are withheld. The refusal
      happens in the client too, before anything is sent, because the client runs the
      admitting side's own validator on its own payload.

      Checked against the six candidates the gate really admitted on 441: five come back
      `device_never_requested` and `feed/reels_media_stream/` comes back `device_requested`,
      blocked by `disable_reels` on 440. None of that was visible when they were ruled.

      Absence of the evidence counts as unmeasured, which is the completeness rule applied
      one level up: a missing *disposition* is never a pass, and neither is a missing
      *measurement*.

- [ ] **`offer_toggle` still does exactly what `block` does.** Both append the endpoint to
      `semantic_deps`; nothing writes a preference key, a settings row or a toggle, and
      `RulingPlan.custom_code` only tells a human which endpoints need hand-written smali.
      The verdict exists in the vocabulary and its distinguishing behaviour does not exist in
      code. `unenforced-endpoints.json` is what makes that visible per build.

## Done 2026-08-13: stopped measuring our own block through Instagram's log

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

- [x] **Emit the block from our own code**, immediately before the throw:
      `observe;->blocked("<literal>")` → `I DFInstaObserve: !blocked <literal>`, through the
      same `android.util.Log.i` used for the toggle line and the observed paths, which has
      never dropped a line. It names the **literal that matched**, not the rule: a rule may
      test several, and `/clips/discover` — the path the two walks disagreed about — shares
      one with `/api/v1/clips/homecoming/`. An observing build takes a fourth register to
      carry the matched literal from the test that matched it to the throw that refuses it.
      The message thrown is still the single fixed string, so a per-rule vocabulary never
      reaches Instagram's error event.
- [x] **A build states what it can report.** The toggle line reads
      `!toggles +blocked disable_feed=1 …`. Without it a capture holding no `!blocked` line
      would be ambiguous between "nothing was refused" and "this build could not have said",
      and all 48 committed sessions are the second — they would have become 48 measured
      zeroes at once, in the field whose zero is the control every arm is compared against.
      On the toggle line rather than a line of its own because `state()` runs on every
      checked request — 625 times in a three-round session — so a second line would have
      grown every committed capture by 55% to repeat one constant.
- [x] **Attribute directly and delete the inference.** `_accounts` and `_attribute` are gone,
      70 lines of block-accounting identity and subset-sum ambiguity check. A path is BLOCKED
      when every session of the arm recorded refusing it, the baseline recorded none, and it
      was still requested. Instagram's count is still parsed and still printed **beside** ours
      and labelled — a reader seeing 17 refusals recorded and 3 events reported is seeing why
      this stopped being the basis — and nothing derives from it.
- [x] **The equivalence claim is executed, not read.** An observing build no longer has the
      same instructions as a shipped one, so comparing their text would assert only that
      nobody changed them. `guards.decide` interprets a rendered method, and shipped and
      observing are run against every literal — plus near-misses and an unrelated path —
      under every toggle state and compared. It refuses any instruction it does not know, so
      it cannot pass by being blind.
- [x] **Re-measured 440**, both walks, 24 sessions with the observing build
      `ccd42be3a8b7`. 440 rather than 439 because it was the version installed, and going
      back costs `pm uninstall -k` and risks the login; the question is about the
      instrument, not the version. The 439 corpus is unchanged and its block half stays
      unanswerable until it is walked again — the signed 439 observing build is at
      `work/439-observe-v3/`.

      **Every arm is readable on both walks.** That has not happened before: an arm used to
      go unreadable whenever Instagram's two sessions disagreed about a block count, and on
      the old 440 one-pass corpus `disable_explore` reporting 0 then 1 made **every path in
      the corpus** unclassifiable — "unaffected" is a claim about every toggle, so one
      feature's telemetry going quiet took the whole answer down.

      **Four blocked endpoints, identical on both walks**, each attributed by the guard's own
      record: `/feed/timeline/` ← `disable_feed`, `/feed/reels_tray/` ← `disable_stories`,
      `/discover/topical_explore` ← `disable_explore`, `/feed/reels_media_stream/` ←
      `disable_reels`. Run the two derivations over the **same twelve one-pass sessions** —
      the new one with `refusals`, the old with that key stripped — and the old names one of
      the four and declines three. It is not contradicted; it could not answer.

      **What Instagram reported, beside what we recorded:** feed 18/18 against 18/18 and
      stories 2/3 against 2/3 — exact, and the positive control without which a different
      number is not a better one. Explore **14/14 against 1/1**. Reels **2/3 against 0/0**.

- [x] **439 re-measured too**, both walks, 24 sessions with `e751d4e9eb33`. Its original 24
      rows were withdrawn on the same grounds, captures kept. **439's two walks agree on every
      endpoint** — which is the result this whole change was for, since before it they
      contradicted each other on one version on one day. All four corpora now have every arm
      readable, and `/feed/timeline/`, `/feed/reels_tray/` and `/discover/topical_explore` are
      blocked by the same toggle in all four.

      On 439 `disable_explore` refused `/discover/topical_explore` **8, 8, 14 and 2** times
      across the four arm-sessions while Instagram reported **0, 0, 0 and 1** — and
      `/feed/timeline/` reported 18/18 and 19/19 exactly in the same captures, which is what
      makes those zeroes a measurement rather than an assertion.

      Two endpoints still read differently between the versions and **both are the app**.
      `/feed/reels_media_stream/` has no 439 baseline at all — requested 0 and 0 with every
      toggle off, so nothing can be called blocked against it — where 440 requests it every
      session. And `/clips/discover`, below.

- [x] **The walk-sensitivity for blocks is gone, and the one remaining difference is a fact
      about the app.** `/clips/discover` reads ERASED on one-pass and BLOCKED on three-round,
      and the counts say why: under `disable_reels` the short walk requests it 0 times of a
      baseline 2, and the long walk requests it 4 times of a baseline 7 and the guard refuses
      all four. Both mechanisms are live. `replaceReelsEndpoint` blanks the literal at the
      `const-string` site, and a longer walk reaches a second route to the same path that the
      erasure does not cover. The previous corpus could only say ERASED versus *undecidable*;
      now both walks decide, and they decide different mechanisms because the app does
      different things.

      **And 439 does not do it.** Its long walk requests `/clips/discover` 0 times under
      `disable_reels`, exactly like its short one, so the fallback is not merely unobserved
      there — the longer walk had every chance. 440 added a second route to that endpoint.
      Seven classes on 440 carry the `clips/discover/` literal and exactly one was patched, on
      the reasoning that only it builds an outgoing request; the others read as analytics maps
      and prefetch allowlists. **Worth finding which one now builds a real request.** Until
      then the `/clips/discover` url_block rule is the only thing catching those four requests
      a session, so it must not be tidied away as redundant — and the hook's own note, which
      told a reader to expect zero blocks, was corrected on 2026-08-13.

- [x] **The superseded 440 rows were withdrawn.** 24 rows from build `55fa576b3c73`, which
      could not report refusals. Not a judgement on them: `classify` refuses to compare
      across builds at all, so leaving both made the version unanswerable, and the newer
      build measures everything the older did. **Their captures stay in `manifest/captures/`**,
      so every withdrawn row is one `observation record` away from returning.

## Three versions, six corpora

Measured 2026-08-13/14 with builds that record their own refusals. **Every arm readable in
all six**, which had never been true of any corpus before — an arm used to go unreadable
whenever Instagram's two sessions disagreed about a block count.

| endpoint | 439 1p | 439 3r | 440 1p | 440 3r | 441 1p | 441 3r |
|---|---|---|---|---|---|---|
| `/feed/timeline/` | blocked feed | blocked feed | blocked feed | blocked feed | blocked feed | blocked feed |
| `/feed/reels_tray/` | blocked stories | blocked stories | blocked stories | blocked stories | blocked stories | blocked stories |
| `/clips/discover/stream/` | erased reels | erased reels | erased reels | erased reels | erased reels | erased reels |
| `/discover/topical_explore` | blocked explore | blocked explore | blocked explore | blocked explore | unclassifiable | blocked explore |
| `/clips/discover` | erased reels | erased reels | erased reels | **blocked reels** | erased reels | erased reels |
| `/feed/reels_media_stream/` | unclassifiable | unclassifiable | blocked reels | blocked reels | unclassifiable | unclassifiable |

**Three endpoints carry the same toggle in all six.** Nothing reaching that reads a name:
`/feed/reels_tray/` and `disable_stories` share no words.

**The explore under-reporting is a trend now, not a coincidence.** Across the six corpora
`disable_explore` recorded refusing `/discover/topical_explore` **8, 8, 14, 2, 7, 7, 15 and
15** times, and Instagram reported **0, 0, 0, 1, 1, 0, 0 and 0**. In the same captures
`/feed/timeline/` reported 16/16, 17/17, 18/18, 19/19 — exactly. Feed agreeing is what makes
those zeroes a measurement rather than an assertion.

**Two cells are unclassifiable for a stated reason, and neither is the derivation wobbling.**
441's one-pass explore has a baseline of **0 and 7**: the first session of the corpus ran on a
freshly upgraded app and Explore had not loaded, so a zero under an arm cannot be told from a
zero the baseline produces on its own. The rule was not loosened to make it classify — the
three-round walk supplies a proper baseline and it lands. And `/feed/reels_media_stream/` has
no 439 or 441 baseline at all, being requested 0 times with every toggle off.

**Only 440 falls back to `clips/discover/`, and the honest claim is narrower than it looks.**
`replaceReelsEndpoint` blanks `clips/discover/stream/` on all three versions — that row is
erased in all six corpora. Only 440, and only on the longer walk, then requests
`clips/discover/` instead: 4 against a baseline of 7, all refused by the url_block rule. 439
and 441 request it 0 times under `disable_reels` on **both** walks, so the long walk had every
chance on the versions either side. A behaviour present in 440 and absent from both its
neighbours looks at least as much like server-side configuration as app code — this project
has been caught by that once already, when a statically perfect 430 settings hook was dead at
runtime because a MobileConfig flag picked the other implementation. What is measured is that
**440's install did it and the others' did not**, and `ThreeVersionsTests` pins that wording so
the stronger claim cannot creep back.

## A port is one command, up to the judgement

`tools/port.py --apk … --version … --run` runs the nine mechanical steps: index, watch,
observing build, sign, install, two device walks, two recordings. It reports by default and
writes nothing without `--run`; it is resumable from artefacts rather than a state file,
because two walks are about a hundred minutes and a failure at step seven must not redo six;
and it never reads, defaults or prints the signing secrets.

It stops before the two steps that are judgement: ruling at the feature gate, and writing the
`url_block_rules` entry afterwards. Neither is derivable — see the three refusals in
`rulings.py`, one of whose supporting regularities has since inverted.

Until 2026-08-14 that sequence existed only as a paragraph in
`DESIGN_EXPLORATION_FIRST.md`, which is where `run_corpus.py` and `record_corpus.py` lived the
day before, and the two device corpora they produced were reproducible by nobody.

## `PortRunWorkflow` was a harness, not a blocked orchestrator — deleted 2026-08-15

It ported nothing. Its four activities wrote placeholders — `admit` echoed
`canonical_json(spec)`, `prepare` wrote the literal string `prepared:<run>:<hash>`, `apply`
wrote `applied:<run>:<hash>:<hash>` — and `docs/ADK_PIPELINE_PLAN.md:247` said so by design:
*"build one **synthetic** `PortRunWorkflow` … this phase contains no APK build, ADK agent,
child workflow, signing, or device action."* Its `phase-a-approval` gate was unanswerable
through a circular dependency: you needed the `RunSpec` to compute the operation key that
would give you the `RunSpec`. Answering it would have given an answerable gate on a workflow
that produced a string.

**What went with it:** its own module file (`workflow.py`, now deleted — do not go looking
for it), the four Phase A activities, two committed Histories, their negative control, and
the two test files that pinned them: about 1,750 lines. Nothing else imported it.

**What Temporal is for here is unaffected**, which is why deleting this cost no coverage.
The reason this project uses Temporal is durable multi-day *human gates*, and
`FeatureAssessmentRunWorkflow` is registered, answerable, and has been answered for real
(`feat-441`). `ReplayRunWorkflow` keeps its own two Histories and its own control, so the
PINNED-replay safety net that catches a non-deterministic edit still stands over both
workflows that do work. The mechanical steps of a port are resumable in `tools/port.py`
with no server at all, and the expensive one — a device walk — cannot be retried without a
human plugging a phone in.

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

- ~~**`IgFunctionalErrorEvent` can be absent for a block that happened.**~~ Closed
  2026-08-13 by not using it. It was true and it was worse than recorded: across six corpora
  `disable_explore` refused `/discover/topical_explore` **8, 8, 14, 2, 7, 7, 15 and 15** times
  while Instagram reported **0, 0, 0, 1, 1, 0, 0 and 0**. The guard now records its own
  refusals and `/discover/topical_explore` reads `blocked` on five of the six corpora — the
  sixth declines on a cold baseline, not on the signal. The count is still parsed and printed
  beside ours, labelled; nothing derives from it.

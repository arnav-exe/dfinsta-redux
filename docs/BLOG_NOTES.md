# Blog Notes: Teaching Agents to Patch Instagram

Working notes for an informal development post. This is a journal and source-material dump, not polished documentation.

## Possible Premise

Instagram changes constantly, while DFInsta works by patching Instagram's compiled Android bytecode. Every release scrambles many internal names, so an update that sounds like "copy the old changes over" is actually a reverse-engineering problem involving more than 100,000 smali files.

The fun question: how much of that can be turned into a repeatable multi-agent pipeline without giving an LLM permission to freestyle-edit an APK?

## Tone

- Explain unfamiliar Android terms before using them heavily.
- Keep the journey chronological, including wrong assumptions and failed approaches.
- Prefer concrete scale, timings, and examples over abstract architecture language.
- Leave room for informal asides and screenshots rather than making this read like a research paper.

## Story Beats So Far

### The misleading starting point

The repository contains DFInsta 1.3 source, but it is not source code in the normal Android-app sense. It is custom smali plus complete copies of a few patched Instagram classes. The build decompiles an official APK, overlays those files, rebuilds it, and signs the result.

At first glance, porting the patch appears to require remapping more than a thousand obfuscated references. The earlier 300-to-340 experiment found the important trick: almost all of those references belong to copied Instagram code. The real 1.3 patch was only eight host classes and a handful of injected instructions.

Potential line: the first major optimization was not a smarter model; it was realizing we were asking the wrong diff question.

### Why 1.4.1 comes before Instagram 430

We have both stock Instagram 340 and a real DFInsta 1.4.1 APK. That pair is an oracle: because both APKs share the same Instagram base, their meaningful differences reveal what the human developer actually kept, removed, or redesigned.

Jumping directly from the older 1.3 source to Instagram 430 would carry forward brittle code that 1.4.1 already abandoned. Reconstructing 1.4.1 first gives the future pipeline a cleaner baseline and a known answer against which its output can be checked.

### Deterministic spine, agents at the seams

APK extraction, indexing, normalization, patch application, building, and symbol verification are ordinary deterministic programs. Agents are useful only where the evidence is ambiguous: identifying a renamed host, understanding a changed field's role, assessing a new feature, or diagnosing why a semantically reasonable patch failed.

This distinction is likely the central architecture point of the eventual post. "Multi-agent" does not mean replacing every shell command with a chatbot.

### Tooling reality

The project pins apktool 2.9.3 because these Instagram resources require the older aapt1 path. Android Studio supplied `adb`, `zipalign`, and `apksigner`, but did not put them on the shell `PATH`. The first reproducibility task was therefore simply locating every tool and recording its exact version/path.

### Temporal: durability without broader authority

Temporal replaced the earlier idea that an ADK session database should own multi-day pauses. The first Workflow is deliberately synthetic: admit a tiny run, prepare a tiny artifact, wait for a validated human Update, record the decision externally, apply one tiny effect, and return compact references. No APK or model call was needed to test the dangerous orchestration semantics.

The first restart test hung for a useful reason. Marking a Workflow `PINNED` does not make an isolated candidate deployment Current, and cached sticky execution can hide whether state really reconstructs. The final test uses Temporal's documented `PinnedVersioningOverride` to target the candidate build and disables Workflow caching so every task must replay History. It stops the Worker at the gate, starts a replacement, and resumes the same approval state.

Temporal Update identity and business identity are separate. Retrying the same Update ID returns its original accepted result; sending a new Update that reuses the decision's business idempotency key is rejected. The gate also binds the canonical run, admission artifact, prepared artifact, actor, policy, and validity interval, so a stale approval cannot authorize changed bytes.

At-least-once Activities required an explicit effect protocol. The ledger no longer overwrites a history row. It appends pending, effect, completion, or quarantine events and forbids update/delete in SQLite. An independent review then found that two attempts could both interpret the same pending event as permission to work. A transactional claim index now gives one attempt ownership; competitors cannot execute. If an injected failure occurs after the effect, retry validates and adopts that CAS object before appending completion. If cancellation arrives after the effect, the Workflow waits for Activity cleanup and appends quarantine instead of promotion.

History replay became a deployment test rather than a slogan. The test fetches and serializes a real History, reloads it, and replays it offline with unchanged code. Replaying the same History against an incompatible implementation of the same Workflow type produces Temporal's `TMPRL1100` nondeterminism error. A committed representative replay corpus is still future work.

The subprocess boundary is capability-based before any APK tool is connected. A request binds the canonical capability hash, immutable executable digest, exact argv template, permitted environment values, workspace paths, artifact kinds, mutation paths, and monolithic APK composition. Capability or executable mismatch fails before the launcher is called. This is a strong policy boundary around a trusted immutable binary, not an OS sandbox around a hostile one; portable pathname verification still has a replacement race unless the worker owns a non-writable tool store.

The persistence test first promotes `phase-a-v1` to the Current Worker Deployment Version and starts normally without a test override. It then gracefully stops the real dev-server process and restarts Temporal CLI 1.8.1 / Server 1.31.2 on the same SQLite file. Fresh Worker and trusted-client SDK connections find the Workflow at the same gate and complete it. That proves normal routing, graceful service persistence, and reconnection, but not yet abrupt process loss or authenticated authority from a separately launched client process.

An independent review caught two more important distinctions. Temporal attempt numbers reset across executions, so they cannot be recovery epochs. The ledger now owns a monotonically increasing fencing generation: a new owner can supersede an abandoned deterministic CAS claim across executions, while stale owners cannot promote. A future external side effect still needs destination-level idempotency or fencing. Schema creation and legacy backfill also run under one serialized transaction. The executor compares both the request and resolved capability with the capability hash stored in the admitted `RunSpec`, rather than trusting two self-consistent caller objects.

Cancellation during process creation had one final awkward branch: the launcher might return a child handle only after the bounded wait. The executor now reports unknown launch state immediately but retains a supervisor task; if the handle arrives while the Worker event loop remains alive, it kills and reaps that child. Abrupt Worker death still requires OS-level process supervision.

The hardened Phase A checkpoint has 35 focused tests and 97 Python tests overall. Remaining durability work is intentionally explicit: authenticated submissions, abrupt process-loss testing, non-idempotent fencing, replay-corpus CI, immutable tool storage/OS confinement and process-tree cleanup, and later signing/device secret isolation.

### Turning two one-off builds into data

Phase B forced the 340 reconstruction and 430 graft to describe themselves through one contract. Exact method ownership expanded 37 maintained 340 records into 45 method/register-scoped smali edits; with resources, manifest, overlays, and deletion, the fixture has 59 operations. The 430 fixture remains seven operations total: six host edits and one custom-DEX overlay. One compiler and applier now handle both without version branches, and tiny provisioned trees prove every operation applies once and adopts on the second pass.

The reviews repeatedly found useful distinctions that self-consistent tests missed: a fixed payload cannot preserve two different registers, a replacement may intentionally retain its anchor, an existing DEX needs descriptor containment rather than exact equality, and a decoded proof is meaningless unless it is hash-bound to the exact APK and an admitted decoder receipt. The most important rejected design was a convenient replay CLI that accepted tool hashes from the same caller choosing the tools. Equality to a caller assertion is not admission. Real replay now waits for ledger-owned attempts, admitted executor profiles, quarantine, and verify-before-publish rather than weakening the Phase A boundary for convenience.

## Facts Worth Capturing During Development

- Decode and index timings, output sizes, and class counts.
- Examples where filenames or DEX directories changed but descriptors remained useful.
- The first hook successfully recovered from the 1.4.1 oracle.
- Failed fingerprints and why they failed.
- Screenshots of the workflow, Temporal UI, reports, and human approval gates.
- Before/after snippets showing a huge host class collapsing into a tiny normalized delta.
- The first successful 340 rebuild, first install, and first verified blocked feature.
- Anything surprising, annoying, or funny enough to make the final post feel like a development story.

## Glossary Candidates

- APK: the installable Android application archive.
- smali: a readable assembly-like representation of Android DEX bytecode.
- hook: a small injected call that redirects behavior into DFInsta code.
- oracle: the known human-built modified APK used to validate reconstructed behavior.
- fingerprint: stable evidence used to relocate a class after obfuscation changes its name.
- anchor: the exact semantic location where a hook should be inserted.

## Timeline

### 2026-07-25

- Reviewed the repository, corrected stale architecture documentation, and established that the sensible route is 1.4.1 reconstruction, then a 430 port.
- Reviewed the proposed pipeline. The sequence is viable, but durable orchestration should own execution while LLM agents handle only ambiguous reasoning.
- Started the 1.4.1 reconstruction with fresh stock and modified APK decodes in an isolated ignored workspace.
- Both decodes finished in roughly 32 seconds. The previous Windows experiment recorded around 10 minutes for a similar decode, making environment and filesystem performance an unexpectedly visible part of the story.
- The first new script failed in zero seconds because Linux had `python3` but no `python`. A suitably unglamorous reminder that automation projects often begin by discovering which spelling of Python the machine understands.
- The first real inventory found 92 added classes, which initially sounds like a fairly large custom implementation. Thirteen were DFInsta classes; the other 79 were a bundled crash-reporting library. Even "new classes" need classification before they become useful evidence.
- Fresh analysis corrected the old conclusion that 1.4.1 merely kept URL blocking and removed brittle response code. It did remove the old response rewriter, but replaced it with 19 host patches that swap Instagram endpoint constants for tiny preference-aware methods. Searching only for old hook names had missed the new architecture entirely.
- The endpoint trick is wonderfully direct: replace `const-string "feed/timeline/"` with a method that returns either `"feed/timeline/"` or `""`. No giant JSON surgery, just make the internal route disappear when its toggle is enabled.
- One less-obvious oracle finding: app startup sends Android ID and the DFInsta version to Amplitude. Reconstructing what the old release did and deciding what a future release should do are deliberately separate questions.
- The modified APK produced 188 changed classes after normalization, but only 23 directly reference DFInsta. Many of the rest are bytecode round-trip noise: duplicate exception-table entries disappear or string instructions switch to their `jumbo` encoding after the DEX string pool changes. A raw diff can be technically accurate and still tell the wrong story.
- I initially hand-counted the 92 added classes as 12 DFInsta plus 80 ACRA. The source bootstrap reported 13 plus 79 and forced the correction. This is a tiny example of the broader rule for the eventual pipeline: prose explains evidence, but generated inventories own the numbers.
- The first complete reconstructed APK built successfully: all 11 DEX files plus resources, 84 MB in total, in about two minutes and forty seconds. Unlike the earlier four-hook experiment, this build includes the recovered custom classes, ACRA dependency, resources, manifest entries, and all 23 direct host patches.
- `apksigner` then announced `DOES NOT VERIFY`, which sounds dramatic but was exactly right: this was intentionally the unsigned intermediate. Assembly, signing, installation, and behavior are separate gates so one green check cannot impersonate the others.
- After alignment and debug signing, `apksigner` verified the APK correctly. `adb devices` then showed absolutely nothing, so the pipeline reached its first genuinely physical dependency: eventually a phone has to be plugged in.
- The first complete build still used exact copies of 23 oracle host classes as scaffolding. Once that proved the recovered custom code and resources were complete, those copies were deleted and replaced by 37 explicit declarative records: 30 endpoint substitutions and seven anchored lifecycle/settings/startup operations. Those 30 endpoint records expand against the real decode into 38 method-scoped operations, so the compiled 340 resolution holds 45 Smali edits in total.
- Running the patchers twice became a useful tiny test: the first run reported every operation applied; the second reported every operation already present. Idempotency is boring right up until an interrupted automation run applies half a smali hook twice.
- The source then built again without any complete patched Instagram classes. That was the real reconstruction milestone: stock 340 plus small declared deltas produced an APK with the same required hook contract as the human-built oracle.
- Finally, the whole deterministic sequence was wrapped in one command and run from scratch again. It prepared the tree, applied all 38 operations, assembled 11 DEX files, built resources, and verified the hook contract in just over two minutes. The agentic pipeline now has a boring, testable mechanical core to call rather than a prompt that says "please patch Instagram carefully."
- The connected phone unexpectedly contained the exact historical oracle APK, byte for byte. That turned a signing obstacle into a behavioral reference device: capture what the old app actually does before replacing it with the reconstruction.
- The first attempt to open settings directly through ADB failed because the activity is not exported. The next assumption was also wrong: long-pressing the bottom Profile tab opened Instagram's account switcher. The real route was Profile, then a long-press on the top-right Options control. Static class context narrowed the search; runtime accessibility evidence settled it.
- The oracle settings screen showed exactly five enabled switches. There was no suggested-post switch and no profile-ad switch, agreeing with the static resource analysis. Agreement between independent static and behavioral evidence is much stronger than either one alone.
- After explicit approval, the oracle was uninstalled and the reconstruction installed. It reached Instagram's first-run screen and stayed alive without a fatal startup trace: the first true runtime proof that the delta-built APK was more than assemblable bytes.
- The old UI test expected the word `Password`; the current first-run screen says `Join Instagram` and `I already have a profile`. Even a valid test can become a false alarm when it confuses one transient UI state with the underlying startup contract.
- The reconstructed welcome dialog appeared after login, and both its Settings button and the profile long-press route opened the same settings screen. A feed toggle survived a full process restart and was then restored.
- One apparent reconstruction failure was just lazy UI rendering: the profile's hamburger button was missing until a small human swipe made the action bar appear. The lesson for automation is not "sleep longer"; it is "stimulate the known state transition, then poll for the semantic control."
- Another apparent failure came from testing too soon after a preference change. A feed post remained visible until the process restarted; afterward feed and Explore were empty and Reels showed a handled error instead of crashing. The eventual test harness needs restart boundaries as part of the feature contract, not as flaky-test folklore.
- Story blocking is also more nuanced than "the tray disappears": the user's own story remains. Behavioral assertions must describe what is intentionally removed rather than demand a visually empty screen.
- On/off comparisons made the feature evidence much stronger. Explore changed from an empty search shell to a populated grid; Reels changed from a handled error to playing video; feed posts disappeared after the required restart. The settings were restored afterward.
- Enabled Reels exposed another test-harness trap: UI Automator refused to dump because a playing video never became idle. Continuous media needs screenshot/process assertions or a non-idle-aware driver, not a test that waits forever for calm.
- The behavioral conclusions were also written into a small JSON contract. Agents can still reason about ambiguous mappings, but they should not have to reread a blog-style journal to learn that settings require a restart or that the Options button is lazily rendered.
- Stories produced the expected restart-bounded contrast without opening anything: enabling showed three other users' unseen entries; disabling left only the current user's own story.
- Shopping exposed the kind of bug a UI checkbox cannot reveal. Its helper checks for `minshop`, while all three patched identifiers contain `minishops`; the direct substitutions therefore preserve every string regardless of the toggle.
- Hardcore Mode really is hardcore: once enabled, the same listener blocks turning Hardcore itself back off. That test belongs on disposable app data, not a logged-in reference installation.

### 2026-07-26

- Adopted Temporal `1.30.0` as the durable outer orchestrator while keeping Google ADK deferred to bounded read-only Activities after deterministic generalization.
- Diagnosed pinned candidate routing and sticky-cache behavior, then proved Worker replacement at a waiting approval gate through explicit deployment override and forced History reconstruction.
- Replaced mutable operation history with append-only pending/effect/completion/quarantine events, then added atomic owner claims after review exposed a concurrent-attempt hole.
- Added a fail-closed executor capability model before connecting any APK tool: admitted capability hash, executable digest, argv, environment, workspace, artifact kinds, mutation audit, timeout cleanup, and split-APK rejection.
- Restarted Temporal CLI 1.8.1 / Server 1.31.2 on the same SQLite state and resumed the gate through fresh Worker/client connections.
- Added retry-safe abandoned-claim recovery, legacy-ledger migration, admitted-`RunSpec` executor binding, strict terminal-result invariants, exclusive gate-expiry validation, and cancellation-safe process creation.
- Replaced attempt-number takeover with ledger-owned cross-execution fencing, serialized concurrent migrations, and supervised late subprocess handles.
- Replayed fetched History successfully, deliberately triggered nondeterminism with incompatible code, and reached 35 Phase A tests plus 97 passing Python tests overall.

---

## The bug that kept happening (the strongest thread in the whole story)

If the post needs one spine, this is it. The same species of bug appeared at least a dozen
times in two weeks, always with the identical signature: a module — or a value, or a
registration — fully implemented, heavily tested, passing, documented as done, and **connected
to nothing**. Nothing produced its input, or nothing consumed its output, or nothing could
invoke it.

The project ended up coining its own phrase for it, and the phrase is in shipped source
(`src/dfinsta_pipeline/assessment.py`): *a value that exists on disk and reaches nothing is
**a wire that looks connected**.* And the reason it kept recurring is in `rulings.py`: *every
piece is complete, tested and green, so the chain **looks** finished.*

A partial inventory, with how long each gap lasted:

| The thing | Gap |
|---|---|
| `feature_gate.py` — 624 lines, 54 tests, one importer: its own test file | 2 days |
| The gate's rulings had no consumer — a human could rule and nothing read the verdict | 1 day |
| `suppressed_candidates` had zero callers — *inside the module written to fix disconnection* | same day |
| `assessment_record.record` had zero callers, so no port ever produced an assessment | 1 day |
| `rulings.py` had a `main()` and no `__main__` guard: `python -m …` ran nothing, exit 0 | same day |
| **14 Temporal Activities registered, 0 of them runnable** | 4 days |
| `claims.py` — the recovery tool — zero importers *and* zero tests, marked done | 1 day |
| `EvidenceKind.DIFFERENTIAL` — *required* of every hook since the ledger's birth, never produced | months |
| A written-down loose end that **named the wrong module** | same day |

That last one is the most interesting for a post, because it is the failure mode *of the fix*.
A note said "stage 3 is a standalone CLI the driver never invokes; nothing schedules the thing
that decides what to assess." Both sentences were true — and about **different modules**.
Fixing what it named would have left the real gap open while the list said it was closed.

Two lessons the project actually adopted:

- **Check the ends of a chain by grep, not by reading the module.** A module is not done when
  its code is done; ask what produces its input and what consumes its output.
- **Run the documented command.** The 14-unrunnable-Activities bug survived four days and an
  exhaustive registration test suite. It took under a minute to find once someone actually
  started the worker: it registered every Activity and could supply neither the source root nor
  the executor path that three of them require.

There is a counterweight worth including, because it stops the lesson being "connect
everything". `surface_diff.py` — 1,155 lines, 95 tests — is invoked by nothing, and the audited
answer was **not to connect it**. Of the 105 candidates from its one real run, 44 were
permalink spellings for a single surface the app already blocks, and putting 105 candidates
through a gate proven at 4 would have turned "every candidate must be ruled on" into a rubber
stamp — the exact check that exists to prevent that. Its one genuinely actionable finding was
reachable by fixing a different stage instead.

## Rules that looked deliberate and turned out to be residue

Twice in one day, both in cancellation handling, both found by asking "why is this like this?"
and getting no answer from the code, the comments, the commit messages or the docs.

Three of five replay stages quarantined an operation unconditionally when cancelled — which is
terminal, requiring a new run id and a new human approval to recover — while the other two
released it if nothing had started yet. The git archaeology is almost comic: **it is a date,
not a decision.** Everything written on 2026-07-28 got one shape; everything from 07-29 on got
the other. Zero tests pinned either behaviour for the entire life of the drift.

The better half of the story is what happened next. An adversarial review established that the
differing branch was **unreachable** — cancellation in Python asyncio is delivered at a
suspension point, and there is no `await` between claiming an operation and creating its
workspace, so the cancellation is latched until the launch by which time both shapes behave
identically. The change was still right, but it changed nothing that runs, and the real
temptation was to tick the roadmap box it *looked* like it closed. Leaving a green checkbox
over a live hazard is worse than leaving the box unticked, because the next reader stops
looking.

## Measurement beat argument, repeatedly

The most quotable numbers, all from real runs rather than estimates:

- **The worker could not answer anything while a stage ran.** 66 of 86 query samples went
  unanswered on Instagram 340 and 113 of 144 on 430; the longest unbroken blocked stretch was
  560.9 s (9.35 min) on 340 and 691.4 s (11.5 min) on 430. The worker logged 191
  `query task not found, or already expired` warnings across the two runs. Cause: the stages
  walk, hash and re-write tens of thousands of files synchronously on the event loop.
- **That falsified a written design claim.** The docs had argued heartbeats were a small change
  "because every long operation inside a stage yields the event loop". Measured: the decode
  stage contains no `asyncio.to_thread` at all. The retraction is a commit —
  `Retract the claim that a wrapper heartbeater would work`.
- **The addictiveness calibration.** Six of seven candidate signals were noise, and the random
  control landed *between* the two labelled groups. That is why the assessment has no composite
  score — a scored list would be "here is the top of a ranking", which is a claim nothing
  supports.
- **Agent invocations per port: 439 → 2, 440 → 0.** 440 arrived *after* the fingerprints that
  resolve it were written, and ported with one command and no agent calls at all. Two points is
  a fall; the project's own roadmap says a third is needed before it is a trend.
- **`by_anchor` selecting 1 class out of 182,479** on a version it had never seen.

The practice underneath all of this: copy the source out of the tree, mutate it, and check the
tests actually fail. It found roughly two dozen real defects, several in code written minutes
earlier — including, on one memorable occasion, six gaps in a test suite that had just caught
10 out of 10 deliberately introduced bugs.

## Corrections — things a post should not overstate

Collected deliberately, because getting these wrong would be worse than omitting them.

- **The current test count is 2,672**, not 2,170. The 2,170 figure is a real snapshot from
  2026-08-02 and belongs only to the anecdote about that day ("2,170 tests and the runtime-probe
  module had none").
- **"100% loop-blocked" is a derived complement**, not the recorded measurement. What was
  recorded is "probe ticks served: 0 of ~6,000". Prefer the raw form.
- **The threading benchmark is not reproducible from the repository** — no benchmark script was
  committed. Treat those figures as a one-off measurement, or replace them with the live-run
  numbers, which are reproducible.
- **The cost metric counts hooks that needed an agent, not model calls**, and the count fell
  because a *human* wrote the `by_anchor` fingerprints from what 439's agents had cited. The
  generaliser proposes; it does not commit.
- **430 and 439 share an architecture**, so agreement between them is closer to one data point
  than two. The docs say so themselves. 440 is the genuine third.
- **Several claims in the docs are self-corrections** — one commit retracts a heartbeat claim,
  a later one corrects that correction, another retracts a note that "had it backwards". These
  are not embarrassments to hide; they are the measurement discipline working, and they are
  probably the most honest thing in the repository.

## Timeline (continued)

### 2026-07-27 → 08-01 — from one port to a pipeline

- The hook manifest replaced hand-authored per-version resolutions: anchors became patterns
  with typed captures, so a payload templates off whatever the anchor matched. Five of seven
  hooks resolved mechanically on both 430 and 439 immediately; the two settings hooks did not,
  and the tier taxonomy had predicted exactly that split.
- A blind holdout answered the question the whole roadmap hinged on: *can a mapper rediscover
  the anchors unaided?* Yes — including the hard settings site, found by two provably
  uncontaminated mappers from an isolated stock decode. Verified from agent transcripts rather
  than from denials.
- "Presence is not execution" arrived as the lesson that reshaped everything. Four separate
  failures — a `minshop`/`minishops` substring mismatch, the 430 settings hook, the 439
  action-bar hook, and a verifier searching for a string DEX does not store — turned out to be
  one failure: *something present and never reached*. Adding a check per incident would always
  have been one version behind, so every hook now announces its own execution. On the first run
  carrying that, two more hooks turned out never to run.

### 2026-08-02 → 08-03 — the decide machine

- Stage 4 was built to answer "is this new feature addictive?" from evidence rather than
  assertion, after a calibration experiment ruled out a composite score.
- The durable human gate landed: dispositions go to content-addressed storage, the decision
  binds their hash, and every candidate must carry a ruling — one nobody ruled on blocks rather
  than defaulting to ignore.
- The design point worth stealing: **the update validator is a filter, the Activity is the
  authority.** A Temporal update validator runs in a sandbox with no I/O, so it cannot read the
  document the rulings live in. The admitting Activity therefore re-derives the request from the
  ledger rather than trusting the workflow's copy. The first tests for it found the authority
  was checking *less* than the filter.

### 2026-08-04 — the first real port through the registered workflow

- Instagram 440 ported itself for zero agent invocations, was device-proved, and shipped
  through the release gate.
- Both 340 and 430 ran end to end through the registered Temporal workflow on a live server for
  the first time. It found two defects in its first minute, having passed every unit test.
- History stayed at 64,563 bytes of a 256 KB budget — the design that passes the admitted
  replay by value would have carried over 510 KB of recipe and source paths through Temporal
  history on every stage.

### 2026-08-05 — hardening, and the decide machine gets sharper

- A leading slash was hiding an entire grouping. The manifest normalises `/clips/discover` to
  `clips/discover`; the index holds the app's own spelling. So the lookup found nothing, and
  the class it should have found also held `delivery/background_prefetch` — a surface absent
  from 430, present on 439, and the one signal family that survived the addictiveness
  calibration. Stage 4a went from 4 candidates to 6.
- Better still: the *other* stage had already flagged that endpoint independently, riding with
  the same two Reels endpoints. Two stages built on different evidence agreeing on one
  candidate.
- Cancellation became non-destructive, gated on whether the subprocess could be *proven* dead.
- The decoded-tree walks moved off the event loop. Mid-decode queries that used to time out now
  answer in ~110 ms.

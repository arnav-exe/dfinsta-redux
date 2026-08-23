# Exploration first: decide what to block after watching the phone, not before

DFInsta blocks by failing outgoing requests. One generated method,
`throwIfBlocked` in `dfinsta_source/newCode/com/dfinstagram/hooks.smali`,
takes the outgoing `java.net.URI`, tests `getPath()` against a list of literals,
and throws `IOException` when the matching rule's toggle is on. There are five
toggles — `disable_feed`, `disable_explore`, `disable_reels`, `disable_stories`,
`disable_adds` — and today nine rules over eleven literals, all declared in
`manifest/hooks.json` under `url_block_rules`.

This document describes how a new endpoint gets from "a string in the decode" to
"a literal under a toggle". The short version: it has to be seen happening on a
phone first.

## What we were doing, and what it cost

The old route was static end to end. `tools/indexer/build_index.py` scans the
decompiled app for strings that look like API paths, a stage groups them by the
class they were found in, and a human rules `block` or `ignore` at a gate whose
entire evidence is "this literal was found in a class alongside two endpoints you
already block". Wrong rulings were corrected afterwards through a reversal gate —
machinery that was deleted on 2026-08-08 along with the approach, having recorded
nothing in its lifetime.

On 2026-08-08 that route produced six `block` rulings in one sitting, all six
recorded under one decision id in `manifest/rulings.jsonl`, all stamped
`2026-08-08T15:00:00Z`. Here is what those six turned out to be worth.

**One was not an endpoint.** `delivery/background_prefetch` appears exactly once
in the whole 441 decode, at `LX/01qe:6023`, where it is passed as an event *name*
to `LX/04mq;->A0C(String, String, int)` — and `LX/04mq` is a stub whose every
method is `return-void`. `throwIfBlocked` tests `URI.getPath()`, so this string
can never be in a path, and a guard for it would have been inert by construction.
It reached a human gate because `looks_like_api_path`
(`tools/indexer/build_index.py:167`) is a pure test of string *shape*: length
between two bounds, contains a slash, no uppercase or whitespace, at least one
segment of some minimum length, not a bare MIME type or URI scheme. Nothing
anywhere checks that the literal is ever used as a URI. It was one of 3830
entries admitted on those terms. The rationale written at the gate was also
wrong, and wrong in the way that a grouping tool invites: it said the literal sat
in a three-literal class beside two already-blocked siblings, when in fact
`LX/01qe` is 6000 lines long and those siblings are at lines 504 and 508,
unrelated to line 6023.

**The other five were built correctly and change nothing.** All five now have
guards; `rulings --audit` went from six unenforced to one. Every one of them
fires zero times on the owner's phone. The strongest of those zeros,
`feed/timeline_stream/`, was measured properly — a throwaway diagnostic build
with its own throw message, zero hits on a short arm and zero across a 22-scroll
session, against a positive control that rebuilt the same branch pointing at
`/feed/reels_tray/` and saw it fire 12 times. The other four are zero in the one
observation session on record.

So: a day of gate work, six decisions, five patches, one endpoint that does not
exist, and no measurable change in what the app receives.

The rest is worth noting because it is the reason the failure was not caught
sooner. A whole-log differential over four runs of each build read
`FEED_NOT_LOADING` at 19/20/21 before and 18/17/18/17 after — which looks like an
effect until you notice `STORY_NOT_LOADING`, a category the change cannot touch,
fell by the same proportion (13/14/14 → 12/11/12/9). That unrelated category was
a free negative control and is the only reason a −2.5 movement was not written up
as a result.

## The instrument already exists

`src/dfinsta_pipeline/guards.py` renders `throwIfBlocked` from the declaration in
`manifest/hooks.json`. Passing `observe=` gives the method an extra pass that
tests each watched literal with `contains` and calls
`Lcom/dfinstagram/observe;->seen(Ljava/lang/String;)V`, which does nothing but
`android.util.Log.i("DFInstaObserve", literal)`. `python -m dfinsta_pipeline.driver
--observe` composes such a build: same rules, plus the pass, plus the observe
class.

`src/dfinsta_pipeline/observation.py` is the host side. It parses a logcat
capture into a session row in `manifest/observations/<version>.jsonl`, keyed on
version, build SHA-256, timestamp, session id, the surface walked, the full watch
list, and the toggle state the build reported. `never_observed(version, root,
toggles=...)` returns the literals that were watched in at least one non-vacuous
session **measured under exactly that state** and never once seen; the state is a
required argument, `states()` lists the ones on record, and there is deliberately
no whole-version answer. It refuses in four distinguishable ways: nothing
recorded, everything recorded saw nothing, everything that saw something states
no configuration, and nothing was measured under the state asked about.

Two properties are load-bearing and both are enforced by tests rather than by
intention.

**Observation runs before any rule can throw.** A throw ends the method, so a
pass placed after the rules would report zero for exactly the paths that are
working — the ones killed before they could be counted.
`tests/test_guards.py::test_observation_runs_before_any_rule_can_throw` asserts
the ordering directly.

**An observing build blocks exactly what a shipped build blocks.** If observation
could alter one instruction of the guard, every number taken with it would
describe a different app while being quoted about the shipped one.
`test_an_observing_build_blocks_exactly_what_a_shipped_one_blocks` compares the
rule spans of both renderings, with
`test_the_span_comparison_would_catch_a_changed_rule` as its control.

That second property has a consequence worth stating plainly, and one the module
docstring of `observation.py` got wrong until 2026-08-10 — it said observe mode
"blocks nothing". It does not: it blocks precisely what the shipped build blocks.
**An exploration session with the
blocks off is produced by turning the five toggles off in the app, not by
building a different APK.** And the toggles default to *on*:
`getBoolTrueEz` is `getSharedPreferences("com.instagram", 0).getBoolean(key,
true)`, so a fresh install of an observing build blocks everything until somebody
switches all five off and restarts the process. Setting changes are not effective
until a process restart; Instagram's own caches keep serving the previous state.

## Where the device run sits in the pipeline

Added 2026-08-14, and it changes the shape of a port: **the phone is walked
between finding the candidates and ruling on them.**

**`tools/port.py` runs the mechanical half of this**, resumably and stopping before the
judgement — it reports by default and executes with `--run`. The sequence below is what it
does, and what to type if you would rather do it a step at a time.

```
driver --stop-after index                     find the candidates
tools/watch_candidates.py --index … --apply   put them on the watch list
driver --observe --stop-after build           an observing APK that watches them
  ; sign ; adb install -r
tools/run_corpus.py one-pass-v1    … both     walk the phone, twice
tools/run_corpus.py three-round-v2 …
tools/record_corpus.py --version … --walk …   commit rows + verified redactions
driver --stop-after assess                    the assessment now carries what the phone did
assessment_record raise ; submission submit   the human rules
rulings --apply                               the rulings reach manifest/hooks.json
driver                                        the shipped build
```

The candidate list is computed **twice** on purpose — once by
`watch_candidates.py` to know what to watch, once inside `assessment.document`
to record with evidence. `assess` is pure and pinned deterministic, so the second
derivation cannot disagree with the first, and the alternative is handing
`assessment` a callback that reads the disk, which is exactly what its
determinism under Temporal replay depends on it not having.

**Why the phone is on the critical path at all.** Because the gate now refuses to
`block` or `offer_toggle` a candidate no device has looked for. That restriction
is narrow on purpose: `ignore` and `defer` stay available, so a port can still
reach a decision without a phone — it just cannot reach a decision that *changes
what ships*. The cost was weighed on 2026-08-08 and accepted on 2026-08-14.

**What the restriction is not.** It is about *looking*, never about what was
found. A path watched across seventy-two sessions and never once requested is
measured, and stays fully blockable — `feed/timeline_stream/` is requested zero
times and blocking it is right, because it is in Instagram's own list of
continuous-feed paths and the routing that decides what an account sees is
server-side. A zero is weak evidence, not a veto.

## The protocol

**1. Find candidates as before.** The string scan stays. It is a cheap net and
its false positives are now caught downstream rather than at a gate.

**2. Build one observing APK per version.** One build serves the whole
exploration: the watch list is `watched_literals(rules, watch_from_manifest(...))`,
which is every literal already blocked *plus* every unruled candidate in
`observe_watch`. Both halves matter. A blocked literal that is never once asked
for is the evidence that a recorded decision should be revisited, and that
cannot be produced by watching only the undecided. `observation.blocked_and_never_observed`
is the query. On the committed 441 corpus it currently **refuses**: that session
predates builds stating their own toggle state, so its zeros cannot be told apart
from zeros our own blocks caused. It named seven until 2026-08-10; that list was
a measurement of our configuration.

**Every capture states its own toggle state.** An observing build logs, on every
checked request and ahead of the path lines that request produces:

    I DFInstaObserve: !toggles disable_feed=1 disable_explore=0 disable_reels=1 ...

That line is read from the device, not typed by whoever ran the session, and the
distinction is the whole reason it exists. A measurement taken with the blocks on
cannot answer "is this endpoint ever requested" — blocking `/feed/timeline/`
leaves no timeline response for Reels to be injected into, so
`/feed/injected_reels_media/` never fires whatever Instagram would otherwise do.
Measured on 2026-08-08: 0 observations with the blocks on, 3 with them off, same
build and same walk. There is a second and independent route to the same wrong
answer — `replaceReelsEndpoint` blanks the endpoint string before the URL is
built, which is also before the observe pass runs, so `disable_reels` suppresses
those paths for a reason unrelated to traffic.

An operator-supplied toggle state would be a formality rather than a safety
property, the same shape of mistake as deriving a retirement's effective version
from a flag the same person typed. So `observation record` has no `--toggles`
flag, and a capture carrying path lines with no `!toggles` line **refuses**
rather than defaulting to "all off".

The line repeats on every checked request rather than once per process, because
the once-per-process version failed on the first real session and failed
silently: `adb logcat -c` immediately before walking, with Instagram's process
already alive, cleared the one line that had been emitted and left the static
flag set — 22 path lines and no statement of what was active. Restating it buys
the invariant *any capture holding a path line also holds the toggle state*
(`ToggleDirectiveTests.test_any_capture_that_counts_a_path_states_its_toggle_state`),
and it makes a toggle changed halfway through a session contradict itself in the
file instead of being invisible; `parse` refuses that too.

**3. Run one session with all five toggles off, and restart first.** This is the
session that says what the app does when it is behaving like stock. It is the
most important measurement in the protocol and the section below explains why.

**4. Candidates never requested in that session go to "recorded, not built".**
No toggle, no rule, no ruling — a row in `manifest/observations/<version>.jsonl`
and a note naming the app version and the surfaces walked. This is the group the
old process would have shipped five guards for.

**5. Candidates that do fire get an isolation session.** Block exactly that
literal, walk the app, and record two things: what the UI actually did, and what
the request log shows. Neither alone is enough — see below.

**6. Endpoints that kill the same thing get grouped, and the group gets a
toggle.** The mapping from endpoint to toggle comes out of step 5, not out of
reasoning about what the endpoint's name suggests. `disable_reels` covering five
literals today is a claim about behaviour that has never been tested as one.

## Why the all-off session is the load-bearing one

A measurement taken with the blocks on can be circular, and the clearest case is
already in the manifest. `/feed/injected_reels_media/` is Reels injected into the
timeline. If `/feed/timeline/` is blocked there is no timeline response for
anything to be injected into, so the child request never fires — **because of our
own configuration**, not because the app would not make it. A session run in that
state and then read as "never observed" is a measurement of the experiment.

The one session on record has exactly this shape. `441-long-multisurface` in
`manifest/observations/441.jsonl` watched 16 literals across
`feed_explore_reels`, saw 52 requests, and saw them across only four paths:
`/feed/timeline/` 28, `/feed/reels_tray/` 20, `/discover/topical_explore` 2,
`/clips/discover` 2. Every newly-guarded literal is zero, including
`/feed/injected_reels_media/`. Nothing in the record says whether the toggles
were on — it was recorded before builds stated it — so every one of those zeros
is unreadable and `never_observed` refuses to answer from it under any state. It
cannot be repaired by writing the state in now: that would be the
operator-supplied state this design refuses, from memory, into an append-only
store. The session has to be walked again.

There is a second, independent route to a circular zero that is easy to miss.
`replaceReelsEndpoint` blanks the endpoint string — it returns `""` when
`disable_reels` is on — at the `const-string` site, which is upstream of the URL
being built. So with `disable_reels` on, the URI never contains the Reels path at
all. It is upstream of the throw, which is why Reels blocks cannot be counted by
exception; but it is equally upstream of the *observe pass*, so an observing
build reports zero for those paths too. A Reels literal reading zero with
`disable_reels` on is not evidence about Reels traffic. (`clips/discover` reading
2 in the session above is consistent with `replace_reels_discover_endpoint` being
one of the three hooks that has never passed a probe on this device.)

## Two instruments, and both of them are ours

**The observe line counts what the app asked for.** It is emitted by our code, in
the request path, before anything can interfere with it, and it is the only
signal in this system that cannot rot into a false pass.

**The `!blocked` line counts what we refused.** Also ours, emitted by the guard at
the moment it decides to throw, naming the literal that matched. "Did this rule
fire, and how often" is therefore known.

That second one is new as of **2026-08-13** and it replaced a dependency that
should never have existed. Ask, of any instrument: **who generates the event, and
who records it?** Whether the app *requests* a path is Instagram's and is the
thing being measured. Whether our guard refused it is ours — and so is whether
that was written down. The third was given away for convenience, because
`java.io.IOException: Blocked by DFInsta setting` was already in logcat and cost
nothing to read.

**What it cost.** `IgFunctionalErrorEvent` is emitted at Instagram's discretion,
and it under-reports **by feature**. `/discover/topical_explore` under
`disable_explore` was refused 7, 6, 12 and 6 times across eight sessions on two
Instagram versions and two walk protocols, and reported 1, 0, 1 and 0 — while
`/feed/timeline/` reported 20/20, 23/23, 17/17 and 16/16 in the very same
captures. Explore was simply unmeasurable through that channel, and the loss was
stable rather than noisy, so no amount of inference recovered it.

It also names a **feature** and never a path, so a path had to be named by
arithmetic over the capture's block total. That derivation is deleted. It refused
a perfect 17-refusals-of-17-requests on `/feed/timeline/` because `7 + 7 + 3 = 17`
among three unrelated paths, and it got *less* discriminating as walks got longer
and counts got higher — which is most of why two walks of one version disagreed.

`probes.py` still exists, and counting those events correctly is still harder than
it looks: a payload has a message body, an indented stack, and indented
`field = value` entries, and a field value that spans lines logs its continuations
*un-indented* under the same tag, so raw grep over-counts by roughly two. The
count is still parsed and still printed beside ours, labelled, so a reader can see
the two disagree. **Nothing is derived from it.**

**A build says what it can report.** The toggle line reads
`!toggles +blocked disable_feed=1 …`, and a capture with no `+blocked` mark is a
build that could not have written a refusal line. Its silence is not a zero. Every
session recorded before 2026-08-13 is in that shape, so the block half of the 439
corpus is unanswerable until it is walked again — and unlike the walk, which the
captures could supply, nothing can be back-filled, because the lines were never
written.

**UI judgement is a third instrument and the least reliable of them.** A
warm cache renders a blocked feed as populated until the process restarts. The
user's own story stays in the tray with `disable_stories` on, so "the tray is
still there" is not a failure and the assertion has to be about *other* entries.
And Reels cannot be judged by block count at all, for the `replaceReelsEndpoint`
reason above. Always pair "did the feature break?" with the request log; a
question answered by one of them alone has been answered wrong here before.

## What this cannot tell you

**Never observed is bounded by the surfaces walked.** A path only the Reels
player requests is not observed by a session that stayed on the feed. `surface`
is recorded per session and every report repeats the list, because the reader's
first question has to be "would this session have seen it if it happened?".

**It is bounded by the account and by Instagram's server-side configuration.**
Much of what a client requests is decided remotely. A MobileConfig flag choosing
the other implementation is how a statically perfect 430 settings hook came to be
dead at runtime, and nothing on the host side can see that decision being made.

**A never-observed endpoint can still be correct to block.**
`feed/timeline_stream/` fires zero times on this account and the guard stays,
because the literal is in `LX/02nZ` on 441 — a class whose `<clinit>` builds a
seven-element list of the continuous-feed API paths and does nothing else — and
`LX/03hm` matches those against `URI.getPath()` with `indexOf(...) >= 0`. This
account is simply not routed there. The guard costs one `contains` and closes the
path the moment the routing changes. *Declared implies guarded* is not *guarded
implies effective*, and only a device session separates them; but neither does
*not observed* imply *should not be blocked*.

**A session that observed nothing at all is not evidence.** It is equally well
explained by the installed build not being the observing one, an empty or
mis-targeted capture, an app that never ran, and every watched path genuinely
going unrequested. Only the last is a finding and nothing in the capture
distinguishes them. `observation.never_observed` therefore *refuses* rather than
returning `()` when no session is evidence, because an empty tuple is the same
answer it gives when every watched path was seen.

**The observe pass tests `contains` while a rule may test `endsWith`.** That
asymmetry is deliberate and it is conservative in the right direction: if a
containment test never saw the path, an `endsWith` rule certainly never would.
It is not conservative in the other direction — "this literal was observed" does
not by itself mean the rule as written would have caught it.

**And this measures; it does not decide.** `observation report` names the blocked
endpoints that were never once requested. Turning one of those into a decision is
a human act, and deliberately has no machinery behind it: deciding late is what
this design substitutes for a correction path.

## What this design does not yet resolve

**~~The session record cannot say which toggles were set.~~** Resolved on
2026-08-10. `ObservationSession.toggles` carries the state the build reported;
`never_observed(version, root, toggles=...)` takes it as a required argument and
answers over the sessions measured under exactly that state, `states()` lists
what is on record, and `observation report` answers each state separately with a
caution naming any that had a block on. Four refusals rather than one, because
"nothing was recorded", "everything recorded saw nothing", "everything that saw
something predates the field" and "nothing was measured under that state" have
four different fixes. What is left is the corpus, not the code: the only 441
session on record is the third of those.

**Step 5 cannot be done with toggles alone.** "Block exactly one literal"
requires that literal to be the only one under its toggle, and `disable_reels`
now covers five. Isolating one means either a throwaway build in which the
literal is the sole rule under its toggle, or a diagnostic build
(`render_method(..., diagnostic=True)`, which gives every rule its own throw
message and must never ship). Either way step 5 costs one build per candidate
group. That is the real argument for step 3: the all-off session is what stops
you paying for builds on candidates that were never going to fire.

**~~Nothing derives grouping from measurement yet.~~** Resolved on 2026-08-10.
`src/dfinsta_pipeline/grouping.py` takes the baseline and the one-toggle-on arms
out of `manifest/observations/<version>.jsonl` and derives, per watched path,
`erased by T` / `blocked by T` / `unaffected` / `never_requested`, refusing with a
reason where the corpus cannot decide. `grouping report --version 439 --walk <name>
[--json]` prints it. It is a **view** — recomputed every time, stored nowhere, and
acting on it is still a human editing `url_block_rules`.

Two things it needed that were not here before. `observation.parse` now also
counts the `java.io.IOException: Blocked by DFInsta setting` headers, because a
path block does not lower a request count — `/feed/reels_tray/` goes 2 → 3 under
`disable_stories`, which is inside the spread two runs of one state produce — so
the request log alone cannot see a block at all. And the noise floor is derived
from the corpus's own within-state spread rather than declared, which is only
possible because every state was walked twice.

**And a third, which the walk change forced.** "The same experiment, run again" is
doing all the work in that last sentence. On 2026-08-11 the driving script went
from one pass over three surfaces to three rounds and the 440 baseline went from
11–16 observed requests to 25 — so two sessions of one state, walked differently,
spread by 14 for a reason no toggle caused, and a floor derived from that spread
swallows every real effect underneath it. A session therefore names its `walk`,
and `grouping.classify` takes it as a **required argument** rather than pooling
and refusing later. `observation.never_observed` deliberately does *not* partition
by walk: it makes a negative claim, and pooling a second walk into one can only
give a path more chances to be seen.

The walk is the one field an operator types — it is a property of the driving
script, and nothing on the phone or in a capture names it — so the docstring says
so instead of implying the guarantee `toggles` has. What a capture does carry is
logcat's timestamps, and `parse` now measures the **span** from them.
`observation.walk_dispute` refuses a walk whose sessions' spans split into two
groups that are both sharper than the corpus's own variation **and** more than 5%
apart: across 439's twelve one-pass sessions, over six toggle states, request
counts run 14–39 while spans run 122–153s. A walk is a script with sleeps in it;
the counts are the app's answer. That does not let the value be derived, but it
lets a wrong one be caught, which is the part worth having.

The 5% is a magnitude and `_MIN_SEPARATION` says so rather than dressing it up.
The sharpness term alone is scale-free and correct wherever the corpus has
variation to derive a scale from — but a scripted walk with fixed sleeps often has
none. Twelve `three-round-v2` sessions walked on 440 read 271, 271, 271 and 273
nine times, both sides of that split are zero seconds wide, and on the derived
term alone a two-second difference across a 271-second walk refused the entire
corpus. No function of the shape can fix that: `{271 x 3, 273 x 9}` and
`{271 x 3, 543 x 9}` are identical in ranks, counts and group ranges. What keeps
the number honest is that both ends are pinned as tests — 0.74% must pass, 33%
must fail — though **both are now constructions**: that 440 corpus was withdrawn
the same day for a navigation fault, so the spans are carried as named synthetics
with their provenance and only the precision side still rests on evidence.

What it says about the **439 captures** is worth recording here, because one of it
disagrees with what a human took from the same numbers by hand. Read the caveat
below it first: the request counts are in the committed store and checkable, the
block counts are not in any committed file and come from re-reading
`work/observations/`, which is gitignored:

* `/clips/discover` and `/clips/discover/stream/` **erased** by `disable_reels`,
  from counts alone, with the arm reporting zero blocks — which is what an
  erasure upstream of the guard has to look like.
* `/feed/timeline/` **blocked** by `disable_feed` and `/feed/reels_tray/`
  **blocked** by `disable_stories`, from the block count matching the path's own
  request count exactly in both sessions of each arm.
* Ten watched paths **never requested**, `delivery/background_prefetch` among
  them.
* `disable_adds` governs nothing observable.
* `/discover/topical_explore` is **unclassifiable**, not blocked by
  `disable_explore`. `439-isolate-explore` reported one block;
  `439-reverse-explore` ran the same state, asked for the path six times, and
  reported **no block at all**. The block signal does not replicate across the two
  running orders, so the arm answers nothing — see the note below about these
  events being Instagram's to emit.

Those verdicts require the sessions to carry block counts. The twelve rows first
committed on 2026-08-10 predated the counter and were re-derived from
`manifest/captures/` on the same day, which changed exactly one field.

**They now predate the walk, and `grouping report` refuses all 24 by name.** That
is deliberate: unlike the toggle state and the block count, the walk is not in a
capture, so nothing can re-derive it and nobody but the person who ran them may
supply it. The repair is not a re-walk — the captures are committed, and
`observation record --capture manifest/captures/<session_id>.log --walk <name>`
reproduces every count, block and toggle identically while adding the walk and the
measured span. Until that happens the verdicts above stand as a record of what was
derived, not as something a clone can reproduce today.

**`IgFunctionalErrorEvent` is weaker than this document said.** The section above
calls it "good at attributing a block to a feature category", validated on two
endpoints. The 439 explore arm shows the prior question is not settled either: the
event can be **absent** for a block that certainly happened. Six requests to
`/discover/topical_explore` with `disable_explore` on produced zero headers in one
session and one header in the other. A zero in this signal is not evidence that
nothing was refused, and every count taken from it needs its own replication.

**`delivery/background_prefetch` is still a recorded `block`, and nothing can now
remove it.** It is in `observe_watch` precisely so its absence becomes citable
evidence rather than a reading of one call site — and with the reversal gate gone
there is no mechanism at all for retiring a ruling. That is a real open end, not
an oversight: the new approach avoids *creating* such rulings, and says nothing
about the six that already exist. Note also that `guards.py`'s own docstring
still justifies the any-of (multiple toggles per rule) form by pointing at this
endpoint. No rule in the manifest uses that form today; it is exercised only by a
test.

**Rule validation is thin at the edges.** `Rule.__post_init__` checks that a
toggle name starts with `disable_` and nothing checks it against the five keys
`throwIfBlocked` actually reads, so a mistyped sixth key would render, assemble,
install, and read `true` by default — blocking unconditionally under a preference
no UI can turn off.

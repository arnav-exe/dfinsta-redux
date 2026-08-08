# Exploration first: decide what to block after watching the phone, not before

DFInsta blocks by failing outgoing requests. One generated method,
`throwIfBlocked` in `dfinsta_source_439/newCode/com/dfinstagram/hooks.smali`,
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
version, build SHA-256, timestamp, session id, the surface walked, and the full
watch list. `never_observed()` returns the literals that were watched in at least
one non-vacuous session and never once seen.

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

That second property has a consequence the module docstring of `observation.py`
currently gets wrong. It says observe mode "blocks nothing". It does not — it
blocks precisely what the shipped build blocks. **An exploration session with the
blocks off is produced by turning the five toggles off in the app, not by
building a different APK.** And the toggles default to *on*:
`getBoolTrueEz` is `getSharedPreferences("com.instagram", 0).getBoolean(key,
true)`, so a fresh install of an observing build blocks everything until somebody
switches all five off and restarts the process. Setting changes are not effective
until a process restart; Instagram's own caches keep serving the previous state.

## The protocol

**1. Find candidates as before.** The string scan stays. It is a cheap net and
its false positives are now caught downstream rather than at a gate.

**2. Build one observing APK per version.** One build serves the whole
exploration: the watch list is `watched_literals(rules, watch_from_manifest(...))`,
which is every literal already blocked *plus* every unruled candidate in
`observe_watch`. Both halves matter. A blocked literal that is never once asked
for is the evidence that a recorded decision should be revisited, and that
cannot be produced by watching only the undecided. `observation.blocked_and_never_observed`
is the query; on the committed 441 corpus it currently names seven.

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
were on, and the schema has nowhere to put it.

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

## Two instruments, and what each is good for

**The observe line counts what the app asked for.** It is emitted by our code, in
the request path, before anything can interfere with it, and it is the only
signal in this system that cannot rot into a false pass.

**`IgFunctionalErrorEvent` counts what Instagram chose to report.** These are
Instagram's own error events, emitted at its discretion, and `probes.py` exists
largely because counting them correctly is harder than it looks: a payload has a
message body, an indented stack, and indented `field = value` entries, and a
field value that spans lines logs its continuations *un-indented* under the same
tag. Raw grep over-counts by roughly two, and the app flushes some of that
narration at a later cold start, so one phase inherits hits belonging to the
previous one. Block counts are not request counts. Use the observe line for "was
this requested"; use the error event only for the thing it is genuinely good at,
which is attributing a block to a feature category — the line immediately above
the exception names it (`FEED_NOT_LOADING`, `STORY_NOT_LOADING`), which was
validated against a positive control on two endpoints and two only.

**UI judgement is a third instrument and the least reliable of the three.** A
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

**The session record cannot say which toggles were set.** `ObservationSession`
carries version, build hash, time, id, surface, watch list and counts, and
nothing else. The entire protocol above turns on the difference between an
all-off session and a one-toggle-on session, and the committed store cannot
express it. Worse, `never_observed` unions across every non-vacuous session for a
version, so an all-off exploration session and five isolation sessions filed in
one `441.jsonl` collapse into a single blended answer with no way to separate
them. This needs a field, and the field needs to be part of what makes a session
readable — a zero whose configuration is unknown is not a measurement.

**Step 5 cannot be done with toggles alone.** "Block exactly one literal"
requires that literal to be the only one under its toggle, and `disable_reels`
now covers five. Isolating one means either a throwaway build in which the
literal is the sole rule under its toggle, or a diagnostic build
(`render_method(..., diagnostic=True)`, which gives every rule its own throw
message and must never ship). Either way step 5 costs one build per candidate
group. That is the real argument for step 3: the all-off session is what stops
you paying for builds on candidates that were never going to fire.

**Nothing derives grouping from measurement yet.** Step 6 describes what a human
should conclude; there is no code that takes a set of isolation sessions and
proposes "these three kill the same surface". Until there is, the toggle mapping
is still a judgement — a much better informed one, but a judgement.

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

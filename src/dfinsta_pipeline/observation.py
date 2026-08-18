"""What the app was actually asked for. Endpoint evidence measured, not guessed.

    python -m dfinsta_pipeline.observation record --version 441 \
        --build-sha256 <64 hex> --recorded-at 2026-08-09T10:00:00Z \
        --session-id 441-feed-1 --surface feed_tab --walk three-round-v2 \
        --watched-from watched.txt --capture logcat.txt
    python -m dfinsta_pipeline.observation report --version 441 [--json]

Stage 4 finds endpoint *strings in a class* and asks a human to judge from a
name. On 2026-08-08 that produced two rulings it should not have: one endpoint
that fires zero times, and one — `delivery/background_prefetch` — that is not an
endpoint at all but a no-op logger's marker name. Both looked exactly like the
four good rulings beside them, because a name in a class of names is all the
evidence stage 4 has.

So the app grows an **observe mode**: a generated form of `throwIfBlocked` that
emits one line per watched path it sees, *before* any rule can throw. It blocks
exactly what a shipped build blocks — `test_an_observing_build_blocks_exactly_what_a_shipped_one_blocks`
**executes both renderings** against every watched path under every toggle state
and compares the decisions, because the two no longer have the same instructions
and comparing their text would assert only that nobody changed them — and that is the whole reason the
section below exists, because a build that still blocks suppresses the very
requests it is counting. This module is the host side: it turns those lines into
committed evidence, and that evidence into an answer to "which of these paths
does this phone never actually request?".

===============================================================================
  THE CONTRACT WITH THE APP
===============================================================================

One line per observed request, through `android.util.Log.i`::

    I DFInstaObserve: /feed/timeline_stream/

and in a real capture, with the threadtime prefix logcat adds::

    08-08 17:31:02.412 12875 12875 I DFInstaObserve: /feed/timeline/

plus a **directive** naming which blocks were active, emitted on *every* checked
request, ahead of any path line that request produces::

    I DFInstaObserve: !toggles +blocked disable_feed=1 disable_explore=0 …
    I DFInstaObserve: /feed/timeline/

`1` is on, meaning blocking. A payload beginning `!` is a directive and never a
path; an unrecognised one **refuses**, so a host reading a capture from a newer
build fails loudly instead of counting `!version 442` as a request. Repeats are
collapsed — a 22-request session states the same thing 22 times.

The `+blocked` token is the build stating **what its instrumentation can report**.
Tokens marked with `+` are capabilities and the rest are toggles, and the two
shapes cannot collide because a preference key can never begin with `+`. It rides
on this line rather than one of its own because this line is written on every
checked request — 625 times in a three-round session — so a second one would have
grown every committed capture by half to repeat one constant.

It repeats because the once-per-process version of it failed in the field, and
failed silently. The protocol is `adb logcat -c` immediately before walking the
app, Instagram's process is usually already alive, so the single line had been
written into the buffer that was then cleared and the flag stayed set: 22 path
lines and no statement of what was active, with nothing marking the omission.
Restating it per request buys the invariant **any capture holding a path line
also holds the toggle state**, and buys a second thing the flag could not — a
toggle changed halfway through a session now contradicts itself in the file
instead of being invisible.

The message is otherwise **verbatim** one of the watched literals, and nothing
else is emitted under that tag. :func:`parse` therefore anchors on the tag
*position* rather than searching for the string: a crash dump quoting one of
these lines inside its own payload is another component talking about DFInsta,
not DFInsta seeing a request, and `probes.count_signal` already paid for that
lesson once — re-narration counted as events and turned an off-side zero into a
phantom leak.

Anything under that tag which is not verbatim a watched literal makes the whole
session refuse — in `ObservationSession`, which is the only place that knows the
watch list; `parse` counts what it is given. It means the build and the `watched`
list disagree about what was being watched, and a session whose watch list is
wrong cannot support a statement about what was *not* seen.

===============================================================================
  AND ONE LINE THE GUARD EMITS, WHICH THE COUNTS CANNOT REPLACE
===============================================================================

The observe line says a request was **made**. It does not say it was **stopped**,
and the two come apart in both directions:

* A path block does not lower the count. The request is made, the observe pass
  logs it, and only then does `throwIfBlocked` throw. `/feed/timeline/` under
  `disable_feed` *rises* 6 → 20 because the app retries; `/feed/reels_tray/`
  under `disable_stories` goes 2 → 3, which is inside the spread two runs of one
  state produce anyway. **A block is not reliably visible in a request count.**
* An upstream erasure does not produce a block. `replaceReelsEndpoint` blanks the
  literal before the URL is built, so the path never reaches the guard, never
  throws, and never appears in the log at all — `/clips/discover` 4 → 0.

So the guard says so itself, immediately before it throws, naming the literal
that matched::

    I DFInstaObserve: !blocked /feed/timeline/

That is a decision **we** made, recorded by **us**, through the same
`android.util.Log.i` that has never dropped a line here. "Did this rule fire, and
how often" is then known rather than derived.

**It replaces a signal that was never ours.** Until 2026-08-13 the only block
evidence was the line Instagram emits when it catches our exception::

    E IgFunctionalErrorEvent: FEED_NOT_LOADING
    E IgFunctionalErrorEvent: java.io.IOException: Blocked by DFInsta setting
    E IgFunctionalErrorEvent: 	at com.dfinstagram.hooks.throwIfBlocked(...)
    ...
    E IgFunctionalErrorEvent: 	 NETWORK_FAILURE_REASON = Blocked by DFInsta setting

**Only the header counts.** The last line is the same event narrating itself into
a field of its own payload, and counting it doubles every total; a third spelling,
`FAILURE_REASON = ...`, is in this corpus too, so a denylist of field names would
have missed one. The rule is therefore positive: the payload must be *exactly*
`java.io.IOException: Blocked by DFInsta setting`, at level `E`, with
`IgFunctionalErrorEvent` in **tag position**. That also excludes the app's own
`aware_trace` narration, which quotes `fault_message: Blocked by DFInsta setting`
inside a JSON blob logged un-indented under the same tag — the re-narration
failure `probes.count_signal` documents, arriving here from a third direction.

The line **immediately above** the header names the failing feature
(`FEED_NOT_LOADING`, `STORY_NOT_LOADING`, `EXPLORE_NOT_LOADING`), so
:class:`BlockCount` carries the breakdown as well as the total. It is
corroboration and never a basis: it names a *feature*, not a path, and the
mapping from one to the other has been measured three times.

**These are Instagram's events, emitted at Instagram's discretion, and they go
missing.** In this corpus `439-reverse-explore` ran with `disable_explore` on,
asked for `/discover/topical_explore` six times, and reported **no block at all**,
while `439-isolate-explore` — same state, same walk, other order — reported one.
Across eight sessions on two versions and two walks the same path was refused 7,
6, 12 and 6 times and reported 1, 0, 1 and 0, while `/feed/timeline/` reported
20/20, 23/23, 17/17 and 16/16 in the very same captures. The loss is
feature-specific and stable, so no inference over the total recovers it — and a
whole layer of inference was built to try, an accounting identity and a
subset-sum ambiguity check, which is now deleted.

:class:`BlockCount` stays because 48 committed sessions carry it and because a
capture that was read was read. **Nothing derives from it.** Every question about
which path was refused is answered by :class:`Refusals`, and a build that could
not report those says so by omitting `+blocked` — so its silence stays a silence
instead of becoming 48 measured zeroes.

===============================================================================
  A ZERO IS ONLY READABLE UNDER A STATED CONFIGURATION
===============================================================================

The blocks suppress requests **downstream of themselves**, so a session measured
with them on can produce a zero that is a fact about our own configuration.
Measured on 2026-08-08, same build and same walk, only the five toggles changed:

    /feed/injected_reels_media/   0 with the blocks on   3 with them off
    /feed/reels_media_stream/     0                      1
    /clips/discover/stream/       0                      3

Blocking `/feed/timeline/` leaves no timeline response for Reels to be injected
into, so the child request is never made. And `replaceReelsEndpoint` blanks the
endpoint string before the URL is built — which is also *before* the observe
pass — so with `disable_reels` on those paths report zero for a reason that has
nothing to do with traffic. Three zeros, none of them about Instagram.

So every session carries the toggle state it was measured under, and:

**The state is read from the device, never typed by the operator.** There is no
`--toggles` flag, and adding one would be the same shape of mistake as the rule
this project shipped and broke in one line the next day: `effective_from` derived
from a `--version` the same person supplied in the same command, a safety
property that was really a formality. `retirement`'s docstring states the lesson —
*ask what the operator controls*. Here the operator controls the phone's settings
and the capture; the build controls what it says about itself. A selector may
*choose* among recorded states, because choosing wrong refuses rather than
answering.

Precisely what that buys, and no more: **the recorded state is a function of the
capture alone.** Somebody who wants a row to say something else has to put the
line into the capture, or construct the record by hand — forging the evidence
rather than filling in a field. That is a different act from typing a flag: it is
visible in the capture that gets kept beside the session, and visible in a diff.
An adversarial pass is right that it is not impossible; it is *the thing you
would have to do*, which is the most any host-side rule can be worth.

**A capture that cannot state its toggle state is a refusal, not an "all off".**
A path line ahead of any directive is a capture whose start was cut off, and its
counts cannot be attributed to any configuration. Two directives that disagree
are two configurations in one file — a toggle changed mid-session, or two
captures concatenated — with no line saying which counts belong to which, and
they refuse too.

A capture with no tag lines at all is the ordinary vacuous capture: no directive
because the observe pass never ran. It records honestly, with an unknown state,
and is excluded from every answer by the vacuity rule below rather than by this
one. The directive proves the build was observing; it does **not** prove the app
was walked, so a stated session that saw nothing is still vacuous.

===============================================================================
  WHY THE SESSIONS ARE NOT BLENDED
===============================================================================

:func:`never_observed` takes the toggle state as a **required argument** and
answers over the sessions measured under exactly that state. The all-off
exploration session and the one-toggle-on isolation sessions of the protocol land
in one `<version>.jsonl` and answer different questions; unioning them produces a
number that is about no configuration at all.

A required argument rather than "group, and refuse when mixed": a call that
answers today and refuses tomorrow because somebody filed a second session is
indistinguishable, from the caller's side, from a corpus that broke. Naming the
state makes the question well-posed at the call site and keeps it well-posed for
ever. :func:`states` says which states are on record, and the report answers
each state separately so the reader never has to pick.

**A session whose toggle state is unknown answers nothing.** It is not "probably
all off" and not "probably as shipped" — it is a measurement whose experiment was
not written down. `manifest/observations/441.jsonl` holds exactly one such row,
recorded on 2026-08-08 before the build reported its own state. The design note
written the same week says it was walked with the blocks on, which would make it
the circular measurement above; that note is a recollection and not a
measurement, and treating it as one is the back-fill this module refuses — which
is why the row answers nothing rather than answering as "blocks on". It stays
readable, though: deleting or back-filling a row in
an append-only store would be inventing a measurement from memory, which is the
operator-supplied state this design refuses — and it is excluded from every
toggle-scoped answer, by name, loudly, in both report forms.

===============================================================================
  AND A WALK, WHICH THE OPERATOR TYPES. SAYING SO RATHER THAN PRETENDING
===============================================================================

A count is produced by two things: the app, and the driving. The section above
pins the first — same build, stated configuration — and until now nothing pinned
the second. On 2026-08-11 the walk went from one pass over three surfaces to
**three rounds** over them, and the 440 baseline went from 11–16 observed
requests to 25. Two sessions of one state, one walked each way, would spread by
14 for a reason no toggle caused; `grouping` derives its noise floor from exactly
that spread and would have read the whole difference as noise, and then swallowed
every real effect underneath it. Nothing in a row said which walk produced it.

So a session names its **walk**: a short, stable identifier for the *protocol* —
`one-pass-three-surfaces`, `three-round-v2` — not for what came out of it. It is
the same reasoning as the toggle state one section up. A session that cannot
state the conditions it was measured under is not evidence for a question that
depends on them, and a differential between two states depends on both of them.

**And here the operator types it, which the toggle state deliberately does not
allow.** That is not an oversight and it is not a guarantee wearing a different
name. The toggle state is a property of the phone, so the build could be made to
report it and the host could refuse anything else. **The walk is a property of
the driving script.** It is not on the device, the app never learns it, and no
line of a capture names it. There is nowhere else for it to come from, so
`record` grows a required `--walk`, and this docstring says outright that its
value is worth exactly what the person who typed it is worth. Do not read the
field as the safety property `toggles` is; `retirement`'s `effective_from` was
once derived from a `--version` the same person supplied in the same command, and
the lesson recorded from breaking it is *ask what the operator controls* — here
the answer is genuinely "this", and the honest response is to label it, not to
dress it up.

**What is not typed is the evidence beside it.** A capture carries logcat's own
timestamps, so :func:`parse` measures the **span** — the time from the first line
it read to the last — and stores it on the session. The span is a fact about the
driving in a way no request count is: across the twelve sessions committed for
439, walked under six different toggle states, the observed request counts run 14
to 39 while the spans run 122s to 153s and ten of the twelve sit inside 9s of one
another. The walk is a script with sleeps in it; the counts are the app's answer.
A three-round walk takes about three times as long, and no toggle makes a
two-minute walk take six.

That does not let the value be derived — a span names no protocol — but it lets a
**wrong** one be caught, which is the part worth having. :func:`walk_dispute`
asks whether the sessions claiming one walk split into two groups whose spans are
further apart than either group is wide. There is no constant in it: the corpus
supplies its own scale, the way `grouping`'s noise floor does, and a claim only
fails when the evidence separates more sharply than it varies. Both committed
corpora pass it, the mixed corpus the paragraph above describes fails it, and
`grouping` **refuses** on it rather than deriving a floor from two protocols.

Its own limits, stated because a check whose reach is not written down gets
believed past it: it needs two sessions on each side, so a single mislabelled
session among a dozen is invisible to it; it reads timestamps, so a capture
carrying none has no span and contributes nothing; and it compares, so it says
nothing at all about a corpus in which every session is mislabelled the same way.
That last one is the residual, and the field is what carries it: `--walk` is a
statement, and a statement can be wrong. Stripping the timestamps out of a
capture would defeat the check — and that is forging the evidence rather than
filling in a field, which is the same line the toggle state draws and the most
any host-side rule is worth.

**`grouping` partitions by walk; :func:`never_observed` does not, and the
asymmetry is the point.** A grouping is a *differential*: it subtracts one state
from another, and mixing protocols there manufactures a difference nothing
caused. `never_observed` makes a *negative* claim — "watched, walked for, never
once requested" — and pooling a second walk into it can only give a path more
chances to be seen. It can retract such a claim and can never invent one, so
pooling there is conservative rather than circular. What it does need is for the
reader to be told, because "would this session have seen it?" is the question the
surface list already exists for and the walk bounds the same answer: every report
names the walks each state was measured on.

===============================================================================
  WHY A SESSION THAT SAW NOTHING IS NOT EVIDENCE
===============================================================================

The claim this module exists to support is a **negative** one: "the app was
watching this path and never once asked for it". Negative claims fail in one
characteristic way here — the measurement silently did not happen, and its
silence reads as the finding. `absence-assertions-need-positive-controls` is the
same lesson from the other end; so is the differential that compared 2 of 7 hooks
and reported it as a comparison.

A session in which **nothing at all** was observed is exactly that failure. It is
equally well explained by:

* the installed build not being the observing one,
* the capture being empty, taken from the wrong device, or cleared too late,
* the app never having run,
* or every watched path genuinely going unrequested.

Only the last is a finding, and nothing in the capture distinguishes it from the
other three. So a session counts as evidence **only if it observed at least one
literal** — the session's own output is its positive control, and the app's own
emission is the one signal that cannot rot into a false pass.

That threshold is *derived*, not chosen: it is `total > 0`, where `total` is the
session's own count. There is deliberately no "at least N sessions" constant.
A number like that would be a judgement about sufficiency dressed as a rule, and
`derive-the-threshold-never-declare-it` is the standing objection — `4` → `3` is
one character and looks like maintenance.

And when *every* session is vacuous, :func:`never_observed` **refuses**. It does
not return `()`. An empty tuple is the same answer it gives when every watched
path was seen, so returning it would report "we measured nothing" in the words of
"nothing is wrong" — the absence-as-a-pass this project refuses everywhere.
`rulings.unenforced_endpoints` refuses in the same place for the same reason.

===============================================================================
  WHAT THIS EVIDENCE CANNOT SAY
===============================================================================

**Never observed is bounded by the surfaces that were walked.** A path the Reels
player requests is not observed by a session that stayed on the feed, and that
silence is about the session, not about the app. `surface` is recorded per
session and every report repeats the list, because the reader's first question
has to be "would this session have seen it if it happened?".

It is also bounded by the account and by server-side configuration — a
MobileConfig flag picking the other implementation is how a statically perfect
430 settings hook came to be dead at runtime. This module records what a phone
did. It does not decide anything, and nothing here changes a block.

===============================================================================
  AND THE ONE QUESTION IT ANSWERS ABOUT BLOCKS
===============================================================================

:func:`blocked_and_never_observed` intersects the manifest's own blocked
literals with :func:`never_observed`. It is the surviving half of a deleted
module: `reconsider` asked the same question in order to *propose withdrawing*
a block, through a reversal gate that in its whole life recorded none. That
whole layer went on 2026-08-08, because the project stopped deciding early on a
name in a class and correcting afterwards, and started exploring on the phone
first. The question outlived the machinery — "we block this and the app has
never once asked for it" is exactly what a decision made on measurement wants to
know — so it lives here, beside the measurement, and answers rather than
proposes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .contracts import SHA256_PATTERN
from .history import _NUMERIC

__all__ = [
    "ObservationError",
    "SCHEMA_VERSION",
    "TAG",
    "TOGGLE_DIRECTIVE",
    "BLOCKED_DIRECTIVE",
    "REPORTS_MARK",
    "REPORTS_BLOCKED",
    "KNOWN_REPORTS",
    "Refusals",
    "BLOCK_TAG",
    "BLOCK_MESSAGE",
    "UNATTRIBUTED",
    "UNWALKED",
    "OBSERVATIONS",
    "ToggleState",
    "BlockCount",
    "Capture",
    "ObservationSession",
    "parse",
    "store_path",
    "append",
    "read",
    "evidential",
    "stated",
    "walked",
    "states",
    "walks",
    "walk_dispute",
    "walk_evidence",
    "never_observed",
    "blocked_endpoints",
    "blocked_and_never_observed",
    "summary",
    "render",
    "main",
]


class ObservationError(RuntimeError):
    """Raised when an observation cannot honestly be read or recorded."""


SCHEMA_VERSION = 1

#: The app's observe-mode log tag. Fixed by the contract above; the app side
#: emits under this and nothing else does.
TAG = "DFInstaObserve"

#: Per version, tracked, beside `manifest/runtime_evidence`. Committed for the
#: same reason that one is: evidence that must survive *between* ports is the
#: evidence a gitignored directory loses, and `evidence-in-scratch-is-not-evidence`
#: cost 441 four of its seven hooks.
OBSERVATIONS = Path("manifest") / "observations"

#: `[<stamp> <pid> <tid>] <LEVEL> DFInstaObserve: <literal>`.
#:
#: The optional prefix is what logcat's `threadtime` format prepends; without it
#: this still reads the bare form the contract states and a hand-made fixture.
#: The tag is anchored in **tag position** — immediately after the level — so a
#: line that merely contains `DFInstaObserve:` inside another tag's message body
#: does not match. That is the re-narration failure `probes.count_signal`
#: documents, and here it would manufacture requests that never happened.
_OBSERVE_LINE = re.compile(
    r"^\s*(?:\S+\s+\S+\s+\d+\s+\d+\s+)?[VDIWEFS]\s+" + re.escape(TAG) + r":\s?(?P<literal>.*)$"
)

#: The one directive the app emits, on every checked request, ahead of any path
#: line that request produces. A payload starting `!` is never a path — no
#: watched literal can begin with one, because `throwIfBlocked` tests
#: `URI.getPath()`.
TOGGLE_DIRECTIVE = "!toggles"

#: How an observing build states a refusal **it made itself**, naming the literal
#: that matched: `!blocked /feed/timeline/`.
#:
#: This exists because :data:`BLOCK_MESSAGE` below is *Instagram's* line. It is in
#: the log only because Instagram catches our IOException and files it into its own
#: error event, and it under-reports by feature: across eight sessions on two
#: Instagram versions and two walk protocols, `/discover/topical_explore` was
#: refused seven times and reported once, and six times and reported **none**,
#: while `/feed/timeline/` reported 20/20 and 23/23 in the very same captures.
#: Whether the app requests a path is Instagram's to say and is the thing being
#: measured; whether our guard refused it is ours, and so is writing that down.
BLOCKED_DIRECTIVE = "!blocked"

#: What a build says its instrumentation can report, carried on the toggle line as
#: `!toggles +blocked disable_feed=1 ...`.
#:
#: Without it a capture holding no `!blocked` line is ambiguous between "nothing
#: was refused" and "this build could not have written one", and every session
#: recorded before 2026-08-13 is the second — so a reader that could not tell them
#: apart would turn 48 committed sessions into 48 measured zeroes at once. That is
#: the absent-versus-empty conflation this store spells apart everywhere else.
#:
#: The mark cannot collide with a preference key: :data:`_TOGGLE_NAME` constrains
#: those to `[A-Za-z_][A-Za-z0-9_]*`, so splitting the line on token shape is exact
#: rather than a convention two modules have to remember.
REPORTS_MARK = "+"

#: The one capability a build can currently claim. Named rather than numbered, so
#: a reader needs no table mapping build generations to what they could say.
REPORTS_BLOCKED = "blocked"
KNOWN_REPORTS = frozenset({REPORTS_BLOCKED})

#: Instagram's own error-event tag. Not ours: these events are emitted at
#: Instagram's discretion and can go missing entirely — see the module docstring.
#: The tag the generated probe class logs under, one line per hook whose patched
#: site executed. A DIFFERENT tag from `TAG` on purpose: an observation is a thing
#: the app did, and a probe is a thing OUR code did, and the two must not be
#: countable as each other.
PROBE_TAG = "DFInstaProbe"

#: `[<stamp> <pid> <tid>] <LEVEL> DFInstaProbe: <hook_id>`, matching `_OBSERVE_LINE`
#: in shape so a line merely mentioning the tag inside another message body cannot
#: be read as a probe.
_PROBE_LINE = re.compile(
    r"^\s*(?:\S+\s+\S+\s+\d+\s+\d+\s+)?[VDIWEFS]\s+"
    + re.escape(PROBE_TAG)
    + r":\s?(?P<hook>.*)$"
)

#: The tag the WALK logs under to say which surface it has just moved to. A third
#: tag, distinct from both others, because it is neither something the app did nor
#: something our patched code did — it is the harness annotating its own actions.
WALK_TAG = "DFInstaWalk"

#: `[<stamp> <pid> <tid>] <LEVEL> DFInstaWalk: surface=<name>`.
_WALK_LINE = re.compile(
    r"^\s*(?:\S+\s+\S+\s+\d+\s+\d+\s+)?[VDIWEFS]\s+"
    + re.escape(WALK_TAG)
    + r":\s?surface=(?P<surface>.*)$"
)

#: Where a request that arrived before the walk touched any tab is counted. App
#: startup fires requests before the first tap, and they are not attributable to
#: a surface — calling them Home, because Home is where the app opens, would be
#: inventing an attribution. Measured on 442: `/feed/reels_tray/` appeared ONLY
#: here.
STARTUP = "(startup)"

BLOCK_TAG = "IgFunctionalErrorEvent"

#: The exception `throwIfBlocked` raises, as the header line spells it. Matched
#: **whole**, so the same string quoted inside a field value or a narration blob
#: is not an event. `guards` renders the message; a build that changes it makes
#: every capture read zero here, which is why the constant is stated once.
BLOCK_MESSAGE = "java.io.IOException: Blocked by DFInsta setting"

#: `[<stamp> <pid> <tid>] E IgFunctionalErrorEvent: java.io.IOException: ...`
#:
#: Anchored the same way `_OBSERVE_LINE` is — tag position, and here the payload
#: is pinned end to end rather than searched for. The indented
#: `NETWORK_FAILURE_REASON = Blocked by DFInsta setting` echo, its `FAILURE_REASON`
#: spelling, the `\tat com.dfinstagram.hooks.throwIfBlocked(...)` frame and the
#: `aware_trace` JSON that quotes the message are all the same event describing
#: itself; each of them would inflate the count, and none of them matches this.
_BLOCK_HEADER = re.compile(
    r"^\s*(?:\S+\s+\S+\s+\d+\s+\d+\s+)?E\s+"
    + re.escape(BLOCK_TAG)
    + r":\s?"
    + re.escape(BLOCK_MESSAGE)
    + r"\s*$"
)

#: The line immediately above a header, when it is a bare feature category.
#: Deliberately narrow — a bare SHOUTING_TOKEN and nothing else — because the
#: line above may equally be a stack frame, an indented field, or an unrelated
#: tag, and guessing which is which is how a payload continuation becomes a
#: feature name.
_FEATURE_LINE = re.compile(
    r"^\s*(?:\S+\s+\S+\s+\d+\s+\d+\s+)?E\s+"
    + re.escape(BLOCK_TAG)
    + r":\s?(?P<feature>[A-Z][A-Z0-9_]*)\s*$"
)

#: The feature key for a block whose preceding line names no category. Spelled
#: with parentheses so it can never collide with a real one, which `_FEATURE_LINE`
#: constrains to `[A-Z][A-Z0-9_]*`. Present rather than dropped: the per-feature
#: breakdown must sum to the total, or a reader cannot tell an unattributed block
#: from one nobody counted.
UNATTRIBUTED = "(no feature line)"

#: How a report spells a session that named no walk. Parenthesised so it can never
#: collide with a real one — `_WALK` forbids the bracket — which is the trick
#: `UNATTRIBUTED` already uses two constants below. It is a *label*, deliberately
#: not a selector: nothing accepts it as an answer to "which walk?", because a
#: comparison over sessions whose protocol nobody stated is the thing this field
#: exists to stop.
UNWALKED = "(no walk stated)"

#: The shape of a walk name. Stricter than `surface`, which is free text, and the
#: difference is what the two are for: `surface` is prose a human reads, while
#: `walk` is a **join key** — `grouping` partitions on it, so `Three-Round` and
#: `three-round` would be one protocol wearing two names and would halve every
#: group silently. That is the argument `ToggleState` already makes for sorting
#: its pairs. Refused rather than lowercased: a value the store rewrites is a
#: value the operator's notes and the file disagree about.
_WALK = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")

#: `MM-DD HH:MM:SS.mmm`, logcat's `threadtime` stamp, in the position the two line
#: patterns below allow it. Read for one purpose only — the span between the first
#: and last line `parse` took — and absent from the bare contract form, where it
#: simply means no span was measurable.
_STAMP = re.compile(r"^\s*(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})\.(\d+)\b")

#: A preference key the guard reads. Shape only, deliberately: `guards.Rule`
#: already decides which names are legitimate for a *rule*, and this module
#: records what the device said rather than judging it. A build that renames its
#: toggles must still be able to file an honest capture.
_TOGGLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class ToggleState:
    """Which blocks were active while a capture was taken. Read from the device.

    Stored sorted by name, so two states are equal exactly when they say the same
    thing however the build ordered them — the app emits them in the order the
    guard reads them, which is rule order and moves when a rule moves. A state
    that compared unequal to itself across a rule reordering would split one
    experiment into two groups and answer both from half the sessions.

    Complete as the build reported it, never as a set of "the ones that were on".
    Two states naming different *keys* are different states and do not blend:
    a version that grows a sixth toggle has not measured the same experiment.
    """

    #: `(name, on)`, sorted by name. A Mapping is accepted here and normalised.
    pairs: tuple[tuple[str, bool], ...]

    def __post_init__(self) -> None:
        given = self.pairs
        items = list(given.items()) if isinstance(given, Mapping) else list(given)
        cleaned: list[tuple[str, bool]] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, Sequence) or isinstance(item, (str, bytes)) or len(item) != 2:
                raise ObservationError(
                    f"a toggle state is (name, on) pairs, got {item!r}"
                )
            name, value = item
            name = str(name)
            if not _TOGGLE_NAME.fullmatch(name):
                raise ObservationError(f"{name!r} is not a preference key")
            if not isinstance(value, bool):
                # `1 == True` in Python, so an int would compare equal here and
                # round-trip through JSON as `1` — one state with two spellings
                # in a store whose whole job is telling two states apart.
                raise ObservationError(
                    f"toggle {name} is {value!r}; a toggle state is on or off, and the "
                    "store writes true/false"
                )
            if name in seen:
                raise ObservationError(
                    f"a toggle state names {name} twice. One key cannot have been both "
                    "on and off for one capture"
                )
            seen.add(name)
            cleaned.append((name, value))
        if not cleaned:
            raise ObservationError(
                "a toggle state that names no toggle states nothing. The build reports "
                "every key it reads, so an empty one is a build that did not answer"
            )
        object.__setattr__(self, "pairs", tuple(sorted(cleaned)))

    @classmethod
    def of(cls, values: Mapping[str, bool] | Iterable[tuple[str, bool]]) -> "ToggleState":
        return cls(tuple(values.items()) if isinstance(values, Mapping) else tuple(values))

    @classmethod
    def parse(cls, text: str) -> "ToggleState":
        """`disable_feed=1 disable_explore=0` — the app's spelling, and a selector's.

        The same reader for both directions, so a state cannot be recorded in a
        form no caller can name back.
        """

        pairs: list[tuple[str, bool]] = []
        for token in str(text).split():
            name, separator, value = token.partition("=")
            if not separator or value not in ("0", "1"):
                raise ObservationError(
                    f"{token!r} is not `key=0` or `key=1`. A toggle state is read "
                    "verbatim from what the build reported, and a token nobody can read "
                    "is a build and a host that disagree about the contract"
                )
            pairs.append((name, value == "1"))
        return cls(tuple(pairs))

    @property
    def text(self) -> str:
        """The canonical spelling. Equal states have equal text, and conversely."""

        return " ".join(f"{name}={int(value)}" for name, value in self.pairs)

    @property
    def on(self) -> tuple[str, ...]:
        return tuple(name for name, value in self.pairs if value)

    @property
    def off(self) -> tuple[str, ...]:
        return tuple(name for name, value in self.pairs if not value)

    @property
    def blocking(self) -> bool:
        """Was anything blocking? The condition under which a zero can be ours."""

        return bool(self.on)

    def as_dict(self) -> dict[str, bool]:
        return dict(self.pairs)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.text


@dataclass(frozen=True)
class BlockCount:
    """How many requests the guard refused in one capture, and under which feature.

    A **measured** number, so `BlockCount(0)` and "nobody counted" must never be
    the same value: the first is the baseline evidence that a state blocks nothing,
    the second is a row written before this host counted at all. Absence is spelled
    by `ObservationSession.blocks` being `None`, and by the key being missing from
    the stored row — the one spelling `toggles` already uses.

    The breakdown must **sum to the total**, with `UNATTRIBUTED` carrying the
    headers whose preceding line named no category. A breakdown that is allowed to
    be partial is a breakdown a hand-edit can quietly shrink, and the number a
    reader would then compare against zero is the one that stayed right.
    """

    total: int
    #: `(feature, count)`, sorted by feature. A Mapping is accepted and normalised.
    by_feature: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        total = self.total
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            raise ObservationError(
                f"a block count is a whole number of refused requests, got {total!r}"
            )
        given = self.by_feature
        items = list(given.items()) if isinstance(given, Mapping) else list(given)
        cleaned: list[tuple[str, int]] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, Sequence) or isinstance(item, (str, bytes)) or len(item) != 2:
                raise ObservationError(
                    f"a block breakdown is (feature, count) pairs, got {item!r}"
                )
            feature, count = item
            feature = str(feature)
            if feature != UNATTRIBUTED and not re.fullmatch(r"[A-Z][A-Z0-9_]*", feature):
                raise ObservationError(
                    f"{feature!r} is not a feature category. The line above a block "
                    f"header is a bare SHOUTING_TOKEN, or it is {UNATTRIBUTED!r}"
                )
            if not isinstance(count, int) or isinstance(count, bool) or count < 1:
                raise ObservationError(
                    f"block feature {feature} has count {count!r}; a recorded zero is a "
                    "second spelling of absent"
                )
            if feature in seen:
                raise ObservationError(f"a block breakdown names {feature} twice")
            seen.add(feature)
            cleaned.append((feature, count))
        if sum(count for _, count in cleaned) != total:
            raise ObservationError(
                f"a block breakdown summing to {sum(count for _, count in cleaned)} "
                f"states a total of {total}. Every header is attributed to a feature or "
                f"to {UNATTRIBUTED!r}, so the two cannot disagree unless one was edited"
            )
        object.__setattr__(self, "by_feature", tuple(sorted(cleaned)))

    @classmethod
    def of(
        cls, total: int, by_feature: Mapping[str, int] | Iterable[tuple[str, int]] = ()
    ) -> "BlockCount":
        return cls(
            total,
            tuple(by_feature.items()) if isinstance(by_feature, Mapping) else tuple(by_feature),
        )

    @property
    def features(self) -> dict[str, int]:
        return dict(self.by_feature)

    @property
    def text(self) -> str:
        if not self.by_feature:
            return str(self.total)
        return f"{self.total} (" + ", ".join(
            f"{feature} {count}" for feature, count in self.by_feature
        ) + ")"

    def as_dict(self) -> dict[str, Any]:
        return {"total": self.total, "by_feature": dict(self.by_feature)}

    @classmethod
    def from_dict(cls, data: Any) -> "BlockCount":
        if not isinstance(data, Mapping):
            raise ObservationError(
                f"blocks must be an object with a total and a breakdown, got "
                f"{type(data).__name__}"
            )
        unknown = sorted(set(data) - {"total", "by_feature"})
        if unknown:
            raise ObservationError(f"blocks has unknown keys: {', '.join(unknown)}")
        if "total" not in data:
            raise ObservationError("blocks states no total")
        by_feature = data.get("by_feature", {})
        if not isinstance(by_feature, Mapping):
            raise ObservationError(
                f"by_feature must be an object of feature -> integer, got "
                f"{type(by_feature).__name__}"
            )
        return cls.of(data["total"], {str(key): value for key, value in by_feature.items()})

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.text


@dataclass(frozen=True)
class SurfaceCounts:
    """Which surface was on screen when each path was requested.

    The walk knows when it taps a tab and the app does not announce it — Instagram
    runs every tab as a fragment of one activity, so nothing in the log marks the
    change. So the walk annotates the same stream it is capturing, and every
    observation after a marker belongs to that surface until the next one.

    **This says where a request happened, not what caused it.** A path seen only
    while Reels was on screen is strong evidence it serves Reels; a path seen
    everywhere is evidence of nothing in particular. It exists to answer "which of
    the switches should own this", which is otherwise guessed from the path's
    name — and a name that reads like a feature is not evidence a feature exists.

    A surface that saw nothing is **absent**, never a recorded zero, and requests
    that arrived before the first marker are counted under :data:`STARTUP` rather
    than attributed to wherever the walk went first.
    """

    #: `(surface, ((path, count), ...))`, sorted. Mappings are accepted and
    #: normalised, so a caller can build one the obvious way.
    by_surface: tuple[tuple[str, tuple[tuple[str, int], ...]], ...] = ()

    def __post_init__(self) -> None:
        source = (
            self.by_surface.items() if isinstance(self.by_surface, Mapping) else self.by_surface
        )
        normalised = []
        for surface, counts in source:
            pairs = counts.items() if isinstance(counts, Mapping) else counts
            inner = tuple(sorted((str(path), int(count)) for path, count in pairs))
            for path, count in inner:
                if not str(path).strip():
                    raise ObservationError("a surface count carries an empty path")
                if count < 1:
                    raise ObservationError(
                        f"{surface}/{path} carries {count}; a path a surface never saw is "
                        "absent, not a recorded zero"
                    )
            if not str(surface).strip():
                raise ObservationError("a surface count carries an empty surface name")
            normalised.append((str(surface), inner))
        object.__setattr__(self, "by_surface", tuple(sorted(normalised)))
        if len({surface for surface, _ in self.by_surface}) != len(self.by_surface):
            raise ObservationError("a surface appears twice")

    def surfaces_for(self, path: str) -> tuple[tuple[str, int], ...]:
        """`(surface, count)` for one path, busiest first. The decision lookup."""

        found = [
            (surface, dict(counts)[path])
            for surface, counts in self.by_surface
            if path in dict(counts)
        ]
        return tuple(sorted(found, key=lambda pair: (-pair[1], pair[0])))

    def consent_split(self, path: str) -> tuple[int, int]:
        """`(unsolicited, solicited)` for one path: launch window vs. a surface.

        **The one measurement this project has that bears on the consent test.**
        The question a human is asked at the gate is "did a user action cause
        this content to appear", and the launch window is the only stretch of a
        walk where the answer is knowably *no*: the harness has tapped nothing
        yet, so every request between process start and the first surface marker
        arrived unbidden. Everything after a marker followed a tap.

        Deliberately weaker than it looks, and the summary that prints it has to
        say so. A request on a surface followed *a* user action; it does not
        follow that the action asked for that content, which is exactly what
        makes generic recommendations score 100% in Lukoff's bands while search
        scores 33% — both arrive after a tap. So a high startup count is evidence
        of unsolicited delivery, and a low one is **not** evidence of solicited
        delivery. The asymmetry is the finding, not a defect in it.

        A dedicated idle probe would answer the other half and was designed and
        dropped: the app goes 37 to 76 seconds without requesting a watched path
        *while being swiped every three seconds*, so a 30-second window of
        silence measures nothing. The launch window costs no extra walk and has
        no user action in it at all.
        """

        return (
            sum(count for surface, count in self.surfaces_for(path) if surface == STARTUP),
            sum(count for surface, count in self.surfaces_for(path) if surface != STARTUP),
        )

    @property
    def surfaces(self) -> tuple[str, ...]:
        return tuple(surface for surface, _ in self.by_surface)

    @property
    def total(self) -> int:
        return sum(count for _, counts in self.by_surface for _, count in counts)

    def as_dict(self) -> dict[str, dict[str, int]]:
        return {surface: dict(counts) for surface, counts in self.by_surface}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SurfaceCounts":
        return cls(by_surface=tuple((str(k), tuple(v.items())) for k, v in data.items()))


@dataclass(frozen=True)
class Probes:
    """Which hooks reported executing in one capture, and how many times.

    The probe is the one signal DFInsta emits **about itself**. A count here says
    a patched site ran; it does not say the request was blocked, and it must never
    be read as one — the site executes in every toggle state, because the toggle
    is tested inside the code the probe sits next to.

    What it is for is the other direction. A path whose count falls and whose hook
    never executed did not fall because of us, and a fall too small to clear the
    noise floor is a different thing when our machinery demonstrably ran in every
    session of the arm. On 442 that was the whole difference between "we cannot
    say" and "we cannot say from counting alone".

    A hook that never reported is **absent**, never a recorded zero — the rule
    `counts` and `Refusals` both follow. Absence of the whole object is different
    again and is spelled by `None`: it means nobody looked for probes, which is
    every row recorded before 2026-08-17.
    """

    #: `(hook_id, count)`, sorted by hook. A Mapping is accepted and normalised.
    by_hook: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.by_hook, Mapping):
            object.__setattr__(
                self, "by_hook", tuple(sorted((str(k), int(v)) for k, v in self.by_hook.items()))
            )
        else:
            object.__setattr__(
                self, "by_hook", tuple(sorted((str(k), int(v)) for k, v in self.by_hook))
            )
        for hook, count in self.by_hook:
            if not hook.strip():
                raise ObservationError("a probe count carries an empty hook id")
            if count < 1:
                raise ObservationError(
                    f"probe {hook!r} carries {count}; a hook that never reported is "
                    "absent, not a recorded zero"
                )
        if len({hook for hook, _ in self.by_hook}) != len(self.by_hook):
            raise ObservationError("a probe hook id appears twice")

    def count(self, hook_id: str) -> int:
        return dict(self.by_hook).get(hook_id, 0)

    @property
    def hooks(self) -> tuple[str, ...]:
        return tuple(hook for hook, _ in self.by_hook)

    @property
    def total(self) -> int:
        return sum(count for _, count in self.by_hook)

    def as_dict(self) -> dict[str, int]:
        return {hook: count for hook, count in self.by_hook}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Probes":
        return cls(by_hook=tuple(sorted((str(k), int(v)) for k, v in data.items())))


@dataclass(frozen=True)
class Refusals:
    """Which paths the guard refused in one capture, and how many times.

    Read from the build's own `!blocked` lines, so each count is a decision this
    build made rather than a report Instagram chose to emit about it. That is the
    whole difference from :class:`BlockCount`, which counts Instagram's error
    events: this one cannot be under-reported by a feature we do not control, and
    it names a **path** where the event names only a feature category.

    A literal with no refusals is **absent**, never a recorded zero — the rule
    `BlockCount` and `counts` both follow. Absence of the whole object is
    different again and is spelled by `None`: it means the build never said it
    could report refusals, which is every build made before 2026-08-13.

    The literal is the one the *rule* tested, which is not always the one the
    request path would suggest. `/api/v1/clips/discover/stream/` is refused by the
    `/clips/discover` rule and recorded under that name, because that is the rule
    that fired. A watched path which no rule names can therefore never appear here,
    and `grouping` says "covered by" rather than inventing a count for it.
    """

    #: `(literal, count)`, sorted by literal. A Mapping is accepted and normalised.
    by_literal: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        given = self.by_literal
        items = list(given.items()) if isinstance(given, Mapping) else list(given)
        cleaned: list[tuple[str, int]] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, Sequence) or isinstance(item, (str, bytes)) or len(item) != 2:
                raise ObservationError(
                    f"a refusal count is (literal, count) pairs, got {item!r}"
                )
            literal, count = item
            literal = str(literal)
            if not literal.strip() or literal != literal.strip():
                raise ObservationError(
                    f"{literal!r} is not a path literal. It is compared verbatim against "
                    "the watch list, so a blank or padded one is a refusal attributed to "
                    "a path no build was testing"
                )
            if not isinstance(count, int) or isinstance(count, bool) or count < 1:
                raise ObservationError(
                    f"refusals for {literal} count {count!r}; a recorded zero is a second "
                    "spelling of absent, in the one field whose zero is evidence"
                )
            if literal in seen:
                raise ObservationError(f"a refusal count names {literal} twice")
            seen.add(literal)
            cleaned.append((literal, count))
        object.__setattr__(self, "by_literal", tuple(sorted(cleaned)))

    @classmethod
    def of(
        cls, by_literal: Mapping[str, int] | Iterable[tuple[str, int]] = ()
    ) -> "Refusals":
        return cls(
            tuple(by_literal.items()) if isinstance(by_literal, Mapping) else tuple(by_literal)
        )

    @property
    def total(self) -> int:
        return sum(count for _, count in self.by_literal)

    @property
    def literals(self) -> tuple[str, ...]:
        return tuple(literal for literal, _ in self.by_literal)

    def get(self, literal: str) -> int:
        """How many times this literal was refused. Zero is a real answer here.

        Safe *because* the object exists at all: a `Refusals` was only built from a
        build that said it could report refusals, so a literal missing from it was
        measured and not refused. The distinction lives one level up, in whether
        this object is `None`.
        """

        return dict(self.by_literal).get(literal, 0)

    @property
    def text(self) -> str:
        if not self.by_literal:
            return "0"
        return f"{self.total} (" + ", ".join(
            f"{literal} {count}" for literal, count in self.by_literal
        ) + ")"

    def as_dict(self) -> dict[str, int]:
        return dict(self.by_literal)

    @classmethod
    def from_dict(cls, data: Any) -> "Refusals":
        if not isinstance(data, Mapping):
            raise ObservationError(
                f"refusals must be an object of literal -> integer, got "
                f"{type(data).__name__}"
            )
        return cls.of({str(key): value for key, value in data.items()})

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.text


@dataclass(frozen=True)
class Capture:
    """What one logcat capture says: the configuration, what was asked for, what
    was refused.

    All from one pass over the text. Two passes could disagree about which lines
    they saw, and both ordering rules — no path before the directive, and no block
    header before it either — are only checkable while reading in order.
    """

    #: `None` when the capture carries no directive, which only a capture with no
    #: path lines at all may do. Never a default of "all off": see the module
    #: docstring.
    toggles: ToggleState | None
    counts: Mapping[str, int] = field(default_factory=dict)
    #: Always a real count, never `None`: a capture that was read was counted. The
    #: default is for the hand-made fixtures that predate this field, and it says
    #: "this text held no block header", which is what reading it would find.
    #:
    #: **Superseded by `refusals` for every question about attribution.** It is
    #: Instagram's count of our refusals and it under-reports by feature; it stays
    #: because 48 committed sessions carry it and because a capture that was read
    #: was read, not because anything should still be derived from it.
    blocks: BlockCount = field(default_factory=lambda: BlockCount(0))
    #: What the guard itself said it refused. `None` — unlike `blocks` — because a
    #: build that never claimed the capability could not have written a `!blocked`
    #: line, and reading its silence as "nothing was refused" would make every
    #: session recorded before 2026-08-13 evidence for a fact none of them measured.
    #: `Refusals()` with nothing in it is the *measured* statement that this
    #: configuration refused nothing, and it is the baseline everything else is
    #: compared against.
    #: Which hooks reported executing. Always present from `parse`, because a
    #: reader that looked is what makes an empty one mean "no hook reported" —
    #: `None` is a row recorded before anything looked for probes at all.
    probes: "Probes | None" = None

    #: Which surface was on screen for each request. `None` when the walk never
    #: announced one — every capture before 2026-08-18, and any run whose harness
    #: did not annotate. Unlike `probes`, absence here is normal rather than
    #: historical: a capture taken by hand has no walk to mark it.
    per_surface: "SurfaceCounts | None" = None

    #: Defaulted to `None` and **not** to an empty `Refusals`: a hand-built capture
    #: that said nothing about refusals must not come out saying none happened.
    #: `parse` always sets this explicitly, so the default is reached only by a
    #: caller constructing one — which is exactly where the rule is easiest to lose.
    refusals: "Refusals | None" = None
    #: Whole seconds from the first line this parse read to the last, or `None`
    #: when fewer than two of them carried a logcat timestamp. `None` and `0` are
    #: different facts — `0` is a capture whose lines all landed inside one second
    #: — and they are spelled apart for the reason `blocks` is.
    #:
    #: A **lower** bound on how long the walk took: the capture starts at the first
    #: checked request, not at the first tap, and ends at the last request rather
    #: than when the operator put the phone down. That is why it is only ever
    #: compared against another span and never against a number somebody chose.
    span_seconds: int | None = None

    @property
    def stated(self) -> bool:
        return self.toggles is not None


#: Days before the first of each month in a non-leap year. A logcat stamp carries
#: no year, and none is needed: the only question asked of two stamps is how far
#: apart they are, over a walk that lasts minutes.
_MONTH_START = (0, 0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)


def _note_stamp(line: str, into: list[float]) -> None:
    """Record `line`'s logcat timestamp, if it carries one.

    Called only for the lines `parse` actually **read**, never for every line in
    the text. `tools/redact_capture.py` keeps a superset of those and drops the
    rest, and its whole guarantee is that `parse` gives the identical answer for
    the reduction and the original — a span measured over lines the redaction is
    free to drop would break that guarantee for every capture at once.
    """

    match = _STAMP.match(line)
    if match is None:
        return
    month, day, hour, minute, second, fraction = match.groups()
    if not 1 <= int(month) <= 12:
        return
    into.append(
        (_MONTH_START[int(month)] + int(day)) * 86400.0
        + int(hour) * 3600
        + int(minute) * 60
        + int(second)
        + int(fraction) / (10 ** len(fraction))
    )


def _span(stamps: Sequence[float]) -> int | None:
    """Whole seconds between the earliest and latest stamp, or `None` under two.

    Extremes rather than first-and-last: logcat interleaves buffers, so two lines
    can arrive a few milliseconds out of order, and a subtraction that could come
    out negative is one somebody would have to interpret.

    Truncated to whole seconds. The comparison this feeds is between walks that
    differ by minutes, and a float would put two spellings of one duration into a
    store whose readers compare rows for equality.

    A capture that crosses midnight on 31 December reads wrong here, because a
    logcat stamp carries no year. It is not a refusal: this is corroborating
    evidence rather than a term of the contract, and a walk that appeared to take
    a year is loud in every report it reaches rather than quietly plausible.
    """

    if len(stamps) < 2:
        return None
    return int(max(stamps) - min(stamps))


def _marks(payload: str, number: int) -> tuple[frozenset[str], str]:
    """Split a toggle line's payload into what the build *can say* and what it says.

    Exact rather than conventional: a preference key matches :data:`_TOGGLE_NAME`
    and so can never begin with :data:`REPORTS_MARK`, and `ToggleState.parse`
    refuses any token that is not `key=0` or `key=1` — so neither reader can
    swallow the other's tokens even if this split were removed.

    An unknown capability is **refused**, for the reason an unknown directive is:
    a host that ignored it would read a newer build's capture as though the build
    had claimed nothing, which is precisely the "could not have said" answer the
    mark exists to distinguish from "said nothing happened".
    """

    marks: set[str] = set()
    rest: list[str] = []
    for token in payload.split():
        if not token.startswith(REPORTS_MARK):
            rest.append(token)
            continue
        name = token[len(REPORTS_MARK):]
        if name not in KNOWN_REPORTS:
            raise ObservationError(
                f"line {number}: the build states it can report {name!r}, which this "
                "host does not know how to read. The build is newer than the reader, "
                "and a capture whose claims are not all understood cannot be recorded"
            )
        marks.add(name)
    return frozenset(marks), " ".join(rest)


def parse(text: str) -> Capture:
    """Read one capture: the state it states, every path it counted, every block.

    Counts **every** appearance of each literal, ordered by its first. The count
    is what matters and the ordering is incidental — an earlier docstring said
    "by first appearance" of the counting rather than of the order, which reads as
    though repeats are collapsed. They are not: two requests for one path are two
    requests.

    Refuses rather than skips. A line under this tag whose payload is empty or
    padded is a build that is not honouring the contract, and quietly dropping it
    would subtract requests that did happen from a count whose whole purpose is
    to be compared against zero.

    Refuses a **path before any directive**, and two directives that disagree.
    The app restates its configuration on every checked request, ahead of the
    path lines that request produces, so a path line with no directive in front
    of it is a capture whose start was cut off — `logcat -c` landing between the
    two lines of one request, or a file assembled from pieces. Its counts belong
    to a configuration nobody can name, and the alternative to refusing is to
    attribute them to whichever state does appear.

    Repeats are collapsed rather than counted: the same statement made 22 times
    is one statement. Two *different* statements are two experiments, and this is
    the shape a toggle changed halfway through a session takes — visible only
    because the line repeats, which is why it repeats.

    **And it measures the span**, from the first line it read to the last. That is
    the only thing in a capture that constrains which *walk* produced it — the app
    never learns the protocol, so the walk is typed, and a typed value with no
    evidence beside it is a value nothing can contradict. See the module docstring
    and :func:`walk_dispute`. Never a refusal: a capture with no timestamps is the
    bare contract form, and it records honestly with no span.

    **Blocks are counted under the same ordering rule as paths.** A block header
    ahead of any directive belongs to a request this capture did not see begin, and
    it would be added to the count a reader compares against zero — a phantom block
    in a baseline is the one number that must not be inventable. It is counted from
    the header alone, never from the echo of the same event in its own payload;
    :data:`_BLOCK_HEADER` says why at length.
    """

    counts: dict[str, int] = {}
    features: dict[str, int] = {}
    refused: dict[str, int] = {}
    probed: dict[str, int] = {}
    on_surface: dict[str, dict[str, int]] = {}
    #: Requests before the first marker belong to app startup, not to whichever
    #: tab the walk happened to visit first.
    surface = STARTUP
    marked = False
    blocks = 0
    toggles: ToggleState | None = None
    reports: frozenset[str] | None = None
    previous = ""
    stamps: list[float] = []
    for number, line in enumerate(text.splitlines(), 1):
        if _BLOCK_HEADER.match(line):
            _note_stamp(line, stamps)
            if toggles is None:
                raise ObservationError(
                    f"line {number}: a block was reported before any {TOGGLE_DIRECTIVE} "
                    "line. Blocks are caused by our own toggles, so one that arrives "
                    "before the build has said which were active cannot be attributed to "
                    "a configuration — and counting it would put a block into a state "
                    "that may have had none"
                )
            blocks += 1
            named = _FEATURE_LINE.match(previous)
            feature = named.group("feature") if named else UNATTRIBUTED
            features[feature] = features.get(feature, 0) + 1
            previous = line
            continue
        previous = line
        walked = _WALK_LINE.match(line)
        if walked is not None:
            named = walked.group("surface")
            if not named.strip() or named != named.strip():
                raise ObservationError(
                    f"line {number}: {WALK_TAG} announced surface={named!r}. The contract is "
                    "one line per surface the walk moves to, and its message names that "
                    "surface — an empty or padded one attributes every request after it to "
                    "nothing"
                )
            _note_stamp(line, stamps)
            surface, marked = named, True
            continue
        probe = _PROBE_LINE.match(line)
        if probe is not None:
            hook = probe.group("hook")
            if not hook.strip() or hook != hook.strip():
                raise ObservationError(
                    f"line {number}: {PROBE_TAG} emitted {hook!r}. The contract is one "
                    "line per hook whose site executed, and its message is the hook id "
                    "— an empty or padded one cannot be attributed to any hook"
                )
            _note_stamp(line, stamps)
            probed[hook] = probed.get(hook, 0) + 1
            continue
        match = _OBSERVE_LINE.match(line)
        if match is None:
            continue
        _note_stamp(line, stamps)
        literal = match.group("literal")
        if not literal.strip():
            raise ObservationError(
                f"line {number}: a {TAG} line carries no path literal. The contract is "
                "one line per observed request, and its message is the literal — an "
                "empty one cannot be attributed to any watched path"
            )
        if literal != literal.strip():
            raise ObservationError(
                f"line {number}: {TAG} emitted {literal!r}, which is padded. The message "
                "is compared verbatim against the watch list, so a padded literal would "
                "be counted against a path no build is watching"
            )
        if literal.startswith("!"):
            keyword, _, payload = literal.partition(" ")
            if keyword == BLOCKED_DIRECTIVE:
                if toggles is None:
                    raise ObservationError(
                        f"line {number}: a refusal was reported before any "
                        f"{TOGGLE_DIRECTIVE} line. A refusal is caused by our own "
                        "toggles, so one that arrives before the build has said which "
                        "were active cannot be attributed to a configuration — and "
                        "counting it would put a refusal into a state that may have "
                        "had none"
                    )
                if reports is None or REPORTS_BLOCKED not in reports:
                    # Not a tolerable inconsistency: the two facts come from one
                    # build, and a build that writes refusals it never claimed it
                    # could write means the reader's model of that build is wrong.
                    # Believing the lines anyway would be believing a contract
                    # nobody stated.
                    raise ObservationError(
                        f"line {number}: {TAG} reported a refusal, and the build never "
                        f"stated `{REPORTS_MARK}{REPORTS_BLOCKED}` on its "
                        f"{TOGGLE_DIRECTIVE} line. The guard and the class it logs "
                        "through disagree about what this build can report, so nothing "
                        "in this capture's refusals can be trusted to be all of them"
                    )
                if not payload.strip() or payload != payload.strip():
                    raise ObservationError(
                        f"line {number}: {TAG} reported the refusal {payload!r}, which "
                        "names no path or a padded one. The name is compared verbatim "
                        "against the watch list, so it would count against a path no "
                        "build was testing"
                    )
                refused[payload] = refused.get(payload, 0) + 1
                continue
            if keyword != TOGGLE_DIRECTIVE:
                # Forward compatibility that fails closed. A host that ignored an
                # unknown directive would read a newer build's capture as though
                # it had said nothing new; one that counted it would manufacture
                # a request for `!version`.
                raise ObservationError(
                    f"line {number}: {TAG} emitted the directive {keyword!r}, which this "
                    "host does not know. The build is newer than the reader, and a "
                    "capture whose statements are not all understood cannot be recorded"
                )
            claimed, state_text = _marks(payload, number)
            try:
                stated = ToggleState.parse(state_text)
            except ObservationError as error:
                raise ObservationError(f"line {number}: {error}") from error
            if toggles is not None and toggles != stated:
                raise ObservationError(
                    f"line {number}: this capture states two toggle states, "
                    f"{toggles.text!r} then {stated.text!r}. A toggle was changed while "
                    "the session was being walked, or two captures were concatenated; "
                    "either way no line says which counts belong to which "
                    "configuration, so this is two experiments at once"
                )
            if reports is not None and reports != claimed:
                # Same argument as two toggle states, and the same cause: one
                # capture cannot have been taken from two builds. Here it would be
                # worse than a wrong count — half the capture could report refusals
                # and half could not, and the zero would be read across both.
                raise ObservationError(
                    f"line {number}: this capture states two different sets of build "
                    f"capabilities, {sorted(reports)} then {sorted(claimed)}. Two "
                    "builds' logs were concatenated, and a refusal count over both "
                    "would be a count of one build's refusals divided by the other's "
                    "requests"
                )
            toggles = stated
            reports = claimed
            continue
        if toggles is None:
            raise ObservationError(
                f"line {number}: {literal} was reported before any {TOGGLE_DIRECTIVE} "
                "line. The build restates which blocks were active on every checked "
                "request, ahead of the paths that request reports, so a path in front of "
                "one comes from a request this capture did not see begin — the start of "
                "the file was cut off, or it was assembled from pieces. Its counts "
                "cannot be attributed to any configuration, and a zero measured under an "
                "unknown one is not evidence about the app"
            )
        counts[literal] = counts.get(literal, 0) + 1
        on_surface.setdefault(surface, {})
        on_surface[surface][literal] = on_surface[surface].get(literal, 0) + 1
    return Capture(
        toggles=toggles,
        counts=counts,
        blocks=BlockCount.of(blocks, features),
        # `None` unless the build said it could report them. An empty `Refusals`
        # is the measured statement that nothing was refused; `None` is a build
        # that could not have said either way, and the two must never collapse.
        refusals=(
            Refusals.of(refused)
            if reports is not None and REPORTS_BLOCKED in reports
            else None
        ),
        # Always a real `Probes`, never `None`, because a parser that looked is
        # what makes an empty one mean "no hook reported". `None` is reserved for
        # a stored row written before anything looked — see `Capture.probes`.
        probes=Probes(probed),
        # `None` unless the walk actually annotated the stream. An empty record
        # would say "we attributed everything to nothing", which is what a capture
        # from a walk that never marked would wrongly look like.
        per_surface=SurfaceCounts(on_surface) if marked else None,
        span_seconds=_span(stamps),
    )


def _stamp(value: str) -> str:
    """An ISO 8601 timestamp with a UTC offset, stripped. Refuses anything else.

    Parsed rather than checked for emptiness, for the reason a sibling record
    store found the hard way: `--recorded-at banana` exited 0 and wrote into an
    append-only file nothing ever deletes from. `Z` is accepted and not
    rewritten — what a human typed is what gets recorded, and
    `datetime.fromisoformat` reads both spellings back.
    """

    from datetime import datetime  # noqa: PLC0415  (only this one function needs it)

    stamp = value.strip()
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError as error:
        raise ObservationError(
            f"{value!r} is not an ISO 8601 timestamp ({error}). It would be written "
            "into an append-only store nothing ever deletes from"
        ) from error
    if parsed.tzinfo is None:
        raise ObservationError(
            f"{value!r} has no UTC offset. A naive stamp cannot be ordered against one "
            "written on another machine, and two sessions being orderable is what makes "
            "them comparable"
        )
    return stamp


@dataclass(frozen=True)
class ObservationSession:
    """One device session: which build watched what, and what it saw.

    Every field that makes the row joinable is required. A measurement that names
    no version, no build and no time is a number nobody can join to anything —
    that was found once already, by asking what a report could honestly say about
    an evidence claim, and the answer was "nothing".
    """

    schema_version: int
    #: The Instagram version the observing build was made from.
    version: str
    #: The APK the operator actually had installed. The device serial identifies
    #: a phone, never a build, so without this the row cannot be joined to what
    #: was measured.
    build_sha256: str
    #: Supplied, never read from the clock here — as everywhere else in this repo.
    #: **Parsed**, with a required UTC offset, and stored stripped. Not merely
    #: checked for emptiness: a sibling record store was checked that way and
    #: `--recorded-at banana` exited 0 into an append-only file nothing ever
    #: deletes from. A naive stamp cannot be ordered against one written on
    #: another machine, which is the whole point of two sessions being comparable.
    recorded_at: str
    session_id: str
    #: Free text: which surface the operator walked, e.g. `feed_tab`. Load-bearing
    #: rather than decorative — a zero means "not on this surface", and a reader
    #: who cannot see which surface cannot read the zero.
    surface: str
    #: Every literal this build was watching. The population the negative claim
    #: is made over; without it a zero count is indistinguishable from a path the
    #: build never looked for.
    watched: tuple[str, ...]
    #: Which blocks were active, as the **build** reported them — never as an
    #: operator typed them. `None` means the capture did not say, which is a
    #: value that has to be written down rather than defaulted: it is the state
    #: of the one row recorded before builds reported themselves, and it answers
    #: no question that depends on the configuration. Required, and deliberately
    #: ahead of `counts`, so that every construction site states it.
    toggles: ToggleState | None
    #: Which driving script produced this session — the *protocol*, not the
    #: outcome. `one-pass-three-surfaces` and `three-round-v2` are walks;
    #: `saw-lots-of-reels` and a session id are not. Two sessions naming one walk
    #: are a claim that they did the same thing, which is what makes a difference
    #: between them attributable to the toggle that changed.
    #:
    #: **Typed by the operator**, unlike `toggles`, because there is nowhere else
    #: for it to come from: the walk lives in the driving script, and nothing on the
    #: phone or in the capture names it. The module docstring says so at length
    #: rather than implying a guarantee this does not have. `None` means not
    #: stated, which is the shape every row committed before 2026-08-11 is in;
    #: `append` refuses it for anything new. Required, and deliberately ahead of
    #: `counts`, for the reason `toggles` is.
    walk: str | None
    #: Which surface was on screen for each request. `None` when the walk did not
    #: annotate the stream, which is every row before 2026-08-18.
    per_surface: "SurfaceCounts | None" = None

    #: Which hooks reported executing, from the build's own probe lines. `None`
    #: is a row recorded before anything looked; an empty `Probes` is a reader
    #: that looked and found none, which is evidence and not an absence.
    probes: "Probes | None" = None

    counts: Mapping[str, int] = field(default_factory=dict)
    #: How many requests the guard refused, as the capture reported them. `None`
    #: means **nobody counted** — the shape of every row written before this host
    #: read the block header at all — and it is not `BlockCount(0)`, which is the
    #: measured statement that a state refused nothing. That distinction is the
    #: whole of the baseline: `grouping` calls a toggle a blocking one by comparing
    #: its arm against a baseline of zero, and an uncounted zero would let a row
    #: that measured nothing supply the control. Defaulted rather than required,
    #: unlike `toggles`, because a row already in the store has to keep reading;
    #: `append` is where the rule that new evidence must state it lives.
    blocks: BlockCount | None = None
    #: What the **guard itself** said it refused, per path. `None` means the build
    #: could not have said — it never claimed `+blocked` on its toggle line — which
    #: is every build made before 2026-08-13 and therefore every row committed
    #: before then. An empty `Refusals` is the measured statement that this
    #: configuration refused nothing, and that is the baseline `grouping` compares
    #: an arm against.
    #:
    #: This is what `blocks` was always a proxy for. `blocks` counts the error
    #: events *Instagram* emitted about our exception, which under-report by
    #: feature and name a feature category rather than a path, and every derivation
    #: built on top of them had to work out which path an untagged total belonged
    #: to. Nothing needs to now.
    refusals: "Refusals | None" = None
    #: How long the capture ran, as :func:`parse` measured it. The evidence the
    #: typed `walk` is checked against, and the reason a wrong walk is catchable at
    #: all. `None` means the capture carried no timestamps — the bare contract
    #: form, and every row written before this was measured — and it is not `0`,
    #: which is a capture whose lines all landed inside one second. Defaulted
    #: rather than required, because it is derived and a row already in the store
    #: has to keep reading.
    span_seconds: int | None = None

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ObservationError(
                f"unsupported observation schema {self.schema_version!r}"
            )
        # `str(...)` on both, because these run on values that may have come
        # straight out of JSON: a numeric `version` would otherwise make the
        # *guard* raise TypeError, replacing a legible refusal with a traceback
        # about the refusal — the shape `rulings.unenforced_endpoints` notes.
        if not _NUMERIC.fullmatch(str(self.version)):
            raise ObservationError(f"{self.version!r} is not a version number")
        if not SHA256_PATTERN.fullmatch(str(self.build_sha256 or "")):
            raise ObservationError(
                f"build_sha256 must be a lowercase SHA-256, got {self.build_sha256!r}. "
                "A session that cannot name the APK it measured cannot be joined to one"
            )
        for value, label in (
            (self.recorded_at, "recorded_at"),
            (self.session_id, "session_id"),
            (self.surface, "surface"),
        ):
            if not str(value).strip():
                raise ObservationError(
                    f"an observation session is missing {label}. A measurement nobody "
                    "can place in time, tell apart from another, or attribute to a "
                    "surface is not evidence a human can read a zero from"
                )
        # Stored stripped, and the stripped value is what `to_dict` writes.
        # Validating `value.strip()` and then recording `value` put the padding
        # into a permanent record in a form the next read refuses — the same
        # defect a sibling record store shipped and this one inherited the fix for.
        object.__setattr__(self, "recorded_at", _stamp(self.recorded_at))
        object.__setattr__(self, "watched", tuple(self.watched))
        # Copied, like `watched` is. Keeping the caller's live mapping meant every
        # check below could be undone after construction: a count added afterwards
        # produced a row `append` wrote and this module's own `read` then refused —
        # a store its writer made and its reader rejects.
        object.__setattr__(self, "counts", dict(self.counts))
        if not self.watched:
            raise ObservationError(
                f"session {self.session_id} watched nothing. The negative claim is made "
                "over the watch list, so an empty one makes every count unattributable "
                "and the session unable to support any statement at all"
            )
        blank = [item for item in self.watched if not str(item).strip()]
        if blank:
            raise ObservationError(
                f"session {self.session_id} has a blank entry in its watch list"
            )
        repeated = sorted({
            item for item in self.watched if self.watched.count(item) > 1
        })
        if repeated:
            raise ObservationError(
                f"session {self.session_id} watches {', '.join(repeated)} more than "
                "once. Two spellings of one fact is how a count comes to be read twice"
            )
        for literal, count in self.counts.items():
            if not isinstance(count, int) or isinstance(count, bool) or count < 1:
                raise ObservationError(
                    f"session {self.session_id}: {literal} has count {count!r}. `parse` "
                    "never produces a zero, so a recorded zero is a second spelling of "
                    "'absent' — and absence is exactly what this store must not blur"
                )
        unwatched = sorted(set(self.counts) - set(self.watched))
        if unwatched:
            raise ObservationError(
                f"session {self.session_id} counted {', '.join(unwatched)}, which it was "
                "not watching. The build and the watch list disagree about what was "
                "being watched, so nothing this session did not see can be relied on"
            )
        if self.toggles is not None and not isinstance(self.toggles, ToggleState):
            # Not coerced from a mapping. A state is normalised — sorted, with
            # every value a real boolean — and a raw dict slipping through would
            # compare unequal to the same state read back out of the store, which
            # is the one comparison every answer here is grouped by.
            raise ObservationError(
                f"session {self.session_id} has toggles {self.toggles!r}; pass a "
                "ToggleState (ToggleState.of({'disable_feed': True, ...})) or None"
            )
        if self.walk is not None:
            if not isinstance(self.walk, str):
                # Not coerced, for the reason `toggles`, `blocks` and
                # `span_seconds` are not: `str(123)` would put `"123"` in the one
                # field this store joins rows on, and a hand-edited `"walk": 123`
                # would then match a session that typed the digits.
                raise ObservationError(
                    f"session {self.session_id} has walk {self.walk!r}; a walk is the "
                    "name of a driving protocol, as a string"
                )
            walk = self.walk
            if not _WALK.fullmatch(walk):
                raise ObservationError(
                    f"session {self.session_id} names the walk {self.walk!r}, which is "
                    "not a walk identifier. It is lowercase, starts with a letter or a "
                    "digit, and carries only letters, digits, `.`, `-` and `_` — "
                    "`three-round-v2`. It is a join key and not prose: `grouping` "
                    "compares only sessions naming the same walk, so two spellings of "
                    "one protocol would quietly halve every group. It is not "
                    "lowercased for you, because a value this store rewrote would stop "
                    "matching the one in the operator's notes"
                )
            if walk == str(self.session_id).strip():
                raise ObservationError(
                    f"session {self.session_id} names itself as its walk. A walk is the "
                    "protocol several sessions share — it is what says two of them did "
                    "the same thing — so one that is unique to a session names an "
                    "outcome, and every group it could form has one member"
                )
            object.__setattr__(self, "walk", walk)
        if self.span_seconds is not None and (
            not isinstance(self.span_seconds, int)
            or isinstance(self.span_seconds, bool)
            or self.span_seconds < 0
        ):
            raise ObservationError(
                f"session {self.session_id} has span_seconds {self.span_seconds!r}; a "
                "span is a whole number of seconds measured from the capture, or None "
                "when it carried no timestamps"
            )
        if self.blocks is not None and not isinstance(self.blocks, BlockCount):
            # Not coerced from an int either. `blocks=0` reads as "no blocks" and
            # would be indistinguishable, once stored, from a real measurement —
            # which is the one distinction this field exists to keep.
            raise ObservationError(
                f"session {self.session_id} has blocks {self.blocks!r}; pass a "
                "BlockCount (BlockCount.of(20, {'FEED_NOT_LOADING': 20})) or None"
            )
        if self.refusals is not None:
            if not isinstance(self.refusals, Refusals):
                # Not coerced from a mapping, for the reason `toggles` and `blocks`
                # are not: `refusals={}` would be indistinguishable, once stored,
                # from a build that could not report them — and that is the one
                # distinction this field exists to keep.
                raise ObservationError(
                    f"session {self.session_id} has refusals {self.refusals!r}; pass a "
                    "Refusals (Refusals.of({'/feed/timeline/': 20})) or None"
                )
            unwatched = sorted(set(self.refusals.literals) - set(self.watched))
            if unwatched:
                raise ObservationError(
                    f"session {self.session_id} refused {', '.join(unwatched)}, which it "
                    "was not watching. The guard blocked a path the build's own watch "
                    "list does not name, so the two halves of this capture came from "
                    "different builds"
                )
            # The observe pass runs at the top of `throwIfBlocked` and tests every
            # watched literal with `contains`, so a request the guard refuses under
            # a literal was necessarily reported under that same literal a few
            # instructions earlier. More refusals than requests therefore cannot
            # happen in one build, and where it does it means lines were lost or a
            # row was edited — the two independent halves of a capture checking
            # each other, which is the only control a single session has.
            excess = sorted(
                literal
                for literal, count in self.refusals.by_literal
                if count > self.counts.get(literal, 0)
            )
            if excess:
                raise ObservationError(
                    f"session {self.session_id} refused {', '.join(excess)} more often "
                    "than it observed them. Every refusal follows an observation of the "
                    "same literal in the same call, so the capture lost lines — logd "
                    "drops the oldest first and the observation is the older of the two "
                    "— or the row was edited. The first is the likely one and the repair "
                    "is a larger buffer and another walk"
                )

    @property
    def total(self) -> int:
        """Every request this session observed, across all watched paths."""

        return sum(self.counts.values())

    @property
    def vacuous(self) -> bool:
        """Did this session observe nothing at all?

        Derived from the session's own output — `total > 0` and no constant. A
        vacuous session is equally well explained by a build that was not
        observing, a capture that was empty, and an app that never ran, so it is
        no evidence about any path. See the module docstring.
        """

        return self.total == 0

    @property
    def observed(self) -> tuple[str, ...]:
        return tuple(sorted(self.counts))

    @property
    def unobserved(self) -> tuple[str, ...]:
        """Watched by this session and not seen by it. Meaningless when vacuous."""

        return tuple(sorted(set(self.watched) - set(self.counts)))

    def to_dict(self) -> dict[str, Any]:
        stated = {"toggles": self.toggles.as_dict()} if self.toggles is not None else {}
        # Same rule, same reason: absent means nobody counted. `append` rewrites
        # the whole file, so the twelve 439 rows written before this field existed
        # come back byte for byte rather than acquiring a zero nobody measured.
        counted = {"blocks": self.blocks.as_dict()} if self.blocks is not None else {}
        # Same rule a fourth time, and the strongest case for it: a build that
        # could not report refusals must come back with the key absent, never with
        # an empty object that reads as "refused nothing".
        refused = (
            {"refusals": self.refusals.as_dict()} if self.refusals is not None else {}
        )
        # Same rule a fifth time. A row written before anything looked for probes
        # comes back with the key absent, never with an empty object that would
        # read as "no hook executed" — which is a measurement none of those rows
        # took.
        probed = (
            {"probes": self.probes.as_dict()} if self.probes is not None else {}
        )
        # Same rule a sixth time.
        surfaced = (
            {"per_surface": self.per_surface.as_dict()}
            if self.per_surface is not None
            else {}
        )
        # Same rule a third time. The twelve 439 rows written before a walk was
        # named come back byte for byte rather than acquiring a walk nobody stated
        # or a span nobody measured.
        driven = {"walk": self.walk} if self.walk is not None else {}
        timed = (
            {"span_seconds": self.span_seconds} if self.span_seconds is not None else {}
        )
        return {
            "schema_version": self.schema_version,
            "version": self.version,
            "build_sha256": self.build_sha256,
            "recorded_at": self.recorded_at,
            "session_id": self.session_id,
            "surface": self.surface,
            "watched": list(self.watched),
            # Written only when the capture stated one. An unknown state is
            # spelled by the key's absence, which is how the row recorded before
            # builds reported themselves is already spelled — so `append`, which
            # rewrites the whole file, gives that row back byte for byte instead
            # of editing a store nothing is allowed to edit.
            **stated,
            **driven,
            **timed,
            **counted,
            **refused,
            **probed,
            **surfaced,
            "counts": dict(sorted(self.counts.items())),
            # Derived and written anyway, following `SignalCount.to_dict`. It is
            # the number a human reads first, and `from_dict` refuses a row whose
            # total disagrees with its counts — so a hand-edit that changes one
            # count and forgets the total is caught instead of believed.
            "total": self.total,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ObservationSession":
        if not isinstance(data, Mapping):
            raise ObservationError(
                f"an observation session must be a JSON object, got {type(data).__name__}"
            )
        allowed = {
            "schema_version",
            "version",
            "build_sha256",
            "recorded_at",
            "session_id",
            "surface",
            "watched",
            "toggles",
            "walk",
            "span_seconds",
            "blocks",
            "refusals",
            "probes",
            "per_surface",
            "counts",
            "total",
        }
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise ObservationError(
                f"observation session has unknown keys: {', '.join(unknown)}"
            )
        watched = data.get("watched")
        if not isinstance(watched, Sequence) or isinstance(watched, (str, bytes)):
            raise ObservationError(
                f"watched must be a list of literals, got {type(watched).__name__}"
            )
        counts = data.get("counts", {})
        if not isinstance(counts, Mapping):
            raise ObservationError(
                f"counts must be an object of literal -> integer, got "
                f"{type(counts).__name__}"
            )
        toggles: ToggleState | None = None
        if "toggles" in data:
            raw = data["toggles"]
            if raw is None:
                # The same rule as the recorded zero below: absence has one
                # spelling. A row that says `null` looks like a build that
                # answered "nothing", and this store must not blur an answer
                # nobody gave with one somebody gave.
                raise ObservationError(
                    "an observation session states toggles: null. An unknown toggle "
                    "state is spelled by the key being absent — the way a row recorded "
                    "before the build reported its own state is spelled — and a null is "
                    "a second spelling of absent"
                )
            if not isinstance(raw, Mapping):
                raise ObservationError(
                    f"toggles must be an object of key -> true/false, got "
                    f"{type(raw).__name__}"
                )
            toggles = ToggleState.of({str(key): value for key, value in raw.items()})
        walk: str | None = None
        if "walk" in data:
            if data["walk"] is None:
                # The third field to say this, and the reason does not change: an
                # unstated walk is spelled by the key being absent, which is how
                # every row committed before 2026-08-11 is spelled, and a null is
                # a second spelling of absent.
                raise ObservationError(
                    "an observation session states walk: null. A walk nobody stated is "
                    "spelled by the key being absent — the way the rows written before "
                    "the walk was named are spelled — and a null is a second spelling "
                    "of absent"
                )
            # Passed through, not `str(...)` — unlike `version` and `session_id`
            # below, which are coerced so that a numeric one produces a legible
            # refusal instead of a TypeError inside the guard that reads it. This
            # is the key `grouping` joins rows on, and `123` coerced to `"123"`
            # would match a session that typed the digits. The constructor owns
            # the rule and refuses a non-string; a second copy of it here would be
            # a guard that can be deleted without changing an answer, which is how
            # the one that matters comes to be deleted beside it.
            walk = data["walk"]
        span: int | None = None
        if "span_seconds" in data:
            if data["span_seconds"] is None:
                raise ObservationError(
                    "an observation session states span_seconds: null. A capture whose "
                    "span was never measured is spelled by the key being absent, and a "
                    "null is a second spelling of absent — in a field whose zero is a "
                    "real measurement"
                )
            if not isinstance(data["span_seconds"], int) or isinstance(
                data["span_seconds"], bool
            ):
                raise ObservationError(
                    f"span_seconds must be a whole number of seconds, got "
                    f"{data['span_seconds']!r}"
                )
            span = data["span_seconds"]
        blocks: BlockCount | None = None
        if "blocks" in data:
            if data["blocks"] is None:
                raise ObservationError(
                    "an observation session states blocks: null. A block count nobody "
                    "took is spelled by the key being absent — the way the rows written "
                    "before this host counted them are spelled — and a null is a second "
                    "spelling of absent, in the one field whose zero is evidence"
                )
            blocks = BlockCount.from_dict(data["blocks"])
        per_surface: SurfaceCounts | None = None
        if "per_surface" in data:
            if data["per_surface"] is None:
                raise ObservationError(
                    "an observation session states per_surface: null. A row whose walk "
                    "did not annotate is spelled by the key being absent"
                )
            per_surface = SurfaceCounts.from_dict(data["per_surface"])
        probes: Probes | None = None
        if "probes" in data:
            if data["probes"] is None:
                raise ObservationError(
                    "an observation session states probes: null. A row nobody read "
                    "probes for is spelled by the key being absent, and a null is a "
                    "second spelling of absent in a field whose empty value is evidence"
                )
            probes = Probes.from_dict(data["probes"])
        refusals: Refusals | None = None
        if "refusals" in data:
            if data["refusals"] is None:
                raise ObservationError(
                    "an observation session states refusals: null. A build that could "
                    "not report refusals is spelled by the key being absent — the way "
                    "every row written before builds recorded their own refusals is "
                    "spelled — and a null is a second spelling of absent, in the one "
                    "field whose empty object is a measurement"
                )
            refusals = Refusals.from_dict(data["refusals"])
        session = cls(
            schema_version=data.get("schema_version"),
            version=str(data.get("version", "")),
            build_sha256=str(data.get("build_sha256", "")),
            recorded_at=str(data.get("recorded_at", "")),
            session_id=str(data.get("session_id", "")),
            surface=str(data.get("surface", "")),
            watched=tuple(str(item) for item in watched),
            toggles=toggles,
            walk=walk,
            span_seconds=span,
            blocks=blocks,
            refusals=refusals,
            probes=probes,
            per_surface=per_surface,
            counts={str(key): value for key, value in counts.items()},
        )
        stated = data.get("total")
        if stated is not None and stated != session.total:
            raise ObservationError(
                f"session {session.session_id} states total {stated!r} and its counts "
                f"sum to {session.total}. One of the two was edited without the other, "
                "and a store that disagrees with itself cannot be read"
            )
        return session


def store_path(version: str, root: Path | str = ".") -> Path:
    """Where one version's sessions live. `root` decides, always."""

    if not _NUMERIC.fullmatch(version):
        raise ObservationError(f"{version!r} is not a version number")
    return Path(root) / OBSERVATIONS / f"{version}.jsonl"


def read(
    version: str, root: Path | str = ".", *, path: Path | str | None = None
) -> tuple[ObservationSession, ...]:
    """Every recorded session for `version`, in file order.

    A missing file means none, which is the ordinary state before any device
    session has been taken. A file that exists and cannot be read is a **refusal**
    — including a path that is a directory, or bytes that are not UTF-8. Those
    are the shapes that make `is_file()` answer False and turn "unreadable" into
    "absent", which is the defect `expectation.sweep` names in as many words.

    Every row must name `version`. A 440 session filed under 441 would make a
    negative claim about a build that was never installed.
    """

    location = Path(path) if path is not None else store_path(version, root)
    # `stat`, not `exists()` / `is_file()`. Both of those answer False for a
    # directory, for a dangling symlink and for a path under a directory this
    # process may not traverse — three unreadable stores wearing the answer
    # "there is nothing here", in the one function whose empty result means
    # "nothing is wrong".
    try:
        status = location.stat()
    except FileNotFoundError as error:
        if location.is_symlink():
            raise ObservationError(
                f"{location} is a symlink that points nowhere. Somebody meant a store "
                "to be there, so this is not the same fact as no store at all"
            ) from error
        return ()
    except OSError as error:
        raise ObservationError(f"{location}: {error}") from error
    if not stat.S_ISREG(status.st_mode):
        raise ObservationError(
            f"{location} exists and is not a regular file, so its sessions cannot be "
            "read. Treating that as 'no sessions' would report an unreadable store as "
            "an empty one"
        )
    try:
        text = location.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ObservationError(f"{location}: {error}") from error

    out: list[ObservationSession] = []
    seen: set[str] = set()
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ObservationError(f"{location}:{number}: {error}") from error
        try:
            session = ObservationSession.from_dict(row)
        except ObservationError as error:
            raise ObservationError(f"{location}:{number}: {error}") from error
        if session.version != version:
            raise ObservationError(
                f"{location}:{number}: session {session.session_id} is about "
                f"{session.version} and is filed under {version}. A session read against "
                "the wrong version is a claim about a build that was never installed"
            )
        if session.session_id in seen:
            raise ObservationError(
                f"{location}:{number}: session {session.session_id} appears twice. Two "
                "rows under one id is the state where nobody can say which capture the "
                "counts came from"
            )
        seen.add(session.session_id)
        out.append(session)
    return tuple(out)


def append(
    session: ObservationSession,
    *,
    root: Path | str = ".",
    path: Path | str | None = None,
) -> Path:
    """Append one session. Append-only, and atomic.

    The existing store is **read first**, so a malformed one refuses instead of
    being overwritten by a writer that never looked at it, and a duplicate
    `session_id` refuses rather than making the counts ambiguous.

    Written to a temporary file in the same directory and renamed, following
    `submission.journal` and `manifest_patch.write_manifest_atomically`. A plain
    `open(…, "a")` that dies mid-line leaves a truncated last row, and this
    store's readers refuse a truncated row **permanently** — the operator would
    have to work out for themselves that the fix is to edit a file nothing told
    them about. `os.replace` within one directory is atomic, so the store is
    either every session before this one or every session including it.

    **The writer refuses a session that saw something and cannot say under what
    configuration.** The constructor cannot hold that rule: it is the one shape
    `manifest/observations/441.jsonl` is already in, and the reader has to be able
    to give that row back. So the record type represents history it is no longer
    allowed to make — new evidence states its configuration or is not written.

    **And it refuses a session with no block count at all.** :func:`parse` always
    produces one, so a `None` arriving here was hand-built; the store's readers
    compare an arm's blocks against a baseline's, and a row that never counted
    would make a comparison unanswerable for a reason nobody could see from the
    file. Refusing at the write says the fix — record from a capture — at the
    moment it is cheap. The thirteen rows already committed keep their absence and
    keep reading, exactly as the unstated toggle state does.

    **And it refuses a session that does not name its walk.** Same shape, third
    time — but note what is different about this one: the walk cannot be recovered
    from the capture the way the toggle state and the block count both were. So
    there is no regeneration available for a row that arrives without it, only a
    person remembering, and the refusal is at the write because the write is the
    last moment the person is still in the room. The rows committed before
    2026-08-11 — 439's twelve, and the one 441 row — keep their absence, keep
    reading, and are named in every report; refusing here is what keeps that set
    closed rather than letting it grow one silent row at a time.
    """

    location = Path(path) if path is not None else store_path(session.version, root)
    # `not session.vacuous`, which is `total > 0` and no constant — the same
    # derived threshold the module docstring insists on. `total > 1` would read as
    # maintenance and would silently admit a one-request session that says nothing
    # about its configuration.
    if session.toggles is None and not session.vacuous:
        raise ObservationError(
            f"session {session.session_id} observed {session.total} request(s) and states "
            "no toggle state. Every count it holds is unreadable: a zero under a block we "
            "set is caused by us, and nothing here says whether one was set. Record it "
            "from a capture that carries the build's own "
            f"`{TOGGLE_DIRECTIVE}` line — there is deliberately no way to supply one by "
            "hand"
        )
    if session.blocks is None:
        raise ObservationError(
            f"session {session.session_id} states no block count. `parse` counts the "
            f"`{BLOCK_MESSAGE}` headers in every capture it reads, so a session without "
            "one was not read from a capture. A state's blocks are only readable against "
            "a baseline that counted its own, and an uncounted zero cannot serve as that "
            "control"
        )
    if session.walk is None:
        raise ObservationError(
            f"session {session.session_id} names no walk. Pass --walk with a short, "
            "stable name for the driving protocol you ran — `three-round-v2`, not what "
            "came out of it. Two sessions are only comparable if they did the same "
            "thing, and on 2026-08-11 the walk went from one pass to three rounds and "
            "took the 440 baseline from 11-16 observed requests to 25; pooled, that "
            "spread becomes `grouping`'s noise floor and swallows every real effect. "
            "Nothing on the phone knows which walk you ran, so this one is yours to "
            "state and yours to get right"
        )
    existing = read(session.version, root, path=location)
    if any(item.session_id == session.session_id for item in existing):
        raise ObservationError(
            f"session {session.session_id} is already recorded in {location}. A second "
            "capture is a new session with its own id; re-filing one under an existing "
            "id would silently replace a measurement"
        )

    body = "".join(
        json.dumps(item.to_dict(), sort_keys=True) + "\n"
        for item in (*existing, session)
    )
    location.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=location.parent,
        prefix=location.name + ".",
        suffix=".tmp",
        delete=False,
    )
    scratch = Path(handle.name)
    try:
        try:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.replace(scratch, location)
    except BaseException:
        scratch.unlink(missing_ok=True)
        raise
    return location


def evidential(sessions: Iterable[ObservationSession]) -> tuple[ObservationSession, ...]:
    """The sessions that observed something, and are therefore evidence.

    The whole non-vacuity control, in one line and with no constant in it.
    """

    return tuple(item for item in sessions if not item.vacuous)


def stated(sessions: Iterable[ObservationSession]) -> tuple[ObservationSession, ...]:
    """The sessions that say which blocks were active while they were measured.

    The second control, and it is the same shape as :func:`evidential`: a filter
    derived from the row's own content, with no constant in it. A session that
    does not state its configuration is not evidence *about a configuration*,
    which is the only kind of evidence this module produces.
    """

    return tuple(item for item in sessions if item.toggles is not None)


def walked(sessions: Iterable[ObservationSession]) -> tuple[ObservationSession, ...]:
    """The sessions that say which walk produced them.

    The third control, the same shape as the two above: a filter over the row's own
    content with no constant in it. A session that does not name its protocol is
    still evidence about what the app asked for — that is the difference from
    :func:`stated`, where an unknown configuration makes the counts themselves
    unreadable — but it is not evidence in a *comparison*, because nothing says
    whether the other side of the comparison did the same thing.
    """

    return tuple(item for item in sessions if item.walk is not None)


def states(
    version: str, root: Path | str = ".", *, path: Path | str | None = None
) -> tuple[ToggleState, ...]:
    """The distinct toggle states that `version` has evidence under, sorted.

    The discovery half of the required argument on :func:`never_observed`: a
    caller cannot name a state it has no way to learn. Deliberately **not** a
    refusal when empty — this enumerates, it does not answer, and `()` here says
    "there is nothing you can ask about", which is not a claim about any path.
    The refusal belongs where the question is asked, with a message that can name
    which of the four reasons applies.
    """

    usable = stated(evidential(read(version, root, path=path)))
    return tuple(sorted({item.toggles for item in usable}, key=lambda item: item.text))


def walks(
    version: str, root: Path | str = ".", *, path: Path | str | None = None
) -> tuple[str, ...]:
    """The distinct walks `version` has evidence under, sorted.

    The discovery half of the required argument on `grouping.classify`, and the
    same reasoning as :func:`states`: a caller cannot name a walk it has no way to
    learn. Empty is not a refusal — this enumerates, it does not answer — and it is
    what a store recorded before walks were named says: nothing you can ask for.
    """

    usable = walked(evidential(read(version, root, path=path)))
    return tuple(sorted({item.walk or "" for item in usable}))


#: How many sessions each side of a split needs before :func:`walk_dispute` will
#: call it one. **Structural, not a magnitude.** A group's "own range" is
#: `max - min`, so with two members it is a single difference, and for a scripted
#: walk it comes out 0 or 1 second — after which any honest five-second variation
#: reads as two protocols.
#:
#: It was two, and an adversarial pass measured what that cost on the corpus this
#: repository actually holds: **66 of the 495 four-session subsets of 439's twelve
#: sessions were refused**, every one of them a single walk on a single build,
#: rising to 145 of 924 at six sessions. Among the refused was the smallest corpus
#: that yields a finding at all — a baseline pair and one arm pair. A check that
#: refuses honest data at that rate is a check somebody turns off, which is the
#: whole lesson of the leak scan that flagged every fixture on legitimate content.
#: At three a side, **no subset of either committed corpus is refused, at any
#: size**, and contamination is still caught from three sessions of the second
#: walk onwards.
#:
#: Three is the smallest group whose range is the larger of two differences rather
#: than the only one there is; the measurement above is why it is not two, and
#: four was rejected because it stops catching a 3+3 split.
#:
#: `derive-the-threshold-never-declare-it` is the standing objection to a constant
#: here and it is worth saying why this one is not that. That lesson is about a
#: **magnitude** — `NOISE = 2` requests, `60` seconds — which encodes a judgement
#: about sufficiency, moves with the data, and can be tuned until the corpus in
#: front of you passes. This is a **cardinality**: how many samples a range needs
#: before it is a range. It does not scale with anything, no value of it can make a
#: particular corpus acceptable, and what it changes is which corpora are
#: *checkable* rather than which are *right*. The magnitude in this check is still
#: derived and still has no name. What it costs is stated
#: in :func:`walk_dispute`: two mislabelled sessions among many are now invisible
#: where they used to be caught. That is the safe direction to be wrong in — the
#: `--walk` field carries the residual either way, and it is cheaper to miss a
#: contamination than to teach an operator to ignore this.
_MIN_PER_SIDE = 3

#: How much longer the slower group must run, as a fraction of the faster one,
#: before a split can be two protocols. **This one is a magnitude and there is no
#: honest way to pretend otherwise.**
#:
#: Why it cannot be derived, stated as the argument rather than as an apology. The
#: rule above scales a gap against the groups' own ranges, and that works until a
#: group has no range: a scripted walk with fixed sleeps produces near-identical
#: spans, which is the *usual* case and not an edge one. The twelve `three-round-v2`
#: sessions committed for 440 read 271, 271, 271 and then 273 nine times — one
#: script, one build, one sitting — and the range of each side of that split is
#: zero, so a two-second difference over a 271-second walk was "infinitely sharper
#: than the variation" and `grouping` refused the whole corpus.
#:
#: No function of the *shape* can fix that, and this is provable rather than a
#: guess: `{271 x 3, 273 x 9}` and `{271 x 3, 543 x 9}` have the same cardinalities,
#: the same ordering and the same group ranges — zero — and differ only in how big
#: the gap is next to the spans. Anything reading ranks, counts or ratios of ranges
#: gives both the same answer. Only size separates them, and a size needs a scale
#: from outside the shape. Given that, a **dimensionless fraction** is the best
#: available: it costs nothing when the walk gets longer or shorter, and
#: `test_the_verdict_is_the_same_at_ten_times_the_scale` holds it to that.
#:
#: Where 5% comes from, and — read this before trusting it — **which of the
#: numbers bracketing it are measurements and which are constructions.**
#:
#: * **Lower end, 0.74%, constructed from a withdrawn measurement.** Twelve
#:   `three-round-v2` sessions were walked on 440 on 2026-08-11 and read 271, 271,
#:   271 then 273 nine times; the whole corpus was discarded the same day for an
#:   unrelated navigation fault (a `content-desc` selector matched a feed node, so
#:   half the sessions never reached the surface their `surface` field named). The
#:   *spans* were never in doubt — the fault was about where the taps landed, not
#:   about how long the script ran — but the store is gone, so this is carried in
#:   `tests/test_observation.py` as the named synthetic `THREE_ROUND_JITTER` rather
#:   than read from anything committed.
#: * **Upper end, 33%, wholly constructed.** Three rounds against four, 271s to
#:   361s: the smallest protocol change anyone would make, and the binding limit.
#:   Nobody has walked a four-round protocol. It is arithmetic on the number above.
#: * **What *is* real is the precision side**, and it is the half that decides
#:   whether this gets switched off. 439's twelve one-pass sessions are committed
#:   with their captures, they span 122-153s with a genuine 31-second internal
#:   spread, and neither they nor any subset of them may dispute. That corpus also
#:   supplies the *faster group* of the contamination anchor, which is the hard half
#:   — a rule that only worked on tight groups would pass a tidy fixture and be
#:   useless here.
#:
#: 0.74% to 33% is a factor of forty-five, and 5% is its geometric middle: 6.7x
#: above the jitter and 6.7x below the tightest contamination. It is not fitted to
#: either end. The protection against a silent retune is not that the number was
#: derived — it was not — but that **both ends are pinned as tests**: raising it
#: fails `test_a_shorter_protocol_is_still_caught`, lowering it fails
#: `test_the_jitter_corpus_is_not_disputed`. A magnitude bracketed on both sides
#: cannot be moved in a diff that looks like maintenance, which is what
#: `derive-the-threshold-never-declare-it` is actually protecting against.
#:
#: **A bracket made of two constructions is weaker than one made of two
#: measurements, and this is currently the former.** Both ends say what a walk
#: *would* do rather than what one *did*, so a systematic error in that reasoning
#: is invisible to them. Re-measuring a three-round corpus replaces the lower end
#: with evidence and is worth doing for that reason alone.
_MIN_SEPARATION = 0.05


def _cuts(count: int) -> range:
    """Every split of `count` sorted spans with :data:`_MIN_PER_SIDE` each side.

    The single owner of the minimum. It used to be stated twice — an early return
    in :func:`walk_dispute` and the loop bound beneath it — and the early return
    was dead, because the loop is empty for exactly the inputs it caught. A line
    that reads like a guard and is not one is how the guard beside it comes to be
    deleted, so there is now one expression and everything asks it.
    """

    return range(_MIN_PER_SIDE, count - _MIN_PER_SIDE + 1)


def _too_few(measured: Sequence[Any]) -> bool:
    """Is there too little here for a split to mean anything?

    One owner for the question, because :func:`walk_dispute` and its own positive
    control both ask it. Two copies of it desynchronised is the control reporting
    "fully evidenced" over a corpus the check structurally cannot read — which is
    exactly the failure the control exists to prevent, arriving through the
    control. Asks :func:`_cuts`, so "can this fire?" and "where could it fire?"
    cannot disagree.
    """

    return not _cuts(len(measured))


def _rows(sessions: Sequence[ObservationSession], caller: str) -> Sequence[ObservationSession]:
    """Refuse a one-shot iterator, because these two functions come in a pair.

    :func:`walk_dispute` and :func:`walk_evidence` are asked about the *same*
    sessions — one says what is wrong, the other says whether anything could have
    been. A generator handed to both is drained by the first, and the second then
    answers from nothing: `walk_evidence` returns `""`, which is its way of saying
    "this could have fired and found nothing", on a corpus it never saw.

    Materialising inside either function does not fix that and reads as though it
    does: `tuple(sessions)` consumes the caller's generator exactly as iterating it
    would. The hazard lives at the call site, so it is refused at the boundary —
    the only place a fix can be real. Everything in this module already passes a
    tuple or a list.
    """

    if iter(sessions) is sessions:
        raise ObservationError(
            f"{caller} was given a one-shot iterator. It and its companion are asked "
            "about the same sessions, and the first to read a generator empties it — "
            "after which the second reports an empty corpus as one it checked. Pass a "
            "list or a tuple"
        )
    return sessions


def walk_dispute(sessions: Sequence[ObservationSession]) -> str:
    """Why these sessions' spans contradict the walk they claim, or `""`.

    The walk is typed and the span is measured, so this is the one place the typed
    value can be checked against evidence. Do the spans **split into two groups
    that are both sharper than the corpus's own variation and larger than a
    fraction of the walk**? Sorted, at every cut with :data:`_MIN_PER_SIDE`
    sessions on each side::

        gap    = the smallest span above the cut - the largest below it
        widest = the wider of the two groups' own ranges
        floor  = _MIN_SEPARATION x the largest span below the cut

    and a dispute is `gap > widest and gap > floor`.

    **The first term is derived and does all the work whenever there is variation
    to derive it from** — the corpus supplies its own scale, exactly as
    `grouping.noise_floors` does, and a claim fails only when the evidence
    separates more sharply than it varies. **The second is a magnitude**, and
    :data:`_MIN_SEPARATION` says so plainly, gives the two committed measurements
    that bracket it, and shows why no function of the shape can replace it. It
    exists because the first term collapses to zero on exactly the corpus this is
    most often asked about: a scripted walk with fixed sleeps produces near-
    identical spans, and a zero scale makes any difference at all look infinitely
    sharp.

    What it has been checked against, separating evidence from construction:

    * **Real.** 439's twelve one-pass sessions are committed with their captures.
      They span 122-153s, no subset of them disputes at any size, and their genuine
      31-second internal spread makes them the hard half of the contamination case
      as well — a rule that only worked on tight groups would pass a tidy fixture
      and refuse this.
    * **Constructed.** Both ends of :data:`_MIN_SEPARATION` are now synthetics, and
      that constant's comment says which and why. The one that mattered — twelve
      spans reading 271, 271, 271, 273 x 9, where each side of the split is zero
      seconds wide — was measured on 440 on 2026-08-11 and withdrawn the same day
      with the rest of its corpus for an unrelated navigation fault. Under the
      derived term alone it disputed on a two-second difference across a
      271-second walk, and `grouping report --version 440 --walk three-round-v2`
      returned nothing at all. That is what the floor exists for, and it is pinned
      as `THREE_ROUND_JITTER` rather than dropped.

    **Its reach, so nobody reads it as more than it is.**

    * It needs :data:`_MIN_PER_SIDE` on each side, so fewer than six timed sessions
      cannot be checked at all and up to two mislabelled ones among many stay
      invisible. :func:`walk_evidence` reports the first; that constant's own
      comment gives the measurement behind the number and what the alternative cost.
    * A capture with no timestamps has no span and is not considered. **Every row
      committed today is in exactly that shape** — 439's twelve predate the field —
      so the contamination this most wants to see, old sessions and new ones
      sharing one name, is invisible to it until those rows are re-recorded. It is
      `grouping`'s refusal of an unstated walk, not this, that stands between them
      and an answer.
    * It compares, so a corpus in which every session is mislabelled the same way is
      silent. That is the residual the `--walk` flag carries, stated in the module
      docstring rather than papered over.
    * Two walks less than :data:`_MIN_SEPARATION` apart are not distinguished at
      all. That is the price of the floor and it is the right way round: a protocol
      change worth catching adds or removes rounds, and the smallest one this
      corpus can show — three rounds against four, 271s to 361s — is 33%, six times
      the floor. A change smaller than 5% of the walk is inside the jitter of
      running it twice, and the field is what carries it.

    Returns a sentence, not a bool. Two callers need to say what is wrong and both
    would otherwise write their own wording of it, which is how a human banner and
    a machine field come to disagree.
    """

    measured = sorted(
        (item.span_seconds, item.session_id)
        for item in _rows(sessions, "walk_dispute")
        if item.span_seconds is not None
    )
    spans = [span for span, _ in measured]
    for cut in _cuts(len(spans)):
        gap = spans[cut] - spans[cut - 1]
        widest = max(spans[cut - 1] - spans[0], spans[-1] - spans[cut])
        # Two scales, and a split must clear both. The first is derived and does all
        # the work whenever the corpus has variation to derive it from. The second
        # is a floor for when it has none — see :data:`_MIN_SEPARATION`, which
        # argues at length why that case cannot be reached by any function of the
        # shape, and what stops the number being tuned.
        floor = _MIN_SEPARATION * spans[cut - 1]
        if gap > widest and gap > floor:
            below = ", ".join(f"{name} {span}s" for span, name in measured[:cut])
            above = ", ".join(f"{name} {span}s" for span, name in measured[cut:])
            return (
                f"{len(measured)} session(s) claim one walk and their capture spans "
                f"fall into two groups {gap}s apart — wider than the {widest}s either "
                f"group spans on its own, and more than the {floor:.0f}s that is "
                f"{_MIN_SEPARATION:.0%} of the faster group: [{below}] against "
                f"[{above}]. A walk is a script with sleeps in it, so its span barely "
                "moves between two runs while the request counts move a lot; a split "
                "this sharp and this large is two protocols wearing one name. "
                "Whatever difference a comparison found between two states here would "
                "include the difference between the two walks"
            )
    return ""


def walk_evidence(sessions: Sequence[ObservationSession]) -> str:
    """Why :func:`walk_dispute` could not have fired on these, or `""` if it could.

    The positive control, as a value rather than as an intention. A check that
    silently cannot fire is the failure this project keeps repeating — an
    assertion that still passes with the behaviour it names deleted — so the two
    ways it goes inert are reported wherever its verdict is, and it asks
    :func:`_too_few` rather than keeping its own copy of the rule.

    Reads its input twice, which is why :func:`_rows` refuses an iterator here as
    well: drained between the two comprehensions, `untimed` comes out empty and a
    corpus with sessions this cannot see reports itself as fully evidenced.
    """

    rows = _rows(sessions, "walk_evidence")
    timed = [item for item in rows if item.span_seconds is not None]
    untimed = sorted(item.session_id for item in rows if item.span_seconds is None)
    if _too_few(timed):
        return (
            f"only {len(timed)} of {len(rows)} session(s) carry a capture span, and "
            f"the walk check needs {_MIN_PER_SIDE} on each side of a split, so it "
            "could not have contradicted the claimed walk here"
            + (f" (no span: {', '.join(untimed)})" if untimed else "")
        )
    if untimed:
        return (
            f"{len(untimed)} session(s) carry no capture span and are outside the "
            "walk check: " + ", ".join(untimed)
        )
    return ""


def never_observed(
    version: str,
    root: Path | str = ".",
    *,
    toggles: ToggleState,
    path: Path | str | None = None,
) -> tuple[str, ...]:
    """Literals watched under `toggles`, in a non-vacuous session, and never seen.

    Three halves now, and each excludes a silence that is about the measurement
    rather than about the app. *Watched* excludes a path no build was looking for.
    *Non-vacuous* excludes a session that saw nothing at all — see the module
    docstring for the three explanations that have nothing to do with the app's
    behaviour. *Measured under this exact state* excludes a zero that our own
    blocks caused: `/feed/injected_reels_media/` was observed 0 times with the
    blocks on and 3 times with them off, on one build and one walk.

    `toggles` is **required and names the experiment**, and the answer is over
    the sessions measured under exactly that state. An all-off exploration
    session and a one-toggle-on isolation session filed under one version answer
    different questions, and a union of them is about no configuration at all.
    The argument can only *select* — a state nobody measured refuses instead of
    answering — so it is not the operator-supplies-the-safety-property mistake
    that `retirement`'s docstring records; nothing here lets an operator say what
    a capture was.

    **Refuses when nothing can answer.** NOT `()`. This function's entire job is
    to name paths whose absence was measured, and an empty tuple is the same
    answer it gives when every watched path was seen — so "we measured nothing"
    would arrive spelled "nothing is wrong". That is absence reported as a pass,
    which is the one failure this project refuses everywhere else;
    `rulings.unenforced_endpoints` refuses in the same place, for the same reason.

    Four refusals, because four different things are wrong and each has its own
    fix: nothing was recorded, everything recorded saw nothing, everything that
    saw something predates builds stating their own configuration, and nothing
    was measured under the state you asked about.
    """

    if not isinstance(toggles, ToggleState):
        raise ObservationError(
            f"toggles must be a ToggleState, got {type(toggles).__name__}. Build one "
            "with ToggleState.parse('disable_feed=1 disable_explore=0 ...') or "
            "ToggleState.of({...}); `states(version, root)` lists the ones on record"
        )
    location = Path(path) if path is not None else store_path(version, root)
    sessions = read(version, root, path=location)
    unanswerable = _unanswerable(version, location, sessions)
    if unanswerable:
        raise ObservationError(unanswerable)
    configured = stated(evidential(sessions))
    matching = [item for item in configured if item.toggles == toggles]
    if not matching:
        raise ObservationError(
            f"no session for {version} was measured with {toggles.text!r}. The states on "
            "record are: "
            + "; ".join(item.text for item in
                        sorted({item.toggles for item in configured},
                               key=lambda item: item.text))
            + ". Answering from a session measured under another configuration would be "
            "answering a different question"
        )

    watched: set[str] = set()
    seen: set[str] = set()
    for item in matching:
        watched.update(item.watched)
        seen.update(item.counts)
    return tuple(sorted(watched - seen))


def blocked_endpoints(root: Path | str = ".") -> tuple[str, ...]:
    """Every path literal the generated guard tests, from the manifest.

    The *manifest's* rules and not the app source's `throwIfBlocked`, though
    `rulings.guarded_endpoints` reads the latter and would answer a very similar
    question. Two reasons. The manifest is committed and always present, while a
    decoded source tree is not — and a question about what this repository
    currently blocks should not become unanswerable on a machine that has not
    decoded an APK. And the manifest literal is the *same string* the observe
    build watches: `guards` renders both from `url_block_rules`, so the join
    below needs no spelling rule and cannot acquire one that is subtly wrong.
    A leading slash going unnormalised is how an entire grouping went invisible
    on 440, and the fix here is to have nothing to normalise.

    Refuses through `ObservationError` rather than leaking `GuardError`: this
    module has one refusal channel and its callers catch one exception.
    """

    from .guards import GuardError, rules_from_manifest  # noqa: PLC0415

    manifest = Path(root) / "manifest" / "hooks.json"
    try:
        rules = rules_from_manifest(manifest)
    except (GuardError, OSError, json.JSONDecodeError) as error:
        raise ObservationError(f"{manifest}: {error}") from error
    return tuple(sorted({literal.text for rule in rules for literal in rule.literals}))


def blocked_and_never_observed(
    version: str,
    root: Path | str = ".",
    *,
    toggles: ToggleState,
    path: Path | str | None = None,
) -> tuple[str, ...]:
    """Of the endpoints this repository blocks, which `version` never requested.

    The one question worth carrying over from the deleted `reconsider` module,
    whose `block_never_observed` rule asked it in order to propose *withdrawing*
    a block. Nothing withdraws anything now — the project decides late, on
    measurement, rather than deciding early and correcting afterwards — so this
    is a measurement and not a proposal. A path that is blocked and never once
    requested is a fact about this phone and these surfaces; what to do about it
    is a human's business.

    **Refuses whenever `never_observed` refuses, and deliberately does not
    soften it.** An empty tuple here is the honest answer to "every blocked path
    was seen at least once", so returning one because nothing was measured would
    report "we know nothing" in the words of "nothing is wrong". That is the
    absence-as-a-pass this module exists to refuse; see the docstring above.

    **Bounded by the watch list as well as by the surfaces.** A blocked endpoint
    no session was watching cannot appear here, and its silence means nothing —
    `summary` warns by name when the manifest blocks something the evidence never
    watched, because otherwise this answer is quietly incomplete.

    **And bounded by `toggles`, which is where this question is at its most
    circular.** Asked under a state in which the blocked endpoint's own toggle is
    on, "we block it and never saw it asked for" is very nearly a tautology: the
    block is upstream of the request for `/feed/injected_reels_media/`, and
    `replaceReelsEndpoint` removes the Reels paths from the URL before the
    observe pass can see them at all. The state is required here for that reason
    and not merely by inheritance; `summary` says so, per state, in both forms.
    """

    unseen = set(never_observed(version, root, toggles=toggles, path=path))
    return tuple(literal for literal in blocked_endpoints(root) if literal in unseen)


# ------------------------------------------------------------------ reporting


def _unanswerable(version: str, location: Path, sessions: Sequence[ObservationSession]) -> str:
    """Why nothing can be answered for `version`, or `""` when something can.

    One producer for the refusal and for the report's banner. `never_observed`
    raises this string and `summary` prints it, because a refusal a human reads in
    one wording and a script reads in another is the defect this module already
    carries a warning about: the machine view went quiet while the human one spoke.

    Three reasons, in the order they stop being fixable by taking another capture.
    """

    usable = evidential(sessions)
    if not sessions:
        return (
            f"there is no observation evidence for {version} ({location} holds no "
            "session). Nothing can be said about what the app never requested until "
            "something recorded what it did"
        )
    if not usable:
        return (
            f"all {len(sessions)} observation session(s) for {version} are vacuous: not "
            "one of them observed a single watched literal. A session that saw nothing "
            "is equally well explained by a build that was not observing, an empty "
            "capture, or an app that never ran — so it is evidence about no path. "
            "Returning an empty tuple here would be the same answer this gives when "
            "every watched path WAS seen"
        )
    if not stated(usable):
        return (
            f"none of the {len(usable)} evidential session(s) for {version} states which "
            "blocks were active: "
            + ", ".join(sorted(item.session_id for item in usable))
            + f". They predate the build reporting its own {TOGGLE_DIRECTIVE} line. A "
            "zero measured under an unknown configuration cannot be told apart from one "
            "our own blocks caused, and no configuration can be assumed for them now — "
            "that would be the operator-supplied state this module refuses"
        )
    return ""


def summary(version: str, root: Path | str = ".") -> dict[str, Any]:
    """Everything a report says, in one shape, so both output forms read it.

    One producer for both views. The human banner and the machine field going out
    of step is a defect this project has shipped — the JSON a script gates on was
    missing the warning the human form printed.

    **Answered per toggle state, and there is no whole-version answer.** There
    used to be a `never_observed` field here, over every evidential session at
    once; it is gone rather than kept alongside, because a blended number that
    looks like an answer is worse than a missing key. A caller reading the old
    field now fails loudly instead of reading a union of two experiments.
    """

    location = store_path(version, root)
    sessions = read(version, root)
    usable = evidential(sessions)
    vacuous = [item for item in sessions if item.vacuous]
    configured = stated(usable)
    unstated = [item for item in usable if item.toggles is None]
    unwalked = [item for item in usable if item.walk is None]

    warnings: list[str] = []
    unanswerable = _unanswerable(version, location, sessions)
    if unanswerable:
        warnings.append(unanswerable)
    if usable and vacuous:
        warnings.append(
            f"{len(vacuous)} of {len(sessions)} session(s) are vacuous — they observed "
            "nothing and are excluded: "
            + ", ".join(sorted(item.session_id for item in vacuous))
        )
    if configured and unstated:
        # Only when something *can* be answered. When nothing states a state the
        # refusal above already names every one of them, and saying it twice in
        # two wordings is how two spellings of one fact come to disagree.
        warnings.append(
            f"{len(unstated)} evidential session(s) state no toggle state and are "
            "excluded from every answer below: "
            + ", ".join(sorted(item.session_id for item in unstated))
            + f". A row without a {TOGGLE_DIRECTIVE} line was measured under a "
            "configuration nobody wrote down"
        )
    if unwalked:
        # Not a refusal here, and that is the asymmetry with `grouping`. This
        # report's answers are *negative* — watched, walked for, never seen — and
        # pooling a second walk into one can only give a path more chances to be
        # seen, so it retracts such a claim and cannot invent one. A differential
        # is the opposite way round, which is why `grouping` refuses.
        warnings.append(
            f"{len(unwalked)} evidential session(s) name no walk: "
            + ", ".join(sorted(item.session_id for item in unwalked))
            + ". They were recorded before a session said which driving protocol "
            "produced it. Nothing below is wrong because of that — a negative claim "
            "only gets safer as more walks are pooled into it — but `grouping` will "
            "not compare states across them, and no walk can be filled in now "
            "without somebody remembering one"
        )
    # Asked once per claimed walk, never over the pool. Pooling two walks and then
    # asking whether the spans split would find the split every time and call the
    # honest corpus a liar — the question is only ever "do the sessions claiming
    # *this* protocol agree that they ran it?".
    for name in sorted({item.walk for item in usable if item.walk is not None}):
        claiming = [item for item in usable if item.walk == name]
        dispute = walk_dispute(claiming)
        if dispute:
            warnings.append(f"the walk {name!r} is contradicted by its own captures: "
                            + dispute)
        else:
            # The positive control, in the report that has no other refusal to
            # hang it on. Saying nothing here would be claiming a check that never
            # ran — and on this repository's own committed stores it never can,
            # because not one row carries a span.
            inert = walk_evidence(claiming)
            if inert:
                warnings.append(
                    f"the walk {name!r} is claimed and only partly evidenced: " + inert
                    + ". The name was typed by whoever ran the session; the capture "
                    "span is the only thing that can contradict it"
                )

    # Read once, for every state. It fails for reasons `never_observed` cannot —
    # an unreadable manifest, or one declaring no block at all — and a reader told
    # "all sessions are vacuous" when the real fault is a missing `url_block_rules`
    # would repair the wrong thing. Reported as a warning as well as a field, so it
    # is still audible when there is no state to hang it on.
    try:
        blocked: list[str] = list(blocked_endpoints(root))
        blocked_refusal = ""
    except ObservationError as error:
        blocked = []
        blocked_refusal = str(error)
    if blocked_refusal:
        warnings.append(
            "the blocked-and-never-observed question cannot be answered for any state: "
            + blocked_refusal
        )

    entries: list[dict[str, Any]] = []
    for state in states(version, root):
        group = [item for item in configured if item.toggles == state]
        unseen = list(never_observed(version, root, toggles=state))
        totals: dict[str, int] = {}
        for item in group:
            for literal, count in item.counts.items():
                totals[literal] = totals.get(literal, 0) + count
        # Computed once and used twice. Two `sorted({...})` expressions — one for
        # the warning, one for the field — are two places that can disagree, and a
        # page whose banner says `feed_tab, reels_tab` while its bound says
        # `feed_tab` is worse than either alone.
        surfaces = sorted({item.surface for item in group})
        # The same rule as `surfaces`, and computed the same way for the same
        # reason: named once, placed in the warning and in the field.
        group_walks = sorted({item.walk or UNWALKED for item in group})
        warnings.append(
            f"{state.text}: never-observed is bounded by the surfaces walked: "
            + ", ".join(surfaces)
            + ", on walk(s) " + ", ".join(group_walks)
            + ". A path only the Reels player requests is not observed by a session "
            "that stayed on the feed, and a path only a third round reaches is not "
            "observed by a walk that made one pass"
        )
        # Produced once and placed twice: in the state's own entry, where `render`
        # prints it immediately above the list it is about, and in `warnings`,
        # which is what a script reads. The most dangerous line in this report is
        # a never-observed literal under a blocking state, and a caution twenty
        # lines below it in a WARNINGS block is a caution the reader has already
        # passed.
        caution = ""
        if state.blocking:
            caution = (
                f"{state.text}: measured with {', '.join(state.on)} ON, so a zero here "
                "can be caused by our own blocks rather than by the app. Blocking "
                "/feed/timeline/ leaves no timeline response for /feed/injected_reels_media/ "
                "to be injected into, and disable_reels blanks the Reels endpoint before "
                "the URL is built, which is upstream of the observe pass. Only a session "
                "with every toggle off answers 'would the app ask for this?'"
            )
            warnings.append(caution)
        builds = sorted({item.build_sha256 for item in group})
        if len(builds) > 1:
            # A toggle key is not the experiment: what `disable_feed` blocks is
            # decided by the manifest the build was rendered from, so two builds
            # of one version can report the same state and block different
            # literals. Not a refusal — the usual case is a rebuild that changed
            # nothing here — but the reader has to be told the group spans two.
            warnings.append(
                f"{state.text}: this answer unions sessions from {len(builds)} builds "
                + ", ".join(item[:12] for item in builds)
                + ". A toggle name is not a rule: two builds can report the same state "
                "and block different literals"
            )
        watched: set[str] = set()
        for item in group:
            watched.update(item.watched)
        # A blocked endpoint no session was watching is not evidence of anything,
        # and it is absent from the answer in exactly the way a finding is. Named,
        # because the reader's question about a short list is "is that all of them?".
        unwatched = [item for item in blocked if item not in watched]
        if unwatched:
            warnings.append(
                f"{state.text}: {len(unwatched)} blocked endpoint(s) were not in any "
                "watch list under this state, so nothing here says anything about them: "
                + ", ".join(unwatched)
            )
        entries.append({
            "toggles": state.as_dict(),
            "toggles_text": state.text,
            "toggles_on": list(state.on),
            "circular": caution,
            "session_ids": sorted(item.session_id for item in group),
            "build_sha256s": builds,
            "surfaces": surfaces,
            "walks": group_walks,
            "observed": dict(sorted(totals.items())),
            "never_observed": unseen,
            "blocked_never_observed": [item for item in blocked if item in set(unseen)],
            "blocked_never_observed_refused": blocked_refusal,
        })

    return {
        "schema_version": SCHEMA_VERSION,
        "version": version,
        "session_count": len(sessions),
        "evidential_session_count": len(usable),
        "stated_session_count": len(configured),
        "vacuous_session_ids": sorted(item.session_id for item in vacuous),
        "unstated_session_ids": sorted(item.session_id for item in unstated),
        "unwalked_session_ids": sorted(item.session_id for item in unwalked),
        "walks": sorted({item.walk for item in usable if item.walk is not None}),
        # Empty exactly when `states` is non-empty: every question this report can
        # answer is answered under one of them, and when it can answer none this
        # says which of the three reasons applies.
        "unanswerable_reason": unanswerable,
        "states": entries,
        "warnings": warnings,
    }


def render(report: Mapping[str, Any]) -> str:
    lines = [f"OBSERVATION  {report['version']}", "=" * 68, ""]
    lines.append(
        f"  {report['session_count']} session(s), "
        f"{report['evidential_session_count']} with observations, "
        f"{report['stated_session_count']} stating which blocks were active"
    )
    lines.append("")

    if report["unanswerable_reason"]:
        lines += ["  NOTHING CAN BE ANSWERED", "",
                  f"    {report['unanswerable_reason']}", ""]

    for state in report["states"]:
        lines.append(f"  TOGGLES  {state['toggles_text']}")
        lines.append(f"    sessions: {', '.join(state['session_ids'])}")
        lines.append(f"    surfaces: {', '.join(state['surfaces'])}")
        lines.append(f"    walks:    {', '.join(state['walks'])}")
        lines.append("")
        if state["circular"]:
            # Above the list, not in the WARNINGS block below it.
            lines += [f"    {state['circular']}", ""]

        if state["never_observed"]:
            # No count in the heading. It is a second spelling of the length of a
            # list printed directly below it, and this project has twice shipped a
            # count that drifted from the thing it counted.
            lines.append("    WATCHED AND NEVER OBSERVED")
            lines.append("")
            for literal in state["never_observed"]:
                lines.append(f"      {literal}")
            lines.append("")
        else:
            lines += ["    Every watched path was observed at least once.", ""]

        if state["blocked_never_observed_refused"]:
            lines += ["    BLOCKED AND NEVER OBSERVED: refused", "",
                      f"      {state['blocked_never_observed_refused']}", ""]
        elif state["blocked_never_observed"]:
            lines.append("    BLOCKED AND NEVER OBSERVED")
            lines.append("")
            for literal in state["blocked_never_observed"]:
                lines.append(f"      {literal}")
            lines.append("")
        else:
            lines += ["    Every blocked path this manifest declares was observed.", ""]

        if state["observed"]:
            lines.append("    OBSERVED")
            lines.append("")
            width = max(len(literal) for literal in state["observed"])
            for literal, count in sorted(
                state["observed"].items(), key=lambda pair: (-pair[1], pair[0])
            ):
                lines.append(f"      {literal.ljust(width)}  {count}")
            lines.append("")

    if report["warnings"]:
        lines += ["  WARNINGS", ""]
        for warning in report["warnings"]:
            lines.append(f"    {warning}")
        lines.append("")

    lines.append(
        "  This measures; it does not decide. A blocked path that was never once "
        "requested is a"
    )
    lines.append(
        "  fact about this phone and these surfaces — what to do about it is a "
        "human's to decide."
    )
    return "\n".join(lines)


# ------------------------------------------------------------------------ CLI


def _watch_list(args: argparse.Namespace) -> tuple[str, ...]:
    watched: list[str] = list(args.watched or ())
    if args.watched_from:
        text = Path(args.watched_from).read_text(encoding="utf-8")
        watched += [line.strip() for line in text.splitlines() if line.strip()]
    ordered: list[str] = []
    for literal in watched:
        if literal not in ordered:
            ordered.append(literal)
    if not ordered:
        raise ObservationError(
            "no watch list given. Pass --watched or --watched-from: without it the "
            "session records counts against a population nobody stated, and a zero "
            "becomes unreadable"
        )
    return tuple(ordered)


def _record_parser(sub: Any) -> argparse.ArgumentParser:
    """Every option `record` accepts — one definition, so a test can ask.

    Not inlined into :func:`main`, because the property worth defending is about
    what this command *offers*: nothing here may carry a toggle state. A test
    that rebuilt the option list to check that would be checking its own copy,
    and a test that named `--toggles` would be a denylist of one — which is the
    shape `retirement` replaced with an allowlist after `agent` was denied and
    `claude`, `bot` and `ci` sailed through.

    `--walk` sits in that allowlist and is the one flag here that states something
    about the measurement rather than about the artefacts. It is there because the
    walk is genuinely not on the device: no build can report a property of the
    script that drove it. That is an argument for *this* flag and not a hole in the
    rule — a `--toggles` would have a different answer available and would be
    choosing the worse one.
    """

    record = sub.add_parser("record", help="turn one logcat capture into a session")
    record.add_argument("--version", required=True)
    record.add_argument("--build-sha256", required=True, help="the APK that was installed")
    record.add_argument(
        "--recorded-at", required=True, help="ISO 8601. Supplied, never read from the clock"
    )
    record.add_argument("--session-id", required=True)
    record.add_argument("--surface", required=True, help="which surface was walked, e.g. feed_tab")
    record.add_argument(
        "--walk",
        required=True,
        help="the driving protocol, e.g. three-round-v2. Lowercase, stable across "
        "sessions and versions, and about the protocol rather than what came out of "
        "it. Nothing on the phone knows this, so it is yours to state",
    )
    record.add_argument(
        "--watched", action="append", default=[], help="a watched literal; repeatable"
    )
    record.add_argument("--watched-from", type=Path, help="a file of watched literals, one per line")
    record.add_argument("--capture", type=Path, help="a logcat capture; default stdin")
    # And deliberately nothing naming a toggle, a block or a state. The toggle
    # state comes out of the capture, where the build put it; an option here would
    # let the person who ran the session state what the session measured, which is
    # the shape of safety property this project shipped and broke the next day.
    return record


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path("."))
    sub = parser.add_subparsers(dest="command", required=True)

    _record_parser(sub)

    report = sub.add_parser("report", help="what was seen at a version, and what was not")
    report.add_argument("--version", required=True)
    report.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "record":
            text = (
                Path(args.capture).read_text(encoding="utf-8")
                if args.capture
                else sys.stdin.read()
            )
            capture = parse(text)
            session = ObservationSession(
                schema_version=SCHEMA_VERSION,
                version=args.version,
                build_sha256=args.build_sha256,
                recorded_at=args.recorded_at,
                session_id=args.session_id,
                surface=args.surface,
                watched=_watch_list(args),
                toggles=capture.toggles,
                walk=args.walk,
                span_seconds=capture.span_seconds,
                blocks=capture.blocks,
                refusals=capture.refusals,
                counts=capture.counts,
                # Both read off the capture, and both were being **dropped**.
                # `parse` has produced them since 2026-08-17 and 2026-08-18, and
                # every field beside them here is passed through, so the omission
                # read as "the capture did not carry one" at every later stage:
                # a row recorded through this command said `probes: None` — the
                # value reserved for a row written before anything looked — for a
                # build that was reporting probes on every hook.
                #
                # It is the same shape as every other producer gap this project
                # has found: the parser, the record type, the store and the
                # reader were all complete and tested, and the one line that
                # moves the value from the first to the second did not exist.
                probes=capture.probes,
                per_surface=capture.per_surface,
            )
            written = append(session, root=args.root)
            print(
                f"{session.session_id}: {session.total} request(s) across "
                f"{len(session.counts)} of {len(session.watched)} watched path(s) "
                f"on {session.surface}"
            )
            if session.toggles is None:
                print(
                    f"  toggles: not stated — this capture carries no {TOGGLE_DIRECTIVE} "
                    "line, which only a capture that observed nothing can do."
                )
            else:
                print(f"  toggles: {session.toggles.text}  (as the build reported them)")
            print(
                f"  blocks:  {session.blocks.text}  (Instagram's own error events, "
                "which go missing)"
            )
            if session.refusals is None:
                # The operator has to know at the moment they file it: this
                # session cannot answer "which paths did the guard refuse", and
                # its silence is not a zero. A build older than 2026-08-13 is the
                # usual cause, and re-walking is the only repair — unlike the
                # walk, nothing here can be back-filled from the capture.
                print(
                    f"  refused: not reportable — this build never claimed "
                    f"`{REPORTS_MARK}{REPORTS_BLOCKED}`, so it could not have written a "
                    f"{BLOCKED_DIRECTIVE} line. This session says nothing about which "
                    "paths were refused, and its zero is not a measurement"
                )
            else:
                print(
                    f"  refused: {session.refusals.text}  (the guard's own count, "
                    "by the literal that matched)"
                )
            if session.span_seconds is None:
                # Said, not swallowed. The span is what a wrong `--walk` would be
                # caught by, and a capture with no timestamps is outside that check
                # — which the operator should know at the moment they file it.
                print(
                    f"  walk:    {session.walk}  (YOURS TO STATE — and this capture "
                    "carries no timestamps, so there is no span to contradict it)"
                )
            else:
                print(
                    f"  walk:    {session.walk}  over {session.span_seconds}s of "
                    "capture  (the name is yours to state; the span is measured)"
                )
            if session.toggles is not None and session.toggles.blocking:
                # The whole reason the field exists, said at the moment the number
                # is produced rather than only where it is read.
                print(
                    "  CIRCULAR: "
                    + ", ".join(session.toggles.on)
                    + " were ON, so a zero in this session can be caused by our own "
                    "blocks. Only a session with every toggle off answers 'would the "
                    "app ask for this?'."
                )
            if session.vacuous:
                # Printed on the way out, not swallowed. A vacuous session is
                # worth recording — it is the honest record of a capture that saw
                # nothing — and it is worth saying that it will never be counted.
                print(
                    "  VACUOUS: nothing at all was observed, so this session is not "
                    "evidence about any path. Check the build is the observing one and "
                    "that the capture covers the app running."
                )
            siblings = [
                item
                for item in read(args.version, args.root, path=written)
                if item.walk == session.walk
            ]
            contradiction = walk_dispute(siblings)
            if contradiction:
                # At the write, where the operator still remembers which script
                # they ran. `grouping` refuses on the same evidence later, and by
                # then the fix is archaeology.
                print(
                    "  WALK DISPUTED: " + contradiction + ". Either this session ran "
                    "a different protocol from the ones it was filed beside, or one "
                    "of them did. `grouping` will refuse to compare states across "
                    "this walk until the names match what was run."
                )
            print(f"recorded in {written}")
            print(
                "Commit it: the report reads the committed files, and an uncommitted "
                "row works here and vanishes on clone."
            )
            return 0

        report_data = summary(args.version, args.root)
        if args.json:
            print(json.dumps(report_data, indent=2, sort_keys=True))
        else:
            print(render(report_data))
        # Exit 0 whatever it finds. This is a measurement, not a gate.
        return 0
    except (ObservationError, ValueError, OSError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

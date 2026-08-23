"""Generate `throwIfBlocked` from a declaration, instead of hand-writing smali.

===============================================================================
  WHY THIS IS DETERMINISTIC AND NOT AN AGENT
===============================================================================

A url-block guard carries exactly three facts: the path text, whether to match it
by `endsWith` or `contains`, and which preference switches it. Everything around
those three facts is the same eleven instructions every time. That is a template,
not a judgement, and this project has already made this move once — hook
resolution went from agent-proposed to `by_anchor` and the agent count went to
zero and stayed there across three versions.

What an agent would add here is the ability to invent a shape nobody anticipated,
which is precisely what must NOT happen inside a method that fails live network
requests. So this module generates, and where it cannot express something it
**refuses** and says what it could not express. A guard that silently came out
wrong is a request that silently keeps flowing.

===============================================================================
  THE SHAPE, AND THE ONE THAT DID NOT FIT
===============================================================================

Every guard shipped before 2026-08-08 was *one or more paths → ONE toggle*. The
five endpoints ruled that day included `delivery/background_prefetch`, which
prefetches for the feed and for Reels both, and so belongs to two toggles at
once. The template grew an any-of form for it: the literals are tested once and
each toggle gets its own `if-nez … :cond_block`.

That is worth stating plainly, because it is the answer to "can this be generated
deterministically?" — yes, **provided the generator can refuse**. The very first
batch after the template was written contained a rule the template could not
express, and a generator without a refusal path would have had to guess.

===============================================================================
  WHY LABEL NAMES ARE NOT PART OF THE CONTRACT
===============================================================================

The hand-written method calls its labels `:cond_reels_home` and
`:cond_reels_setting`; this module calls them `:cond_rule_2` and
`:cond_rule_2_toggle`. Those are not the same text and they are the same method:
the assembler discards the names entirely and baksmali renumbers them
positionally. So equivalence is asserted over :func:`normalise`, which replaces
every label with its position, and the test that matters proves this generator
reproduces the **device-proved** seven-rule method instruction for instruction.
Comparing the raw text instead would have made a cosmetic rename look like a
behaviour change, which is how a real difference gets lost in the noise.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "GuardError",
    "OBSERVE_DESCRIPTOR",
    "OBSERVE_TAG",
    "watched_literals",
    "watch_from_manifest",
    "write_observe_class",
    "render_observe_class",
    "toggles_of",
    "Literal",
    "Rule",
    "MATCHES",
    "BLOCK_MESSAGE",
    "BLOCKED_DIRECTIVE",
    "BLOCKED_METHOD",
    "TOGGLE_DIRECTIVE",
    "REPORTS_MARK",
    "REPORTS",
    "REPORTS_LINE",
    "normalise",
    "Decision",
    "decide",
    "render_method",
    "rules_from_manifest",
    "apply_to_source",
]


class GuardError(Exception):
    """Raised rather than emitting a guard whose behaviour was guessed."""


#: How a path may be tested, and the smali each form emits. `contains` takes a
#: `CharSequence` and `endsWith` a `String`; getting that argument type wrong
#: assembles fine and throws `NoSuchMethodError` at runtime, which is why it is
#: data here rather than a formatting string at the call site.
MATCHES = {
    "endswith": ("endsWith", "Ljava/lang/String;"),
    "contains": ("contains", "Ljava/lang/CharSequence;"),
}

#: The one message every rule throws. Deliberately not per-rule: Instagram files
#: our IOException into its own `IgFunctionalErrorEvent` as
#: `NETWORK_FAILURE_REASON`, and that event names `logview_group_by`, so the
#: string very likely reaches Meta. One fixed string is a smaller fingerprint
#: than a per-rule vocabulary that would enumerate which rules a client carries.
#: Owner decision, 2026-08-08. Rule-level attribution is done with a throw-away
#: diagnostic build instead, which never ships.
BLOCK_MESSAGE = "Blocked by DFInsta setting"

#: How a measurement build says *we* refused a request, and which path it was.
#:
#: The block signal used to be `java.io.IOException: Blocked by DFInsta setting`
#: grepped out of logcat — a line that is there because **Instagram** catches our
#: exception and files it into its own error event. It cost nothing to read, so it
#: became the signal, and it under-reports: across eight sessions on two Instagram
#: versions and two walk protocols, `/discover/topical_explore` was refused seven
#: times and reported once, and six times and reported **none**, while
#: `/feed/timeline/` reported 20/20, 23/23 and 17/17 in the very same captures. The
#: loss is feature-specific and stable, so no inference over the total recovers it.
#:
#: Whether the app *requests* a path is Instagram's to say, and that is the thing
#: being measured. Whether our guard refused it is ours — and so is whether that
#: was written down. This is us writing it down.
#:
#: It carries the **literal that matched**, not the rule, because a rule may test
#: several: `/api/v1/clips/homecoming/` and `/clips/discover` are one rule under
#: `disable_reels`, and `/clips/discover` is the exact path two walk protocols
#: disagreed about. A per-rule name would have merged the contested path into its
#: neighbour.
BLOCKED_DIRECTIVE = "!blocked"
BLOCKED_METHOD = "blocked"

#: What this build's instrumentation is able to state, carried **on the toggle
#: line** — `!toggles +blocked disable_feed=1 ...`.
#:
#: Without it a capture holding no `!blocked` line is ambiguous between "nothing
#: was refused" and "this build could not have said", and every session recorded
#: before 2026-08-13 would read as the first. That is the absent-vs-empty
#: conflation, which has shipped in five modules of this project; here it would
#: turn 48 committed sessions into 48 false zeroes at once.
#:
#: On the toggle line rather than a line of its own because `state()` runs on
#: **every checked request** — 625 times in a three-round session, not once per
#: watched path — so a second line would have grown every committed capture by
#: 55% to repeat one constant. Nine bytes on a line already being written costs
#: 8%. The `logcat -c` argument that makes `state()` per-request applies to the
#: capability exactly as it applies to the toggles, so the two belong together.
#:
#: Marked with a leading `+` so it can never be read as a preference key: those
#: match `[A-Za-z_][A-Za-z0-9_]*`, so the two token shapes cannot collide and a
#: reader needs no table to tell one from the other.
REPORTS_MARK = "+"

#: Named one by one rather than as a version number, so a build states what it can
#: do rather than what generation it belongs to — a reader then needs no table
#: mapping versions to capabilities in order to know whether a zero is a zero.
REPORTS = ("blocked",)

#: The whole seed of the toggle line: the directive, then what this build can say.
TOGGLE_DIRECTIVE = "!toggles"
REPORTS_LINE = " ".join(
    [TOGGLE_DIRECTIVE] + [f"{REPORTS_MARK}{name}" for name in REPORTS]
)

PREFERENCE_READER = "Lcom/dfinstagram/dfinstagram;->getBoolTrueEz(Ljava/lang/String;)Z"
METHOD_NAME = "throwIfBlocked"


@dataclass(frozen=True)
class Literal:
    """One path test. The match kind is per-literal, not per-rule.

    The shipped Reels rule tests `/api/v1/clips/homecoming/` with `endsWith` and
    `/clips/discover` with `contains`, under one toggle — so a rule-level match
    kind could not reproduce the method this generator has to reproduce.
    """

    text: str
    match: str = "endswith"

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise GuardError("a literal must have text; an empty path matches every request")
        if self.match not in MATCHES:
            raise GuardError(
                f"unknown match {self.match!r} for {self.text!r}; "
                f"expected one of {', '.join(sorted(MATCHES))}"
            )


@dataclass(frozen=True)
class Rule:
    """Some paths, and the toggles any one of which blocks them."""

    literals: tuple[Literal, ...]
    toggles: tuple[str, ...]
    #: Free text emitted as a smali comment above the rule. This is where a
    #: reason like "contains because the app's own matcher is an indexOf" lives,
    #: so the rationale survives regeneration instead of being a comment somebody
    #: has to remember to re-add.
    note: str = ""

    def __post_init__(self) -> None:
        if not self.literals:
            raise GuardError("a rule must test at least one path")
        if not self.toggles:
            raise GuardError(
                f"rule for {self.literals[0].text!r} names no toggle. A rule with no toggle "
                "would block unconditionally, ignoring the user's settings — which is a "
                "behaviour nobody asked for and the opposite of what a toggle is."
            )
        for toggle in self.toggles:
            if not toggle.startswith("disable_"):
                raise GuardError(
                    f"{toggle!r} is not a preference key; DFInsta's keys all start "
                    "with 'disable_'"
                )
        if len(set(self.toggles)) != len(self.toggles):
            raise GuardError(f"rule for {self.literals[0].text!r} repeats a toggle")


# ------------------------------------------------------------------- rendering


def _test_lines(literal: Literal, jump: str, target: str) -> list[str]:
    method, argument = MATCHES[literal.match]
    return [
        f'    const-string v1, "{literal.text}"',
        "",
        f"    invoke-virtual {{v0, v1}}, Ljava/lang/String;->{method}({argument})Z",
        "",
        "    move-result v2",
        "",
        f"    {jump} v2, :{target}",
        "",
    ]


def _toggle_lines(toggle: str, block_label: str = "cond_block") -> list[str]:
    return [
        f'    const-string v1, "{toggle}"',
        "",
        f"    invoke-static {{v1}}, {PREFERENCE_READER}",
        "",
        "    move-result v2",
        "",
        f"    if-nez v2, :{block_label}",
        "",
    ]


def _throw_body(message: str) -> list[str]:
    return [
        "    new-instance v0, Ljava/io/IOException;",
        "",
        f'    const-string v1, "{message}"',
        "",
        "    invoke-direct {v0, v1}, Ljava/io/IOException;-><init>(Ljava/lang/String;)V",
        "",
        "    throw v0",
        "",
    ]


def _rule_lines(
    index: int,
    rule: Rule,
    entry: str | None,
    fallthrough: str,
    block_label: str = "cond_block",
    *,
    record: bool = False,
) -> list[str]:
    lines: list[str] = []
    if entry is not None:
        lines.append(f"    :{entry}")
    for line in rule.note.splitlines():
        lines.append(f"    # {line}".rstrip())
    toggle_label = f"cond_rule_{index}_toggle"
    for position, literal in enumerate(rule.literals):
        last = position == len(rule.literals) - 1
        # Every literal but the last short-circuits INTO the toggle check; the
        # last one falls out to the next rule. With one literal there is nothing
        # to short-circuit to, so no label is emitted and none is referenced —
        # an unused label assembles fine but reads as a branch that is missing.
        if last:
            lines += _test_lines(literal, "if-eqz", fallthrough)
        else:
            lines += _test_lines(literal, "if-nez", toggle_label)
    if len(rule.literals) > 1:
        lines.append(f"    :{toggle_label}")
    if record:
        # v1 holds whichever literal matched — every route into this point set it
        # and nothing has overwritten it yet — and the toggle checks below are
        # about to reuse v1 for the preference key. One instruction, here, is what
        # lets the throw name a *path* rather than a rule: `/clips/discover` and
        # `/api/v1/clips/homecoming/` are one rule, and the first is the path two
        # walk protocols disagreed about.
        lines += ["    move-object v3, v1", ""]
    for toggle in rule.toggles:
        lines += _toggle_lines(toggle, block_label)
    return lines


OBSERVE_DESCRIPTOR = "Lcom/dfinstagram/observe;"
OBSERVE_TAG = "DFInstaObserve"
OBSERVE_CLASS_PATH = "com/dfinstagram/observe.smali"

def toggles_of(rules: Sequence[Rule]) -> tuple[str, ...]:
    """Every preference key the guard reads, in the order it reads them."""
    seen: list[str] = []
    for rule in rules:
        for toggle in rule.toggles:
            if toggle not in seen:
                seen.append(toggle)
    return tuple(seen)


def render_observe_class(toggles: Sequence[str]) -> str:
    """The class an observing build logs through.

    It emits four kinds of line, and the first two are what make the other two
    readable:

        I DFInstaObserve: !toggles +blocked disable_feed=1 disable_explore=0 ...
        I DFInstaObserve: /feed/timeline/
        I DFInstaObserve: !blocked /feed/timeline/

    **The last line is the one this class exists for now.** `!blocked` is emitted
    by the guard at the moment it decides to throw, so "did this rule fire, and how
    often" is *known*. It used to be inferred from how many times Instagram logged
    our own exception back to us, which under-reports by feature — see
    :data:`BLOCKED_DIRECTIVE`.

    **`+blocked` is what stops the new signal being worse than none.** A capture
    with no `!blocked` lines means nothing on its own: it is either a state where
    nothing was refused, or a build that could not have said. Every session
    recorded before this mark existed is the second, and a reader that could not
    tell would read all 48 of them as proof that nothing ever blocked.

    **The toggle line is read from the device, not typed by whoever ran the
    session.** A measurement taken with the blocks on cannot answer "is this
    endpoint ever requested" — blocking `/feed/timeline/` means there is no
    timeline response for Reels to be injected into, so the child never fires
    whatever Instagram would otherwise do. So every capture has to state which
    blocks were active, and the one place that cannot be wrong about it is the
    build itself. An operator-supplied answer would be the same shape of mistake
    as deriving `effective_from` from a flag the same person typed: a safety
    property that is really a formality.

    **Emitted on every call, not once per process.** The first version used a
    static "already logged" flag, and it failed on the first real session: the
    standard protocol is `logcat -c` immediately before walking the app, and
    Instagram's process is usually already alive at that point — so the one
    toggle line had been written into the buffer that was then cleared, the flag
    stayed set, and the capture contained 22 path lines and no statement of what
    was active. Silently. The property that matters is that **any capture holding
    a path line also holds the toggle state**, and a once-per-process line cannot
    give that. One extra line per checked request is the price, and a session here
    produces tens of them, not thousands.
    """
    if not toggles:
        raise GuardError(
            "an observing build with no toggles could not state what was active, and a "
            "capture that cannot state its toggle state is not evidence for any question "
            "that depends on it"
        )
    body = [
        f".class public final {OBSERVE_DESCRIPTOR[:-1]};",
        ".super Ljava/lang/Object;",
        "",
        "# GENERATED by src/dfinsta_pipeline/guards.py. Do not edit by hand.",
        "#",
        "# One line per watched request path; one line per request the guard REFUSED;",
        "# and, ahead of both, one line naming which blocks were active and one naming",
        "# what this build can report. All to android.util.Log. It NEVER throws: the",
        "# point is to learn what the app asks for without changing what it receives.",
        "#",
        "# Unlike the block message this stays on the device -- Instagram catches our",
        "# IOException and files it into its own error event, but has no reason to read",
        "# our log tag. That is exactly why the refusal is recorded here and not left to",
        "# be read back out of Instagram's telemetry, which under-reports by feature.",
        "",
        "",
        "# direct methods",
        ".method public constructor <init>()V",
        "    .locals 0",
        "",
        "    invoke-direct {p0}, Ljava/lang/Object;-><init>()V",
        "",
        "    return-void",
        ".end method",
        "",
        ".method public static seen(Ljava/lang/String;)V",
        "    .locals 1",
        "",
        f'    const-string v0, "{OBSERVE_TAG}"',
        "",
        "    invoke-static {v0, p0}, Landroid/util/Log;->i(Ljava/lang/String;Ljava/lang/String;)I",
        "",
        "    return-void",
        ".end method",
        "",
        "# One line per request the guard REFUSED, naming the literal that matched.",
        "# Called from throwIfBlocked immediately before the throw, so it records the",
        "# decision rather than Instagram's report of the consequence. Routed through",
        "# seen() like state() is: one Log call site, one tag, one thing to get wrong.",
        f".method public static {BLOCKED_METHOD}(Ljava/lang/String;)V",
        "    .locals 1",
        "",
        f'    const-string v0, "{BLOCKED_DIRECTIVE} "',
        "",
        "    invoke-virtual {v0, p0}, Ljava/lang/String;->concat(Ljava/lang/String;)Ljava/lang/String;",
        "",
        "    move-result-object v0",
        "",
        f"    invoke-static {{v0}}, {OBSERVE_DESCRIPTOR}->seen(Ljava/lang/String;)V",
        "",
        "    return-void",
        ".end method",
        "",
        "# Appends ` <key>=<0|1>`. A smali boolean IS the int 0 or 1, so the result of",
        "# getBoolTrueEz goes straight to append(I) with no branch to get wrong.",
        ".method private static one(Ljava/lang/StringBuilder;Ljava/lang/String;)V",
        "    .locals 2",
        "",
        '    const-string v0, " "',
        "",
        "    invoke-virtual {p0, v0}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;",
        "",
        "    invoke-virtual {p0, p1}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;",
        "",
        '    const-string v0, "="',
        "",
        "    invoke-virtual {p0, v0}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;",
        "",
        f"    invoke-static {{p1}}, {PREFERENCE_READER}",
        "",
        "    move-result v1",
        "",
        "    invoke-virtual {p0, v1}, Ljava/lang/StringBuilder;->append(I)Ljava/lang/StringBuilder;",
        "",
        "    return-void",
        ".end method",
        "",
        ".method public static state()V",
        "    .locals 3",
        "",
        "    new-instance v1, Ljava/lang/StringBuilder;",
        "",
        "    invoke-direct {v1}, Ljava/lang/StringBuilder;-><init>()V",
        "",
        f'    const-string v2, "{REPORTS_LINE}"',
        "",
        "    invoke-virtual {v1, v2}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;",
        "",
    ]
    for toggle in toggles:
        body += [
            f'    const-string v2, "{toggle}"',
            "",
            f"    invoke-static {{v1, v2}}, {OBSERVE_DESCRIPTOR}->one(Ljava/lang/StringBuilder;Ljava/lang/String;)V",
            "",
        ]
    body += [
        "    invoke-virtual {v1}, Ljava/lang/StringBuilder;->toString()Ljava/lang/String;",
        "",
        "    move-result-object v1",
        "",
        f"    invoke-static {{v1}}, {OBSERVE_DESCRIPTOR}->seen(Ljava/lang/String;)V",
        "",
        "    return-void",
        ".end method",
        "",
    ]
    return "\n".join(body)


def write_observe_class(custom_code_root: Path | str, toggles: Sequence[str]) -> Path:
    """Write the observe class into a patch source's `newCode/` tree.

    Mirrors `runtime_identity.write_probe_class`. Written only for an observing
    build, so a shipped APK carries neither the class nor the log tag.
    """
    destination = Path(custom_code_root) / OBSERVE_CLASS_PATH
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_observe_class(toggles), encoding="utf-8")
    return destination


def watched_literals(rules: Sequence[Rule], extra: Iterable[str] = ()) -> tuple[str, ...]:
    """Every path an observing build reports, in a stable order.

    Both the literals the guard already blocks and the candidates nobody has
    ruled on yet. The blocked ones matter as much as the candidates: "this rule
    is declared, enforced, and has never once been asked for" is the evidence
    that a recorded decision should be reconsidered, and it cannot be produced by
    watching only the undecided.
    """
    seen: list[str] = []
    for rule in rules:
        for literal in rule.literals:
            if literal.text not in seen:
                seen.append(literal.text)
    for literal in extra:
        if literal not in seen:
            seen.append(literal)
    return tuple(seen)


def _observe_lines(index: int, literal: str) -> list[str]:
    # `contains`, always: an observing build is asking "was this ever requested",
    # and a narrower test could answer no for a path that was.
    label = f"cond_observed_{index}"
    return [
        f'    const-string v1, "{literal}"',
        "",
        "    invoke-virtual {v0, v1}, Ljava/lang/String;->contains(Ljava/lang/CharSequence;)Z",
        "",
        "    move-result v2",
        "",
        f"    if-eqz v2, :{label}",
        "",
        f"    invoke-static {{v1}}, {OBSERVE_DESCRIPTOR}->seen(Ljava/lang/String;)V",
        "",
        f"    :{label}",
    ]


def slug(rule: Rule) -> str:
    """A short name for a rule, from its first path. Diagnostic builds only."""
    return re.sub(r"[^a-z0-9]+", "_", rule.literals[0].text.lower()).strip("_")


def render_method(
    rules: Sequence[Rule], *, diagnostic: bool = False, observe: Sequence[str] = ()
) -> str:
    """The complete `throwIfBlocked`, ready to splice into hooks.smali.

    `.locals 3` regardless of how many rules there are: v0 holds the path, v1 the
    literal or key under test and v2 the boolean, and every rule reuses all
    three. This is why adding a **rule** can never change the register contract,
    and why the count is not computed from the rules.

    An **observing** build takes a fourth, v3, to carry the literal that matched
    from the test that matched it down to the throw that refuses it. That is the
    one thing the shipped register contract cannot express, and it is confined to
    a build that never ships. What keeps it honest is that observation may not
    change what the guard *decides*: :func:`decide` executes a rendered method, and
    the observing and shipped forms are asserted to agree on every watched path
    under every toggle state, which is the property the old byte-for-byte
    comparison of the rule span was a proxy for.

    `diagnostic=True` gives every rule its own throw carrying its own message, so
    one grep says which rule fired. **This must never ship.** Instagram files our
    IOException into its own `IgFunctionalErrorEvent`, so a per-rule vocabulary
    would tell Meta which rules a modified client carries — owner decision,
    2026-08-08. It exists because "is this guard ever reached?" has no other
    honest answer, and a guard nobody has seen fire is the failure this project
    ships most. Each diagnostic message CONTAINS :data:`BLOCK_MESSAGE`, so such a
    build is a strict superset: every existing grep, probe and canonical count
    keeps working untouched.
    """
    if not rules:
        raise GuardError(
            "no rules: `throwIfBlocked` with no rule would compile, install, and "
            "silently block nothing. If that is genuinely wanted, delete the hook "
            "rather than generating an empty one."
        )
    _refuse_shadowed(rules)

    lines = [
        f".method public static {METHOD_NAME}(Ljava/net/URI;)V",
        f"    .locals {4 if observe else 3}",
        "",
        "    # GENERATED by src/dfinsta_pipeline/guards.py from the url_block_rules",
        "    # in manifest/hooks.json. Do not edit by hand: the next port regenerates it.",
        "",
        "    if-eqz p0, :cond_return",
        "",
        "    invoke-virtual {p0}, Ljava/net/URI;->getPath()Ljava/lang/String;",
        "",
        "    move-result-object v0",
        "",
        "    if-eqz v0, :cond_return",
        "",
    ]
    # Observation runs BEFORE any rule, and the order is the whole point: a
    # blocked path throws, and anything emitted after the throw would never run.
    # Observing first is what makes "declared, enforced, and never once asked
    # for" a measurable statement rather than a guess about a path we killed
    # before we could count it.
    if observe:
        # First, before any path can be reported: which blocks were active. A
        # capture that cannot say that is not evidence for "is this ever
        # requested", because our own blocks suppress requests downstream of them.
        lines += [f"    invoke-static {{}}, {OBSERVE_DESCRIPTOR}->state()V", ""]
    for index, literal in enumerate(observe):
        lines += _observe_lines(index, literal)
    if observe:
        lines.append("")
    # Only a diagnostic build needs a landing site per rule, because only its
    # message differs per rule. An observing build does NOT: it names the path from
    # v3, which every rule has already loaded, so one shared `:cond_block` serves
    # all of them — and the rules themselves keep the shape a shipped build has.
    per_rule = diagnostic
    for index, rule in enumerate(rules):
        entry = None if index == 0 else f"cond_rule_{index}"
        fallthrough = (
            "cond_return" if index == len(rules) - 1 else f"cond_rule_{index + 1}"
        )
        block = f"cond_block_{index}" if per_rule else "cond_block"
        lines += _rule_lines(index, rule, entry, fallthrough, block, record=bool(observe))
    lines += ["    :cond_return", "    return-void", ""]
    # Recorded BEFORE the throw, because the throw is the last thing this method
    # does. The message thrown stays the one fixed string whatever the rule was:
    # the rule's identity goes to our own log, where Instagram has no reason to
    # look, and not into an exception Instagram files into its own error event —
    # owner decision, 2026-08-08.
    record = (
        [
            f"    invoke-static {{v3}}, {OBSERVE_DESCRIPTOR}->"
            f"{BLOCKED_METHOD}(Ljava/lang/String;)V",
            "",
        ]
        if observe
        else []
    )
    if not per_rule:
        lines += ["    :cond_block"] + record + _throw_body(BLOCK_MESSAGE)
    else:
        for index, rule in enumerate(rules):
            lines += (
                [f"    :cond_block_{index}"]
                + record
                + _throw_body(f"{BLOCK_MESSAGE} [DIAG-{slug(rule)}]")
            )
    lines.append(".end method")
    return "\n".join(lines) + "\n"


def _refuse_shadowed(rules: Sequence[Rule]) -> None:
    """Refuse when an earlier rule would swallow a later one under a different toggle.

    Order is behaviour here, not presentation: the rules are tested top to bottom
    and the first match throws or falls through, so a broad `contains` above a
    narrow path means the narrow path's toggle is never consulted. `/feed/timeline/`
    tested with `contains` would swallow `/feed/timeline_stream/` exactly this way,
    and the two are under the same toggle today only by luck.

    Same-toggle shadowing is allowed and reported by nothing, because the outcome
    is identical either way — the request blocks when that toggle is on.
    """
    seen: list[tuple[Literal, tuple[str, ...]]] = []
    for rule in rules:
        for literal in rule.literals:
            for earlier, toggles in seen:
                if set(toggles) == set(rule.toggles):
                    continue
                if _swallows(earlier, literal):
                    raise GuardError(
                        f"{earlier.text!r} (match {earlier.match}, toggles "
                        f"{', '.join(earlier_t for earlier_t in toggles)}) is tested before "
                        f"{literal.text!r} (toggles {', '.join(rule.toggles)}) and matches "
                        "every path that one does, so the later rule's toggles would never "
                        "be consulted. Reorder them, or narrow the earlier match."
                    )
            seen.append((literal, rule.toggles))


def _swallows(earlier: Literal, later: Literal) -> bool:
    """Does every path matching `later` also match `earlier`?"""
    if earlier.match == "contains":
        # A substring test matches anything containing it, so it swallows any
        # later literal that contains it anywhere.
        return earlier.text in later.text
    # endsWith only swallows a later endsWith with the same tail.
    return later.match == "endswith" and later.text.endswith(earlier.text)


# ---------------------------------------------------------------- equivalence


_LABEL = re.compile(r":([A-Za-z_][A-Za-z0-9_]*)")


def normalise(method: str) -> tuple[str, ...]:
    """The instruction sequence, with label NAMES replaced by their position.

    Two methods are the same method when their normal forms are equal. Label text
    is discarded by the assembler and renumbered by baksmali, so comparing it
    would report a rename as a behaviour change — and a real change would then be
    one diff line among many.

    Comments and blank lines go too: a comment cannot alter control flow, and
    keeping them would make the rationale text in a rule's `note` part of the
    behavioural contract.
    """
    order: dict[str, int] = {}

    def number(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in order:
            order[name] = len(order)
        return f":L{order[name]}"

    out = []
    for raw in method.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(_LABEL.sub(number, line))
    return tuple(out)


# ------------------------------------------------------------------- manifest


MANIFEST_KEY = "url_block_rules"


# --------------------------------------------------------------- what it does


@dataclass(frozen=True)
class Decision:
    """What a rendered `throwIfBlocked` did with one path under one toggle state."""

    #: Did it throw? The only thing the app can observe.
    blocked: bool
    #: The message thrown, or `None`. A shipped build has exactly one of these.
    message: str | None = None
    #: The literal handed to `observe;->blocked`, or `None` in a build that does
    #: not record refusals. This is the claim the whole change rests on, so it is
    #: returned rather than inferred: the path named must be the path that matched.
    recorded: str | None = None
    #: The literals handed to `observe;->seen`, in order.
    observed: tuple[str, ...] = ()


#: An opcode is lowercase letters, hyphens, digits and `/` — `rem-int/lit8`,
#: `move-result-object`, `const/4`. Narrower than that and a line this interpreter
#: cannot execute is rejected as *unreadable* rather than as *unknown*, which puts
#: it in the wrong branch: the unknown-opcode refusal below then has no way to be
#: reached, and a mutation deleting it survives every test.
_INSTRUCTION = re.compile(r"^\s*(?P<op>[a-z][a-z0-9/-]*)(?:\s+(?P<rest>.*))?$")
_STRING = re.compile(r'^(?P<register>[vp]\d+), "(?P<text>.*)"$')
_INVOKE = re.compile(r"^\{(?P<args>[^}]*)\}, (?P<target>\S+)$")
_STEPS = 10_000


class _Unset:
    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "<no result pending>"


_UNSET = _Unset()


def _zero(value: Any) -> bool:
    """Is this register's content the `if-eqz` sense of zero?

    Type-aware on purpose. `""` is a *reference* and therefore non-zero, while
    Python would call it falsey — and an empty path is exactly what
    `replaceReelsEndpoint` leaves behind, so getting this backwards would model
    the one case the pipeline most needs to reason about.
    """

    if value is None:
        return True
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value == 0
    return False


def decide(
    method: str, path: str | None, toggles: Mapping[str, bool], *, uri: bool = True
) -> Decision:
    """Execute a rendered method against one path and one toggle state.

    An interpreter over exactly the instructions :func:`render_method` emits, and
    it **refuses** anything else — so it cannot quietly skip an instruction and
    report the decision the rest of the method would have made.

    This exists because the property that matters is *what the guard blocks*, and
    the observing and shipped forms no longer have the same instructions: an
    observing build carries a fourth register and a call the shipped one does not.
    Comparing their text used to stand in for comparing their behaviour, which was
    honest while the rules were byte-identical and would now be a test asserting
    that a change nobody made was not made. Running both is the direct statement,
    and it is the one a device would make.

    `toggles` must name **every** key the method reads: a missing key is refused
    rather than defaulted, because the interesting states here are the ones where
    a single toggle is on and the rest are off, and a default would make "off" and
    "never mentioned" the same answer.

    The URI and its path are **separate**, which is why `uri` exists. The guard
    tests both — `if-eqz p0` for a null URI and `if-eqz v0` for a null path — and
    modelling p0 *as* the path collapsed them: `path=None` took the first branch
    and the second could never run, so a matrix over paths looked like it covered
    a branch it never reached.
    """

    labels: dict[str, int] = {}
    program: list[tuple[str, str]] = []
    for line in method.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(".method") or stripped.startswith(".end method"):
            continue
        if stripped.startswith(".locals"):
            locals_count = int(stripped.split()[1])
            continue
        if stripped.startswith(":"):
            labels[stripped[1:]] = len(program)
            continue
        found = _INSTRUCTION.match(stripped)
        if found is None:
            raise GuardError(f"decide cannot read {stripped!r}")
        program.append((found.group("op"), (found.group("rest") or "").strip()))

    # Every jump target, checked before anything runs. Checking one only when its
    # branch is taken would let a method that does not assemble be "decided" —
    # right answer, from a model of a class that could never have existed.
    for op, rest in program:
        target = rest[rest.index(":") + 1:] if op == "goto" else (
            rest.partition(", :")[2] if op in ("if-eqz", "if-nez") else ""
        )
        if target and target not in labels:
            raise GuardError(f"decide: :{target} is jumped to and never defined")

    registers: dict[str, Any] = {"p0": "<uri>" if uri else None}
    #: What the last value-returning instruction produced. `_UNSET` between them,
    #: because `move-result` is only legal immediately after an invoke that
    #: returned something — dalvik rejects the class otherwise, and a model that
    #: quietly moved a stale value would report a decision for a method that could
    #: not run.
    result: Any = _UNSET
    observed: list[str] = []
    recorded: str | None = None

    def read(name: str) -> Any:
        if name not in registers:
            raise GuardError(f"decide: {name} is read before it is written")
        return registers[name]

    def write(name: str, value: Any) -> None:
        # A register the method never declared assembles nowhere, and this is the
        # cheapest place to find that out. `.locals 3` with a `v3` in it is the
        # exact mistake an observing build could make.
        if name.startswith("v") and int(name[1:]) >= locals_count:
            raise GuardError(
                f"{name} is written by a method declaring .locals {locals_count}; "
                "smali numbers locals from v0, so this method would not assemble"
            )
        registers[name] = value

    counter = 0
    at = 0
    while at < len(program):
        counter += 1
        if counter > _STEPS:
            raise GuardError("decide: the method did not terminate; a jump loops")
        op, rest = program[at]
        at += 1
        if op not in ("move-result", "move-result-object", "invoke-virtual",
                      "invoke-static", "invoke-direct"):
            result = _UNSET
        if op == "const-string":
            found = _STRING.match(rest)
            if found is None:
                raise GuardError(f"decide cannot read const-string {rest!r}")
            write(found.group("register"), found.group("text"))
        elif op == "move-object":
            target, _, source = rest.partition(", ")
            write(target, read(source))
        elif op == "move-result" or op == "move-result-object":
            if result is _UNSET:
                raise GuardError(
                    f"decide: {op} at instruction {at} follows no invoke that returned a "
                    "value. Dalvik requires it to come straight after one, so this "
                    "method would be rejected by the verifier and the class would not "
                    "load"
                )
            write(rest, result)
            result = _UNSET
        elif op == "goto":
            at = labels[rest[1:]]
        elif op in ("if-eqz", "if-nez"):
            register, _, label = rest.partition(", ")
            taken = _zero(read(register)) if op == "if-eqz" else not _zero(read(register))
            if taken:
                at = labels[label[1:]]
        elif op == "return-void":
            return Decision(False, None, recorded, tuple(observed))
        elif op == "new-instance":
            write(rest.split(",")[0], "(uninitialised)")
        elif op == "throw":
            return Decision(True, read(rest), recorded, tuple(observed))
        elif op in ("invoke-virtual", "invoke-static", "invoke-direct"):
            found = _INVOKE.match(rest)
            if found is None:
                raise GuardError(f"decide cannot read {op} {rest!r}")
            arguments = [a.strip() for a in found.group("args").split(",") if a.strip()]
            target = found.group("target")
            if target.endswith("getPath()Ljava/lang/String;"):
                # Reads through the URI rather than returning it: a URI is not its
                # path, and the guard branches on each separately.
                read(arguments[0])
                result = path
            elif target.endswith("endsWith(Ljava/lang/String;)Z"):
                subject, argument = read(arguments[0]), read(arguments[1])
                result = int(subject.endswith(argument))
            elif target.endswith("contains(Ljava/lang/CharSequence;)Z"):
                subject, argument = read(arguments[0]), read(arguments[1])
                result = int(argument in subject)
            elif target.endswith("concat(Ljava/lang/String;)Ljava/lang/String;"):
                result = read(arguments[0]) + read(arguments[1])
            elif target == PREFERENCE_READER:
                key = read(arguments[0])
                if key not in toggles:
                    raise GuardError(
                        f"the method reads the preference {key!r} and the state given to "
                        "decide does not name it. A state that leaves a key out cannot "
                        "say whether it was off or simply never considered"
                    )
                result = int(bool(toggles[key]))
            elif target == f"{OBSERVE_DESCRIPTOR}->seen(Ljava/lang/String;)V":
                observed.append(read(arguments[0]))
                result = _UNSET
            elif target == (
                f"{OBSERVE_DESCRIPTOR}->{BLOCKED_METHOD}(Ljava/lang/String;)V"
            ):
                recorded = read(arguments[0])
                result = _UNSET
            elif target == f"{OBSERVE_DESCRIPTOR}->state()V":
                result = _UNSET
            elif target.startswith("Ljava/io/IOException;-><init>"):
                write(arguments[0], read(arguments[1]))
                result = _UNSET
            else:
                raise GuardError(f"decide does not know the method {target}")
        else:
            raise GuardError(f"decide does not know the instruction {op!r}")
    raise GuardError("decide: the method ran off its end without returning or throwing")


def rules_from_manifest(
    manifest_path: Path | str, hook_id: str = "tigon_url_block"
) -> tuple[Rule, ...]:
    """Read the declared rules, refusing anything the template cannot express."""
    data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    entry = next(
        (h for h in data.get("hooks", ()) if h.get("hook_id") == hook_id), None
    )
    if entry is None:
        raise GuardError(f"{manifest_path} declares no hook {hook_id!r}")
    declared = entry.get(MANIFEST_KEY)
    if not declared:
        # NOT an empty tuple. `render_method` would refuse it anyway, but the
        # message would be about having no rules rather than about the manifest
        # not declaring any — and the two are different repairs.
        raise GuardError(
            f"{hook_id} in {manifest_path} has no {MANIFEST_KEY!r}, so there is nothing "
            "to generate from. This is not the same as a hook that blocks nothing."
        )
    return tuple(_rule_from(item) for item in declared)


WATCH_KEY = "observe_watch"


def watch_from_manifest(
    manifest_path: Path | str, hook_id: str = "tigon_url_block"
) -> tuple[str, ...]:
    """Extra paths a measurement build watches but does NOT block.

    Candidates nobody has ruled on, and — deliberately — literals we suspect are
    not request paths at all. `delivery/background_prefetch` was ruled `block` on
    2026-08-08 and turned out to be a no-op logger's marker name; watching it is
    how the pipeline produces *evidence* of that rather than a reading of one
    call site. A literal that is never observed while its neighbours are observed
    hundreds of times is a fact, not an interpretation.

    Absent is fine here and means "watch only what is blocked" — unlike
    `url_block_rules`, an empty watch list is a coherent state rather than a
    manifest that forgot to say anything.
    """
    data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    entry = next((h for h in data.get("hooks", ()) if h.get("hook_id") == hook_id), None)
    if entry is None:
        raise GuardError(f"{manifest_path} declares no hook {hook_id!r}")
    return tuple(str(x) for x in entry.get(WATCH_KEY) or ())


def _rule_from(item: Mapping[str, Any]) -> Rule:
    literals = item.get("literals")
    if not isinstance(literals, list) or not literals:
        raise GuardError(f"rule {item!r} declares no literals")
    return Rule(
        literals=tuple(
            Literal(text=str(l["text"]), match=str(l.get("match", "endswith")))
            for l in literals
        ),
        toggles=tuple(str(t) for t in item.get("toggles") or ()),
        note=str(item.get("note") or ""),
    )


# --------------------------------------------------------------------- source


def apply_to_source(source_path: Path | str, rules: Sequence[Rule]) -> bool:
    """Rewrite `throwIfBlocked` in place. Returns whether anything changed.

    Only that method: `hooks.smali` also holds `replaceReelsEndpoint`, which is a
    different hook and is not generated. Refuses a file with anything other than
    exactly one `throwIfBlocked` rather than replacing the first — two would mean
    silently generating one and leaving the other, which is the shape of every
    half-applied patch this project has shipped.
    """
    path = Path(source_path)
    text = path.read_text(encoding="utf-8")
    starts = [m.start() for m in re.finditer(rf"^\.method .*{METHOD_NAME}\(", text, re.M)]
    if len(starts) != 1:
        raise GuardError(
            f"{path} declares {len(starts)} methods named {METHOD_NAME}; expected exactly "
            "one. Generating over the first would leave the other untouched and shipping."
        )
    start = starts[0]
    end = text.index(".end method", start) + len(".end method\n")
    generated = render_method(rules)
    if text[start:end] == generated:
        return False
    path.write_text(text[:start] + generated + text[end:], encoding="utf-8")
    return True


def read_method(source_path: Path | str) -> str:
    """The `throwIfBlocked` currently in a source file, for comparison."""
    text = Path(source_path).read_text(encoding="utf-8")
    match = re.search(rf"^\.method .*{METHOD_NAME}\(", text, re.M)
    if match is None:
        raise GuardError(f"{source_path} declares no {METHOD_NAME}")
    end = text.index(".end method", match.start()) + len(".end method\n")
    return text[match.start() : end]


def _cli(argv: Sequence[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, default=Path("manifest/hooks.json"))
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("dfinsta_source/newCode/com/dfinstagram/hooks.smali"),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the source does not already match the declaration, and "
        "change nothing. This is the form a build or a test runs.",
    )
    args = parser.parse_args(argv)
    try:
        rules = rules_from_manifest(args.manifest)
        generated = render_method(rules)
        if args.check:
            current = read_method(args.source)
            if normalise(current) == normalise(generated):
                print(f"{args.source} matches {args.manifest}: {len(rules)} rules")
                return 0
            print(
                f"{args.source} does NOT match the {len(rules)} rules declared in "
                f"{args.manifest}. Run without --check to regenerate.",
                file=sys.stderr,
            )
            return 1
        changed = apply_to_source(args.source, rules)
        print(
            f"{'rewrote' if changed else 'unchanged'} {args.source}: {len(rules)} rules, "
            f"{sum(len(r.literals) for r in rules)} paths"
        )
        return 0
    except (GuardError, OSError, ValueError, KeyError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())

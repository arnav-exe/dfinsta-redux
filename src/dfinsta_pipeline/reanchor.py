"""Ask for a new anchor when the host is found and the anchor is not.

This is the automated half of a repair that was done by hand once, on Instagram
442. It is deliberately the *second* half: `tools/check_anchor.py` — the counting
that decides whether a proposed anchor is any good — was built first, because
a proposal loop whose verification does not exist yet is a machine for writing
confident wrong answers into the manifest.

===============================================================================
  THE ONE SITUATION THIS IS FOR
===============================================================================

`Resolution` says, in these words: *"1 candidate host(s) found, none matched the
anchor"*. The class is known. What changed is the **shape** of the site inside it.

That is not the same as a host that cannot be found — `discovery.py` already
handles that, by asking k agents which class and having verifiers try to refute
them. It is also not "the site is gone", which is a retirement and a human
ruling. It is the narrow case where 442 moved `clips/discover/` out of its host
class into a shared string table: same class, same method, same register
receiving the path, and a `const-string` that no longer exists.

===============================================================================
  ACCEPTANCE IS BY ARITHMETIC, NOT BY AGREEMENT
===============================================================================

`proposer.collect` runs k proposers and accepts what they **agree** on. That is
right for a class descriptor, where the answer space is small enough for two
agents to land on the same string. It is wrong here: an anchor is free text, two
correct answers can differ in every capture name and in how many lines of
context they carry, and `ask-the-agent-only-what-varies` records k-of-n failing
for exactly this reason when the question was a patch rather than a class.

So k is not a vote here. **k is attempts.** Every candidate is checked on its own
against every decode on disk, and a candidate is accepted because it is selective
— not because another agent said the same thing. This is also why this module can
afford to ask for the whole form (anchor, payload and mode) where
`ask-the-agent-only-what-varies` warns against it: nothing is graded by
comparing two answers, so a wider answer does not dilute a vote.

What is checked, all of it mechanical:

1. the anchor compiles as a pattern;
2. it matches **exactly once inside the host class** — not merely somewhere;
3. across every decode on disk it is `unique` or `selective`, never `ambiguous`
   and never `dead` (see `tools/check_anchor.py` for what those mean);
4. the form constructs — `AnchorForm` and `Hook` between them refuse a payload
   using a capture the anchor does not bind, a payload that does not write the
   marker the right number of times, and an unknown mode.

===============================================================================
  IT PROPOSES; IT DOES NOT WRITE
===============================================================================

A verified candidate is returned, printed, and — only when a caller explicitly
asks — written into `manifest/hooks.json` as a new variant. `manifest/` is the
280 KB that is the project, and an agent's answer landing in it without a person
choosing to put it there is not a thing this pipeline does.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .hook_manifest import (
    AnchorForm,
    Hook,
    ManifestError,
    compile_anchor,
    find_form_hits,
)
from .proposer import AgentRunner, _checked_against, _last_object

#: What an answer must carry, and nothing else. An unknown field is an agent that
#: answered a question of its own; a missing one would be reported later as a
#: property of the app.
ANSWER_SCHEMA: Mapping[str, Any] = {
    "properties": {
        "anchor": {"type": "array"},
        "payload": {"type": "array"},
        "mode": {"type": "string"},
        "evidence": {"type": "array"},
    },
    "required": ["anchor", "payload", "mode"],
}

ACCEPTED = "accepted"
#: Every way a candidate can fail, kept as distinct strings because they mean
#: different things about what to do next: a pattern that does not compile is the
#: agent's mistake, and one that matches four classes is a real answer that is
#: not selective enough.
NOT_COMPILED = "does-not-compile"
NOT_IN_HOST = "not-once-in-the-host"
NOT_SELECTIVE = "matches-several-classes"
NOT_MY_SITE = "another-hook-s-site"
DEAD = "matches-nothing"
BAD_FORM = "form-refused"


@dataclass(frozen=True)
class Candidate:
    """One agent's answer, before anything has been checked about it."""

    proposer: str
    anchor: tuple[str, ...]
    payload: tuple[str, ...]
    mode: str
    evidence: tuple[str, ...] = ()

    def form(self) -> AnchorForm:
        return AnchorForm(anchor=self.anchor, payload=self.payload, mode=self.mode)


@dataclass(frozen=True)
class Checked:
    """A candidate and what counting it said."""

    candidate: Candidate
    outcome: str
    reason: str
    #: `{decode label: classes matched}`, so a rejection can be read rather than
    #: taken on trust.
    counts: Mapping[str, int] = field(default_factory=dict)
    in_host: int = 0

    @property
    def accepted(self) -> bool:
        return self.outcome == ACCEPTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposer": self.candidate.proposer,
            "outcome": self.outcome,
            "reason": self.reason,
            "anchor": list(self.candidate.anchor),
            "payload": list(self.candidate.payload),
            "mode": self.candidate.mode,
            "evidence": list(self.candidate.evidence),
            "in_host": self.in_host,
            "counts": dict(self.counts),
        }


@dataclass(frozen=True)
class ReanchorRun:
    """Every candidate for one hook, and which of them survived."""

    hook_id: str
    host: str
    checked: tuple[Checked, ...]
    failures: tuple[str, ...] = ()

    @property
    def accepted(self) -> tuple[Checked, ...]:
        return tuple(item for item in self.checked if item.accepted)

    @property
    def winner(self) -> Checked | None:
        """The accepted candidate with the fewest anchor lines.

        A tie-break, not a judgement: every accepted candidate has already
        passed the same counting, so preferring the shortest picks the one with
        the least surface to break next version rather than the one that happens
        to be first in a dict.
        """
        survivors = self.accepted
        if not survivors:
            return None
        return min(survivors, key=lambda item: (len(item.candidate.anchor), item.candidate.proposer))

    def to_dict(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook_id,
            "host": self.host,
            "accepted": len(self.accepted),
            "candidates": [item.to_dict() for item in self.checked],
            "failures": list(self.failures),
        }


def prompt(hook: Hook, host: str, host_source: str, version_label: str) -> str:
    """The task one proposer gets.

    It is handed the class body directly rather than a directory to search. The
    host is already known — that is the premise of this whole module — so asking
    the agent to find it again would be asking a question that has an answer, and
    grading it on one that does not.

    `hook.constraints` is withheld, as everywhere else: it records what the last
    version's patch looked like, and the shape is precisely what changed.
    """
    old = "\n".join(f"    {line}" for line in hook.anchor)
    payload = "\n".join(f"    {line}" for line in hook.payload)
    # `intent_constraints` says what the patch must ACHIEVE and is safe to show;
    # `hook.constraints` records what last version's patch looked like and is not.
    must = (
        "\n".join(f"  - {item}" for item in hook.intent_constraints)
        or "  - (none beyond the description above)"
    )
    return f"""A patch site in a decompiled Android app has changed shape, and the pattern
that used to locate it no longer matches. Your job is to write a new pattern.

## The app

Instagram {version_label}, decompiled with apktool. The class the site lives in
is already known — it is `{host}` — and its full body is at the end of this
message. You do not need to find the class. You need to find the site inside it.

## What the patch does

{hook.intent}

It must satisfy:

{must}

## The pattern that used to work, and no longer does

{old}

It is written in a small pattern language, and yours must be too:

  - Each line matches one whole smali instruction, and the lines must match
    CONSECUTIVE instructions (blank lines, `.line` directives and comment lines
    are skipped before matching, so do not include them).
  - `<name:kind>` captures a part that varies. The kinds are:
      `reg`    a register, `v0` or `p1`
      `type`   a type descriptor, `Lcom/example/Thing;` or `I`
      `member` a field or method name
      `any`    one run of non-space characters
  - `<name>` on its own repeats an earlier capture and must match the same text.
  - Everything else is literal and must match exactly.
  - `<init>` and `<clinit>` are literal text, not captures.

## What the patch writes there

{payload}

Your answer supplies this too, because the instruction that used to receive the
value may not exist any more. Whatever captures your payload uses, your anchor
must bind. It must contain the marker `{hook.marker}` exactly
{hook.expected_marker_count} time(s). `mode` is either `replace`, which replaces
the matched instructions with the payload, or `insert_after`, which leaves them
and adds the payload after the last one.

## What makes an answer good, and how it will be judged

Your pattern will be counted, not read. It is accepted only if it matches
**exactly once** in the class above, and if across every version of the app on
this machine it never matches more than one class. So:

  - **Prefer names the obfuscator cannot touch.** `Lcom/instagram/…`,
    `Landroid/…`, `Ljava/…` survive between versions. `LX/8Ec;` does not — that
    same name means a different class in the next release.
  - **Never pin an integer you did not derive.** Constants renumber every release.
  - Register numbers change; capture them, do not write them.
  - Enough lines to be unique, and not one more. Every extra line is another
    thing that can change next version.

## Answer

Reply with one JSON object and nothing after it:

{{"anchor": ["...", "..."], "payload": ["...", "..."], "mode": "insert_after",
 "evidence": ["what you checked, in one line each"]}}

## `{host}`

{host_source}
"""


def parse(proposer: str, response: str | Mapping[str, Any]) -> Candidate:
    """Turn one agent response into a `Candidate`, or raise.

    The LAST JSON object in the text wins: an agent that revises itself leaves
    the draft behind, and the draft is the answer it decided against.
    """
    data = _last_object(proposer, response) if isinstance(response, str) else dict(response)
    _checked_against(proposer, data, ANSWER_SCHEMA)
    anchor = tuple(str(line).strip() for line in data["anchor"])
    payload = tuple(str(line) for line in data["payload"])
    if not anchor:
        raise ValueError(f"{proposer}: proposed an empty anchor")
    if not payload:
        raise ValueError(f"{proposer}: proposed an empty payload")
    return Candidate(
        proposer=proposer,
        anchor=anchor,
        payload=payload,
        mode=str(data["mode"]).strip(),
        evidence=tuple(str(item) for item in data.get("evidence", ())),
    )


def _with_form(hook: Hook, candidate: Candidate) -> Hook:
    """The hook as it would be with this candidate as a second form.

    Built so `Hook.__post_init__` does the checking: it already refuses a payload
    using an unbound capture, a payload that writes the marker the wrong number
    of times, and an unknown mode. Re-implementing those here would be a second
    set of rules to keep in step with the first.
    """
    return Hook(
        hook_id=hook.hook_id,
        intent=hook.intent,
        tier=hook.tier,
        strategy=hook.strategy,
        semantic_deps=hook.semantic_deps,
        hosts=hook.hosts,
        anchor=hook.anchor,
        payload=hook.payload,
        marker=hook.marker,
        expected_marker_count=hook.expected_marker_count,
        mode=hook.mode,
        variants=(*hook.variants, candidate.form()),
    )


def check(
    hook: Hook,
    candidate: Candidate,
    host_source: str,
    counts: Mapping[str, int],
    *,
    fingerprint: bool = True,
    others: Sequence[Hook] = (),
) -> Checked:
    """Judge one candidate. Nothing here asks an agent anything.

    *counts* is `{decode label: how many classes this anchor matched}` over every
    decode on disk — `tools/check_anchor.py` computes exactly this, and the
    caller passes it in rather than this module scanning, so a test needs no
    decode and a run needs no second scanner.

    *fingerprint* is whether this anchor must ALSO identify the host class, and
    it is the difference between two very different bars. Measured over four
    versions, only three of the eight shipped anchor forms match exactly one
    class in a whole decode; the rest work because a `named` or `by_literal`
    fingerprint picks the class first and the anchor only has to be unique
    *inside* it. `tigon_url_block`'s matches seven classes on every version and
    always has.

    So demanding app-wide selectivity when the host is already findable asks for
    something harder than the job needs, and rejects repairs that would work.
    It defaults to True because the case this module exists for — 442 — was one
    where the host search had died with the literal, so the new anchor had to do
    both. Pass False only when something else really does find the class, and
    know that a variant accepted this way needs a host fingerprint that resolves.
    """
    try:
        compile_anchor(candidate.anchor)
    except ManifestError as error:
        return Checked(candidate, NOT_COMPILED, str(error), counts)
    try:
        extended = _with_form(hook, candidate)
    except ManifestError as error:
        return Checked(candidate, BAD_FORM, str(error), counts)

    form_index = len(extended.forms) - 1
    hits = find_form_hits(extended, host_source, only=form_index)
    in_host = len(hits[0][1]) if hits else 0
    if in_host != 1:
        return Checked(
            candidate,
            NOT_IN_HOST,
            f"matched {in_host} time(s) inside the host; the patch site is one place",
            counts,
            in_host,
        )
    if fingerprint and counts and any(value > 1 for value in counts.values()):
        worst = max(counts.items(), key=lambda item: item[1])
        return Checked(
            candidate,
            NOT_SELECTIVE,
            f"matched {worst[1]} classes in {worst[0]}; an anchor that picks several "
            "classes cannot be a fingerprint, and this one has to be",
            counts,
            in_host,
        )
    if fingerprint and counts and not any(counts.values()):
        return Checked(
            candidate,
            DEAD,
            "matched no class in any decode, including the one it was written for — "
            "which cannot be true while it matches once in the host, so the counts "
            "and the host body disagree about what is being checked",
            counts,
            in_host,
        )
    # Does it pin a site another hook already owns? Counting cannot tell: an
    # anchor on the WRONG endpoint in the RIGHT class is unique, selective and
    # compiles. A live proposer produced exactly that — it read the discover
    # literal as having been renamed to `clips/discover/stream/` and anchored on
    # the stream site, which `replace_reels_stream_endpoint` already patches. Two
    # hooks on one site, a different marker each, no collision to detect, and the
    # endpoint this hook exists for never blanked.
    span = (hits[0][1][0].first_line, hits[0][1][0].last_line)
    for other in others:
        if other.hook_id == hook.hook_id:
            continue
        for _, found in find_form_hits(other, host_source):
            for hit in found:
                if hit.first_line <= span[1] and span[0] <= hit.last_line:
                    return Checked(
                        candidate,
                        NOT_MY_SITE,
                        f"lines {span[0]}-{span[1]} are where {other.hook_id} patches. "
                        "An anchor on the wrong endpoint in the right class passes every "
                        "count there is",
                        counts,
                        in_host,
                    )
    reason = (
        "once in the host, and never ambiguous"
        if fingerprint
        else "once in the host; app-wide selectivity NOT required, so this variant "
             "needs a host fingerprint that still resolves"
    )
    return Checked(candidate, ACCEPTED, reason, counts, in_host)


def collect(
    hook: Hook,
    host: str,
    host_source: str,
    version_label: str,
    proposers: Mapping[str, AgentRunner],
    counts_for: Callable[[tuple[str, ...]], Mapping[str, int]],
    *,
    fingerprint: bool = True,
    others: Sequence[Hook] = (),
) -> ReanchorRun:
    """Ask every proposer, check every answer, return all of it.

    A proposer that fails is recorded and dropped rather than retried: k−1 real
    answers is a smaller sample, while a retried agent is a correlated one.

    Every answer is checked, including the ones that fail — a run that recorded
    only the winner could not afterwards say whether the others were close or
    nonsense, which is the difference between "this needs a better prompt" and
    "there is nothing to find here".
    """
    task = prompt(hook, host, host_source, version_label)
    checked: list[Checked] = []
    failures: list[str] = []
    for name, run in proposers.items():
        try:
            candidate = parse(name, run(task))
        except Exception as error:  # noqa: BLE001 - any failure is one proposer lost
            failures.append(f"{name}: {type(error).__name__}: {error}")
            continue
        checked.append(
            check(
                hook, candidate, host_source, counts_for(candidate.anchor),
                fingerprint=fingerprint, others=others,
            )
        )
    return ReanchorRun(hook.hook_id, host, tuple(checked), tuple(failures))


def as_variant(checked: Checked, note: str) -> dict[str, Any]:
    """The manifest fragment a human would paste, or `--apply` would write."""
    if not checked.accepted:
        raise ValueError(f"{checked.candidate.proposer}: not accepted ({checked.outcome})")
    return {
        "note": note,
        "anchor": list(checked.candidate.anchor),
        "mode": checked.candidate.mode,
        "payload": list(checked.candidate.payload),
    }


def apply_variant(manifest: Path, hook_id: str, variant: Mapping[str, Any]) -> None:
    """Append a variant to a hook, in place, preserving the file's shape.

    Refuses rather than overwriting: a second variant identical to one already
    there is a re-run, not a change, and appending it would leave the manifest
    with two forms that can both match — which the resolver refuses, for a hook
    that was working before this ran.
    """
    data = json.loads(manifest.read_text(encoding="utf-8"))
    for hook in data["hooks"]:
        if hook["hook_id"] != hook_id:
            continue
        existing = hook.setdefault("variants", [])
        if any(item.get("anchor") == variant["anchor"] for item in existing):
            raise ValueError(f"{hook_id} already carries this anchor as a variant")
        existing.append(dict(variant))
        manifest.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return
    raise ValueError(f"no hook {hook_id!r} in {manifest}")

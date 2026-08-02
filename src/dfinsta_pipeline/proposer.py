"""Stage 5a: ask k independent agents where a hook goes, without telling them.

`proposals.py` knows what to do with an answer. This is what produces one. It
covers the three things that have to be right before an agent's answer is worth
anything, and each of them is a thing that has gone wrong here before.

**The answers must be physically absent, not merely forbidden.** A blind holdout
on the 430 settings hook only produced a trustworthy number because the proposers
worked in a directory containing the stock decode and nothing else. Forbidding a
file is not removing it: `.git` alone makes every answer reachable, and this
repo's own history contains the resolved anchors for every version it has ported.
:func:`build_sandbox` hardlinks the decode into an isolated root outside the
repository — zero extra disk, and nothing else reachable from there.

Because the trees share inodes, **a sandbox is read-only**. Writing into one
corrupts the original decode, so the prompt says so explicitly and the caller
should not hand agents a write tool rooted there.

**The intent must be behavioural, never structural.** The proposer is told what
the control does for the user, not what class implements it. Anything more and
the experiment measures reading comprehension. The one structural hint that is
fair — and that a human porter would also hold — is that the app may ship more
than one implementation of the same control and pick between them at runtime,
because a proposer that stops at the first hit produces a patch that is perfect
statically and dead on half the devices. That is not a hypothetical: it is what
the 430 settings hook did.

**The verifier must not see the reasoning.** It is given the claim alone and told
to refute it, because a verifier shown a fluent rationale agrees with it. One
holdout proposer justified a correct answer with a fabricated claim about
register state; the answer survived, the reasoning was nonsense, and a reviewer
reading both would have been reassured by exactly the wrong thing.

**Ask the narrowest question that is actually open.** There are two prompts here.
:func:`host_prompt` asks only which class, and is the preferred one;
:func:`proposer_prompt` asks for an entire patch, and remains for a hook whose
manifest entry has no usable anchor shape. The first full k-proposer run against
439 measured why: 2 of 3 proposers reached the correct host and 1 of 3 agreed by
effect, because the two who were right wrote a 2-line anchor with a 16-line
payload and a 4-line anchor with a 2-line payload. The manifest already owns the
anchor pattern and the payload template, and ``resolve.Outcome.NEEDS_AGENT`` is
returned precisely when the missing fact is the host — so asking for the rest
manufactures the variance that then reads as disagreement.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .hook_manifest import Hook
from .proposals import (
    HostProposal,
    Proposal,
    ProposalError,
    Refutation,
    host_agreement,
    plurality,
)

#: What a proposer must return. Kept small: a host, an anchor, a payload, and the
#: evidence chain that led there — never a confidence score, which nothing
#: downstream reads.
PROPOSAL_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "required": ["descriptor", "smali_path", "anchor", "payload", "evidence"],
    "additionalProperties": False,
    "properties": {
        # The example is deliberately a descriptor no class has. An earlier
        # version used the real 439 answer here, which handed the result to every
        # proposer through the schema itself. `LX/0aaa;` stood in for a while and
        # was no better than a guess: it is a real class in both the 430 and the
        # 439 decode. `LX/0zzz;` was measured absent from both.
        "descriptor": {
            "type": "string",
            "description": "smali type descriptor of the host class, e.g. LX/0zzz;",
        },
        "smali_path": {"type": "string", "description": "path relative to the decode root"},
        "anchor": {
            "type": "array",
            "items": {"type": "string"},
            "description": "consecutive significant smali lines, each already stripped",
        },
        "payload": {
            "type": "array",
            "items": {"type": "string"},
            "description": "the smali to insert, indented as it should appear",
        },
        "evidence": {
            "type": "array",
            "items": {"type": "string"},
            "description": "each step you actually checked, with the file and line",
        },
        "alternatives": {
            "type": "array",
            "items": {"type": "string"},
            "description": "other sites you considered and why you rejected them",
        },
        "unresolved": {
            "type": "array",
            "items": {"type": "string"},
            "description": "anything you could not establish. Say so rather than guessing.",
        },
    },
}

#: What a proposer must return when the question is only WHICH CLASS. No anchor
#: and no payload: the manifest already owns the shape of the patch, and asking an
#: agent to reinvent it manufactures the variance that kills k-of-n agreement. See
#: :class:`~dfinsta_pipeline.proposals.HostProposal` for the measurement.
HOST_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "required": ["descriptor", "smali_path", "evidence"],
    "additionalProperties": False,
    "properties": {
        # Named after no class, like `PROPOSAL_SCHEMA`'s and for the same reason:
        # an earlier version of that schema used the real 439 answer as its
        # example and handed the result to every proposer through the schema
        # itself. Absence is measured rather than asserted — see
        # `tests/test_host_proposals.py` — because the descriptor that replaced
        # the answer, `LX/0aaa;`, turned out to be a real class in both decodes.
        "descriptor": {
            "type": "string",
            "description": (
                "smali type descriptor of the class that contains the site, exactly as "
                "the decode writes it, e.g. LX/0zzz; — no method suffix"
            ),
        },
        "smali_path": {
            "type": "string",
            "description": "path to that class's .smali file, relative to the decode root",
        },
        "evidence": {
            "type": "array",
            "items": {"type": "string"},
            "description": "each step you actually checked, with the file and line",
        },
        "alternatives": {
            "type": "array",
            "items": {"type": "string"},
            "description": "other classes you considered and why you rejected them",
        },
        "unresolved": {
            "type": "array",
            "items": {"type": "string"},
            "description": "anything you could not establish. Say so rather than guessing.",
        },
    },
}

VERDICT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "required": ["refuted", "finding", "checked"],
    "additionalProperties": False,
    "properties": {
        "refuted": {"type": "boolean"},
        "finding": {
            "type": "string",
            "description": "what you found, including when you found nothing",
        },
        "checked": {"type": "array", "items": {"type": "string"}},
    },
}


class SandboxError(RuntimeError):
    """Raised when an isolated sandbox cannot be made, or would not be isolated."""


def build_sandbox(decode: Path, root: Path, extra: Iterable[Path] = ()) -> Path:
    """Hardlink *decode* into *root*, so the answers are absent rather than banned.

    Hardlinks make this nearly free for a 1.7 GiB decode, at the cost that the
    trees share inodes — so nothing may write here. *root* must be outside the
    repository, because the repository contains every previously resolved anchor.
    """
    decode = Path(decode).resolve()
    root = Path(root).resolve()
    repository = Path(__file__).resolve().parents[2]
    if root == repository or repository in root.parents:
        raise SandboxError(
            f"{root} is inside {repository}. The repository holds the resolved anchors for "
            "every version ported so far, so a sandbox within it does not remove the "
            "answers — and forbidding a path is not removing it."
        )
    if root.exists():
        raise SandboxError(f"refusing to reuse {root}; a stale sandbox may hold answers")
    root.mkdir(parents=True)
    target = root / decode.name
    result = subprocess.run(
        ["cp", "-al", str(decode), str(target)], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        detail = result.stderr.strip()
        # `cp -al` reports this once per file, so the raw stderr is a screenful
        # of the same fact. Name the cause instead: a hard link cannot cross a
        # filesystem, and the usual way to hit it is a sandbox root under /tmp
        # (tmpfs) while the decode is not.
        if "cross-device" in detail or "Invalid cross-device link" in detail:
            raise SandboxError(
                f"{root} is on a different filesystem from {decode}, and a sandbox is "
                "hardlinked rather than copied — 1.7 GiB per run is what the hardlink "
                "avoids. Choose a sandbox root on the same device as the decode."
            )
        raise SandboxError(f"hardlinking {decode} failed: {detail[:400]}")
    for path in extra:
        path = Path(path)
        destination = root / path.name
        if path.is_dir():
            shutil.copytree(path, destination)
        else:
            shutil.copy2(path, destination)
    if (target / ".git").exists():  # pragma: no cover - decodes have no .git
        raise SandboxError(f"{target} contains a .git directory; the answers are reachable")
    return target


def proposer_prompt(hook: Hook, sandbox_decode: Path, version_label: str) -> str:
    """The task one proposer gets: behaviour, constraints, schema — no structure."""
    # `intent_constraints` ONLY. `hook.constraints` records what last version's
    # patch looked like — "the five-line anchor including the drawable and label
    # is what disambiguates" hands over the answer's shape, and the shape is
    # precisely what changes between versions.
    constraints = (
        "\n".join(f"  - {item}" for item in hook.intent_constraints)
        or "  - (none beyond the description above)"
    )
    marker = hook.marker
    return f"""You are locating where a behavioural modification must be injected into a
decompiled Android app. You have the decompiled smali at:

    {sandbox_decode}

That directory is Instagram {version_label}, decompiled with apktool. It is
HARDLINKED from a master copy: treat it as strictly READ-ONLY. Writing into it
corrupts the original.

Work ONLY inside that directory. Do not read anything under any git repository,
do not run `git`, and do not search the filesystem outside the path above. The
point of this task is to find the site from the app itself; a previously
recorded answer exists elsewhere on this machine and using it makes the result
worthless.

## What the modification must do

{hook.intent}

Constraints the final patch must satisfy:
{constraints}

## What makes this hard

The app is obfuscated: class and method names like `LX/04tC;` are meaningless and
are RECYCLED between versions, so a name that looks familiar is probably a
different class. Resolve by content only.

Be aware the app may ship MORE THAN ONE implementation of the same user-facing
control and choose between them at runtime from a server-driven flag. If that is
the case here, say so and say which one you are proposing, because patching only
one produces a modification that looks correct and does nothing on half of
devices.

## What to return

A JSON object matching this schema:

{json.dumps(PROPOSAL_SCHEMA, indent=2)}

The `anchor` is the exact consecutive smali lines the patch attaches to, each one
stripped of leading and trailing whitespace, copied verbatim from the file. It
must occur EXACTLY ONCE in that file — if your candidate lines occur twice,
lengthen the anchor until they are unique, and say in `evidence` how you checked.

Your `payload` MUST contain this line exactly once, as its own line:

    {marker}

It is an idempotence marker. The applier looks for it to tell whether this patch
is already present; a payload without it applies once, becomes invisible, and is
applied a second time on the next run. Nothing in the app tells you this — it is
a requirement of the tool that will apply your answer. Use a comment marker
verbatim as given; do not adapt or translate it.

Put in `evidence` the specific chain you actually followed, with file and line
for each step. Do not assert anything you did not verify — an unverified claim
that happens to sit beside a correct answer is worse than an admitted gap,
because it survives review. If you cannot establish something, list it in
`unresolved`.
"""


def host_prompt(hook: Hook, sandbox_decode: Path, version_label: str) -> str:
    """The narrow question: which class holds the site. Not what to write in it.

    Preferred over :func:`proposer_prompt` wherever the manifest carries a usable
    anchor shape, which is every hook but the one whose payload needs a value no
    anchor can capture. The framing is the same — physically absent answers,
    read-only sandbox, obfuscated and recycled names, more than one implementation
    of the same control — and the ask is smaller by everything the manifest
    already knows.
    """
    # `intent_constraints` ONLY, exactly as in `proposer_prompt`. `hook.constraints`
    # records what LAST version's patch looked like: "the five-line anchor including
    # the drawable and label is what disambiguates" hands over the answer's shape,
    # and it names the register and the self-profile type the previous port used.
    # The shape is precisely what changes between versions, so showing it turns a
    # search into a reading-comprehension exercise and makes the measurement
    # worthless.
    constraints = (
        "\n".join(f"  - {item}" for item in hook.intent_constraints)
        or "  - (none beyond the description above)"
    )
    return f"""You are locating WHICH CLASS of a decompiled Android app a behavioural
modification has to be made in. You have the decompiled smali at:

    {sandbox_decode}

That directory is Instagram {version_label}, decompiled with apktool. It is
HARDLINKED from a master copy: treat it as strictly READ-ONLY. Writing into it
corrupts the original.

Work ONLY inside that directory. Do not read anything under any git repository,
do not run `git`, and do not search the filesystem outside the path above. The
point of this task is to find the class from the app itself; a previously
recorded answer exists elsewhere on this machine and using it makes the result
worthless.

## What is being asked

Name the ONE class that contains the site where the modification below has to be
made, and prove that it is that class. That is the entire question.

You do NOT need to write the patch. Do not produce an anchor, a payload, or any
smali at all. What the modification does and how it is written are already known
and do not change between versions of this app. The class does — that is the
only fact being asked for, and the only one you will be judged on.

## What the modification must do

{hook.intent}

Constraints on the RESULT, listed because each one can rule a candidate class
out — not because you have to implement them:
{constraints}

## What makes this hard

The app is obfuscated: class and method names like `LX/04tC;` are meaningless and
are RECYCLED between versions, so a name that looks familiar is probably a
different class. Resolve by content only.

Be aware the app may ship MORE THAN ONE implementation of the same user-facing
control and choose between them at runtime from a server-driven flag. If that is
the case here, put the one you are proposing in `descriptor` and the other(s) in
`alternatives`, naming the flag if you found it. Reporting only the first hit
produces a modification that looks correct and does nothing on half of devices.

## What to return

A JSON object matching this schema:

{json.dumps(HOST_SCHEMA, indent=2)}

`descriptor` must be a smali CLASS descriptor written exactly as this decode
writes it — `Lpackage/Name;`, leading `L`, trailing semicolon, no method suffix
and no arguments. The tool that consumes your answer looks that string up
literally, so a descriptor naming a method, or missing its semicolon, matches no
class and is reported as a class that does not exist in this version.

Put in `evidence` the specific chain you actually followed, with file and line
for each step. Do not assert anything you did not verify — an unverified claim
that happens to sit beside a correct answer is worse than an admitted gap,
because it survives review. If you cannot establish something, list it in
`unresolved`.
"""


def verifier_prompt(hook: Hook, proposal: Proposal, sandbox_decode: Path) -> str:
    """The task the adversarial verifier gets: the claim only, and no rationale."""
    anchor = "\n".join(f"    {line}" for line in proposal.anchor)
    payload = "\n".join(f"    {line}" for line in proposal.payload)
    return f"""Your job is to REFUTE the claim below. You are not reviewing it for
approval — you are trying to show it is wrong. Someone else produced it and you
are deliberately not being shown their reasoning, because a plausible-sounding
justification makes reviewers agree with claims they should reject.

The decompiled app is at (READ-ONLY, hardlinked — do not write):

    {sandbox_decode}

Work only inside that directory. Do not read any git repository and do not
consult a previously recorded answer.

## The claim

The following modification, in `{proposal.descriptor}`, achieves this:

    {hook.intent}

Anchor (asserted to occur exactly once in that class):

{anchor}

Payload to inject:

{payload}

## What to check, and what would refute it

  - Does that anchor occur in that class EXACTLY once? More than once means the
    patch may attach in the wrong place; zero means it never applies at all.
  - Is this class actually on the path that produces the behaviour described, or
    merely a plausible-looking neighbour — analytics, prefetch, a config builder?
  - Does the payload read or write a register that is live afterwards?
  - Does the payload satisfy every constraint of the stated intent, including any
    condition restricting WHEN it should take effect? A modification that fires
    in cases it should not is a defect even though it works in the intended one.
  - Is there another implementation of the same control that this misses?

Return JSON matching:

{json.dumps(VERDICT_SCHEMA, indent=2)}

Set `refuted` true if you found a real problem. **Default to `refuted: true` if
you cannot satisfactorily establish that the claim holds** — an unrefuted claim
is treated downstream as evidence, so "I could not check" must not read as "it is
fine". Put in `finding` what you actually did and found, including when you found
nothing wrong; "looked and found nothing" and "did not look" must not be
indistinguishable.
"""


def host_verifier_prompt(
    hook: Hook, proposal: HostProposal, sandbox_decode: Path
) -> str:
    """Refute one host claim. The class and the behaviour, and nothing else.

    `proposal.evidence`, `alternatives` and `unresolved` are deliberately absent:
    a verifier shown the chain that produced a claim checks the chain instead of
    the claim, and agrees with it. One holdout proposer justified a correct answer
    with a fabricated statement about register state, and a reviewer reading both
    would have been reassured by exactly the wrong thing.
    """
    where = f" ({proposal.smali_path})" if proposal.smali_path.strip() else ""
    return f"""Your job is to REFUTE the claim below. You are not reviewing it for
approval — you are trying to show it is wrong. Someone else produced it and you
are deliberately not being shown their reasoning, because a plausible-sounding
justification makes reviewers agree with claims they should reject.

The decompiled app is at (READ-ONLY, hardlinked — do not write):

    {sandbox_decode}

Work only inside that directory. Do not read any git repository and do not
consult a previously recorded answer.

## The claim

`{proposal.descriptor}`{where} is the class containing the site where this
modification must be made:

    {hook.intent}

No patch is being claimed — only the class. Judge the class.

## What to check, and what would refute it

  - Does that class exist in this decode, at that path?
  - Is this class actually on the path that produces the behaviour described, or
    merely a plausible-looking neighbour — analytics, prefetch, a config builder,
    or a class that only passes the value along?
  - Names are obfuscated AND recycled between versions, so a familiar-looking
    descriptor is evidence of nothing. Confirm by content.
  - Is there another implementation of the same user-facing control, selected at
    runtime by a server-driven flag, that this class is not on? Patching one of
    two implementations looks correct and does nothing on half of devices.

Return JSON matching:

{json.dumps(VERDICT_SCHEMA, indent=2)}

Set `refuted` true if you found a real problem. **Default to `refuted: true` if
you cannot satisfactorily establish that the claim holds** — an unrefuted claim
is treated downstream as evidence, so "I could not check" must not read as "it is
fine". Put in `finding` what you actually did and found, including when you found
nothing wrong; "looked and found nothing" and "did not look" must not be
indistinguishable.
"""


JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _json_objects(text: str) -> list[str]:
    """Every balanced top-level `{...}` in *text*, in order.

    A greedy `\\{.*\\}` was enough for hand-collected answers and is wrong for
    real ones. The first closed-loop run returned a draft object, then prose,
    then a revised object; the greedy match spanned from the first brace to the
    last and produced "Extra data", so a proposal that had been reached
    correctly was discarded as malformed.

    Braces are counted rather than matched with a regex because JSON nests, and
    string contents are skipped so that a brace inside a smali payload -- which
    is where they live, in `invoke-static {v0}, ...` -- cannot unbalance the
    scan. That case is not hypothetical: every payload this schema carries is
    full of them.
    """

    objects: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, character in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            if depth == 0:
                start = index
            depth += 1
        elif character == "}":
            if depth:
                depth -= 1
                if depth == 0:
                    objects.append(text[start : index + 1])
    return objects


def _last_object(proposer: str, text: str) -> dict[str, Any]:
    """The LAST balanced JSON object in *text*, or raise.

    The last, not the first. An agent that revises itself leaves the draft
    behind, and the draft is the answer it decided against -- taking it would
    grade the wrong answer, and silently, because a draft is well-formed and
    plausible. Whatever it settled on is what it proposed.

    Shared by every parser that takes an answer rather than a verdict, so the
    rule has one home: a change here moves both, instead of one path keeping a
    discipline the other lost.
    """
    candidates = _json_objects(text)
    if not candidates:
        raise ProposalError(f"{proposer} returned no JSON object")
    errors: list[str] = []
    for candidate in reversed(candidates):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as error:
            errors.append(str(error))
            continue
        if isinstance(parsed, dict):
            return parsed
        errors.append("JSON value is not an object")
    raise ProposalError(
        f"{proposer} returned unparseable JSON: {errors[0] if errors else 'no object'}"
    )


def _checked_against(
    proposer: str, data: Mapping[str, Any], schema: Mapping[str, Any]
) -> None:
    """Refuse an answer whose fields are not the ones that were asked for.

    An unknown field is not a harmless extra. It is an agent that answered a
    question of its own -- the third proposer of the first real run invented a
    schema field and was dropped for it -- and accepting it would mean grading an
    answer nothing downstream reads. Missing required fields are refused here for
    the same reason: later stages would report the absence as a property of the
    app.
    """
    unknown = set(data) - set(schema["properties"])
    if unknown:
        raise ProposalError(f"{proposer} returned unknown field(s): {sorted(unknown)}")
    missing = [key for key in schema["required"] if key not in data]
    if missing:
        raise ProposalError(f"{proposer} omitted required field(s): {missing}")


def parse_proposal(hook_id: str, proposer: str, response: str | Mapping[str, Any]) -> Proposal:
    """Turn one agent response into a checked `Proposal`.

    Accepts a dict or raw text with JSON in it. Every field is validated by
    `Proposal` itself, so a malformed answer fails here rather than four stages
    later as "anchor not found".
    """
    data = _last_object(proposer, response) if isinstance(response, str) else dict(response)
    _checked_against(proposer, data, PROPOSAL_SCHEMA)
    return Proposal(
        hook_id=hook_id,
        proposer=proposer,
        descriptor=str(data["descriptor"]).strip(),
        anchor=tuple(str(line).strip() for line in data["anchor"]),
        payload=tuple(str(line) for line in data["payload"]),
        rationale=" | ".join(str(item) for item in data.get("evidence", ())),
        evidence=tuple(str(item) for item in data.get("evidence", ())),
    )


def parse_host(
    hook_id: str, proposer: str, response: str | Mapping[str, Any]
) -> HostProposal:
    """Turn one agent response into a checked `HostProposal`.

    Same discipline as :func:`parse_proposal`: the last object wins, unknown
    fields are refused, and the descriptor is checked for shape by `HostProposal`
    itself. A malformed answer fails here, where the message says what the agent
    did wrong, rather than at the Resolve stage where it reads as the class not
    existing in this version of the app.
    """
    data = _last_object(proposer, response) if isinstance(response, str) else dict(response)
    _checked_against(proposer, data, HOST_SCHEMA)
    return HostProposal(
        hook_id=hook_id,
        proposer=proposer,
        descriptor=str(data["descriptor"]).strip(),
        smali_path=str(data["smali_path"]).strip(),
        evidence=tuple(str(item) for item in data.get("evidence", ())),
        alternatives=tuple(str(item) for item in data.get("alternatives", ())),
        unresolved=tuple(str(item) for item in data.get("unresolved", ())),
    )


def parse_verdict(hook_id: str, verifier: str, response: str | Mapping[str, Any]) -> Refutation:
    if isinstance(response, str):
        # The same balanced scan `parse_proposal` uses, and for the same reason:
        # a greedy `\{.*\}` spans from the first brace to the last, so a verifier
        # that reasons in one JSON object and concludes in another produces
        # "Extra data" and is recorded as having refuted the claim. That fails
        # closed, which is right, but it discards a real verdict and reports a
        # refutation nobody made. Both verifiers in the first real run hit it.
        candidates = _json_objects(response)
        data = None
        error: Exception | None = None
        for candidate in reversed(candidates):
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError as failure:
                error = failure
                continue
            if isinstance(parsed, dict) and "refuted" in parsed:
                data = parsed
                break
        if data is None:
            # A verifier that produced nothing usable has not cleared anything.
            reason = f"unparseable verdict: {error}" if error else "no verdict object"
            return Refutation(hook_id, verifier, True, reason)
    else:
        data = dict(response)
    return Refutation(
        hook_id=hook_id,
        verifier=verifier,
        refuted=bool(data.get("refuted", True)),
        finding=str(data.get("finding") or "no finding reported"),
        checked=tuple(str(item) for item in data.get("checked", ())),
    )


#: An agent runner takes a prompt and returns the agent's final text. Injected so
#: this module needs no particular LLM client, and so tests need none at all.
AgentRunner = Callable[[str], str]


@dataclass
class ProposerRun:
    """One hook's proposers and verifier, as data rather than as a transcript."""

    hook_id: str
    proposals: tuple[Proposal, ...]
    refutations: tuple[Refutation, ...]
    failures: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook_id,
            "proposals": [item.to_dict() for item in self.proposals],
            "refutations": [
                {
                    "hook_id": item.hook_id,
                    "verifier": item.verifier,
                    "refuted": item.refuted,
                    "finding": item.finding,
                    "checked": list(item.checked),
                }
                for item in self.refutations
            ],
            "failures": list(self.failures),
        }


def collect(
    hook: Hook,
    sandbox_decode: Path,
    version_label: str,
    proposers: Mapping[str, AgentRunner],
    verifiers: Mapping[str, AgentRunner] | None = None,
) -> ProposerRun:
    """Run k proposers, then verifiers against the answer they agreed on.

    A proposer that fails is recorded as a failure and dropped, not retried into
    existence: k−1 real answers is a smaller sample, while a retried agent is a
    correlated one.
    """
    prompt = proposer_prompt(hook, sandbox_decode, version_label)
    proposals: list[Proposal] = []
    failures: list[str] = []
    for name, run in proposers.items():
        try:
            proposals.append(parse_proposal(hook.hook_id, name, run(prompt)))
        except (ProposalError, Exception) as error:  # noqa: BLE001
            failures.append(f"{name}: {type(error).__name__}: {error}")

    refutations: list[Refutation] = []
    if proposals and verifiers:
        # Verify the most-agreed answer; that is the one that would be applied.
        # Through `plurality`, for the same two reasons `collect_hosts` routes
        # through `host_agreement`: one vote per proposer, so a repeated agent
        # cannot choose what the expensive adversarial check is spent on, and
        # grouping by effect, so the subject is the answer `assess` would accept
        # rather than whichever wording happened to be submitted first.
        subject = plurality(proposals, hook).group[0]
        for name, run in verifiers.items():
            if name in proposers:
                # A verifier that also proposed is not independent evidence, and
                # `assess` would refuse it anyway.
                failures.append(f"{name}: skipped as verifier because it also proposed")
                continue
            check = verifier_prompt(hook, subject, sandbox_decode)
            try:
                refutations.append(parse_verdict(hook.hook_id, name, run(check)))
            except Exception as error:  # noqa: BLE001
                refutations.append(
                    Refutation(
                        hook.hook_id, name, True, f"verifier failed: {type(error).__name__}"
                    )
                )
    return ProposerRun(hook.hook_id, tuple(proposals), tuple(refutations), tuple(failures))


@dataclass
class HostRun:
    """One hook's host proposers and verifier, as data rather than as a transcript."""

    hook_id: str
    proposals: tuple[HostProposal, ...]
    refutations: tuple[Refutation, ...]
    failures: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook_id,
            "proposals": [item.to_dict() for item in self.proposals],
            "refutations": [
                {
                    "hook_id": item.hook_id,
                    "verifier": item.verifier,
                    "refuted": item.refuted,
                    "finding": item.finding,
                    "checked": list(item.checked),
                }
                for item in self.refutations
            ],
            "failures": list(self.failures),
        }


def collect_hosts(
    hook: Hook,
    sandbox_decode: Path,
    version_label: str,
    proposers: Mapping[str, AgentRunner],
    verifiers: Mapping[str, AgentRunner] | None = None,
) -> HostRun:
    """Ask k proposers which class, then verify the answer they converged on.

    The prompt is built ONCE and handed to every proposer unchanged, so no
    proposer can see another's answer — the independence the whole k-of-n
    measurement rests on is a property of this loop.

    A proposer that fails is recorded as a failure and dropped, not retried into
    existence: k−1 real answers is a smaller sample, while a retried agent is a
    correlated one.
    """
    prompt = host_prompt(hook, sandbox_decode, version_label)
    proposals: list[HostProposal] = []
    failures: list[str] = []
    for name, run in proposers.items():
        try:
            proposals.append(parse_host(hook.hook_id, name, run(prompt)))
        except Exception as error:  # noqa: BLE001
            failures.append(f"{name}: {type(error).__name__}: {error}")

    refutations: list[Refutation] = []
    if proposals and verifiers:
        # Verify the plurality answer, agreed or not: that is the one that would
        # be put forward, and a verifier's finding is worth having either way.
        # One vote per proposer first, so a repeated agent cannot choose the
        # subject.
        subject = host_agreement(proposals).group[0]
        for name, run in verifiers.items():
            if name in proposers:
                # A verifier that also proposed is not independent evidence, and
                # the ledger would refuse its claim anyway.
                failures.append(f"{name}: skipped as verifier because it also proposed")
                continue
            check = host_verifier_prompt(hook, subject, sandbox_decode)
            try:
                refutations.append(parse_verdict(hook.hook_id, name, run(check)))
            except Exception as error:  # noqa: BLE001
                refutations.append(
                    Refutation(
                        hook.hook_id, name, True, f"verifier failed: {type(error).__name__}"
                    )
                )
    return HostRun(hook.hook_id, tuple(proposals), tuple(refutations), tuple(failures))


def from_responses(
    hook: Hook, responses: Mapping[str, str | Mapping[str, Any]]
) -> ProposerRun:
    """Parse already-collected agent responses, keyed by proposer id.

    The seam for a caller that runs its agents elsewhere — a standalone app with
    an LLM client, or a session driving them by hand. Parsing and validation are
    the same either way, so a malformed answer is caught here rather than
    surfacing four stages later as "anchor not found".
    """
    proposals: list[Proposal] = []
    failures: list[str] = []
    for name, response in responses.items():
        try:
            proposals.append(parse_proposal(hook.hook_id, name, response))
        except Exception as error:  # noqa: BLE001
            failures.append(f"{name}: {type(error).__name__}: {error}")
    return ProposerRun(hook.hook_id, tuple(proposals), (), tuple(failures))


# ---------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    import argparse

    from .hook_manifest import load_manifest

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="action", required=True)

    make = sub.add_parser("sandbox", help="hardlink a decode somewhere the answers are not")
    make.add_argument("decode", type=Path)
    make.add_argument("--root", type=Path, required=True)

    ask = sub.add_parser("prompt", help="print the prompt one proposer should be given")
    ask.add_argument("hook_id")
    ask.add_argument("--sandbox", type=Path, required=True)
    ask.add_argument("--version", required=True)
    ask.add_argument("--manifest", type=Path, default=Path("manifest/hooks.json"))
    ask.add_argument(
        "--host",
        action="store_true",
        help="ask only WHICH CLASS, letting the manifest supply the patch shape",
    )

    take = sub.add_parser("collect", help="parse agent responses into a proposals file")
    take.add_argument("hook_id")
    take.add_argument(
        "responses",
        type=Path,
        help="directory of one response per proposer, named <proposer-id>.json",
    )
    take.add_argument("--manifest", type=Path, default=Path("manifest/hooks.json"))
    take.add_argument("--out", type=Path, required=True)

    args = parser.parse_args(argv)

    if args.action == "sandbox":
        print(build_sandbox(args.decode, args.root))
        return 0

    hooks = {hook.hook_id: hook for hook in load_manifest(args.manifest)}
    hook = hooks.get(args.hook_id)
    if hook is None:
        print(f"error: unknown hook {args.hook_id!r}", file=__import__("sys").stderr)
        return 2

    if args.action == "prompt":
        write = host_prompt if args.host else proposer_prompt
        print(write(hook, args.sandbox, args.version))
        return 0

    responses = {
        path.stem: path.read_text(encoding="utf-8")
        for path in sorted(args.responses.glob("*.json"))
    }
    run = from_responses(hook, responses)
    for failure in run.failures:
        print(f"dropped {failure}")
    args.out.write_text(
        json.dumps({hook.hook_id: [p.to_dict() for p in run.proposals]}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"{len(run.proposals)} proposal(s) -> {args.out}")
    return 0 if run.proposals else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

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
from .proposals import Proposal, ProposalError, Refutation

#: What a proposer must return. Kept small: a host, an anchor, a payload, and the
#: evidence chain that led there — never a confidence score, which nothing
#: downstream reads.
PROPOSAL_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "required": ["descriptor", "smali_path", "anchor", "payload", "evidence"],
    "additionalProperties": False,
    "properties": {
        # The example is deliberately a made-up descriptor. An earlier version
        # used the real 439 answer here, which handed the result to every
        # proposer through the schema itself.
        "descriptor": {
            "type": "string",
            "description": "smali type descriptor of the host class, e.g. LX/0aaa;",
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
        raise SandboxError(f"hardlinking {decode} failed: {result.stderr.strip()[:400]}")
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


JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def parse_proposal(hook_id: str, proposer: str, response: str | Mapping[str, Any]) -> Proposal:
    """Turn one agent response into a checked `Proposal`.

    Accepts a dict or raw text with JSON in it. Every field is validated by
    `Proposal` itself, so a malformed answer fails here rather than four stages
    later as "anchor not found".
    """
    if isinstance(response, str):
        match = JSON_BLOCK.search(response)
        if match is None:
            raise ProposalError(f"{proposer} returned no JSON object")
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as error:
            raise ProposalError(f"{proposer} returned unparseable JSON: {error}") from error
    else:
        data = dict(response)
    unknown = set(data) - set(PROPOSAL_SCHEMA["properties"])
    if unknown:
        raise ProposalError(f"{proposer} returned unknown field(s): {sorted(unknown)}")
    missing = [key for key in PROPOSAL_SCHEMA["required"] if key not in data]
    if missing:
        raise ProposalError(f"{proposer} omitted required field(s): {missing}")
    return Proposal(
        hook_id=hook_id,
        proposer=proposer,
        descriptor=str(data["descriptor"]).strip(),
        anchor=tuple(str(line).strip() for line in data["anchor"]),
        payload=tuple(str(line) for line in data["payload"]),
        rationale=" | ".join(str(item) for item in data.get("evidence", ())),
        evidence=tuple(str(item) for item in data.get("evidence", ())),
    )


def parse_verdict(hook_id: str, verifier: str, response: str | Mapping[str, Any]) -> Refutation:
    if isinstance(response, str):
        match = JSON_BLOCK.search(response)
        if match is None:
            # A verifier that produced nothing usable has not cleared anything.
            return Refutation(
                hook_id, verifier, True, "verifier returned no parseable verdict"
            )
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as error:
            return Refutation(hook_id, verifier, True, f"unparseable verdict: {error}")
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
        tally: dict[str, list[Proposal]] = {}
        for proposal in proposals:
            tally.setdefault(proposal.fingerprint, []).append(proposal)
        subject = max(tally.values(), key=len)[0]
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
        print(proposer_prompt(hook, args.sandbox, args.version))
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

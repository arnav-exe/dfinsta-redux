"""The `AgentRunner` stage 5a has always declared and never had.

`proposer.py` writes the prompt and builds the sandbox; `sandbox_tools.py` says
what an agent may do inside it; `proposals.py` decides what an answer is worth.
Between them sat `AgentRunner = Callable[[str], str]` and a human pasting
prompts into a chat window. This is the missing callable, and with it a run
produces its own candidates instead of waiting for someone to fetch them.

## Why this runtime

Three were priced against this seam rather than argued about.

**Google ADK**, which `docs/ADK_PIPELINE_PLAN.md` specifies, was measured and
rejected for *this* seam: +55 packages and +143 MiB over the direct SDK --
FastAPI, uvicorn, OpenTelemetry, gRPC and a Cloud Spanner client -- for a
minimal program 67 lines against the alternative's 66. It offers no confinement
help, cannot reach Anthropic's server-side JSON-schema enforcement, and makes
independence across k runs *opt-in*: reuse a `session_id` and run k is silently
conditioned on runs 1..k-1, which would look like agreement rather than like an
error. That is precisely the failure this project has already been burned by.
The divergence from the plan is recorded in `docs/PROPOSER_RUNTIME.md`; ADK
remains the right answer if this ever becomes multi-agent composition.

**The Anthropic API directly** is the smallest thing that works -- 16 packages,
structural independence, server-enforced output schemas -- and needs an API key
this machine does not have.

**The Claude Agent SDK** wins on the one fact that decides it today: it uses the
Claude Code credentials already on this machine, so closing the loop costs no
billing setup. Its confinement was verified by experiment rather than by
documentation: with `tools=[]`, `setting_sources=[]` and only the three sandbox
tools allowed, a model explicitly instructed to read a planted answer file
outside the sandbox had no tool that could, and every attempt was refused by
:class:`~.sandbox_tools.SandboxReader`.

The seam is unchanged either way, which is the point. `AgentRunner` is a plain
callable, so a second backend is a new function in this module and nothing else.

## What this deliberately does not do

**It does not retry a malformed answer.** `collect` already treats a failed
proposer as one fewer sample rather than retrying it, because a retried agent is
a correlated one, and the plan makes invalid agent output non-retryable so that
another generation costs an explicit budget. A retry hidden in here would spend
that budget invisibly and make k look larger than it is.

**It does not share anything between runs.** Each call is its own query with its
own tool server, so k proposers are independent by construction rather than by
remembering to vary an identifier.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .sandbox_tools import SandboxDenied, SandboxReader

#: Turns, not tokens: the thing that runs away here is a model searching the same
#: literal twenty different ways. Enough for a real navigation -- the measured
#: 439 host searches took well under this -- and short enough that a lost agent
#: fails rather than grinds.
DEFAULT_MAX_TURNS = 40

#: The three tools, named as the SDK exposes in-process MCP tools.
SERVER_NAME = "sandbox"
TOOL_NAMES = ("list_dir", "read_file", "search")
ALLOWED_TOOLS = tuple(f"mcp__{SERVER_NAME}__{name}" for name in TOOL_NAMES)


class AgentUnavailable(RuntimeError):
    """The runtime is not installed or not usable. Never a proposal failure.

    Kept distinct from a bad answer on purpose: "no agent ran" and "an agent
    answered badly" look identical in a results file and mean opposite things
    about whether the run measured anything.
    """


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    denied: bool
    #: Why it was refused. Recorded because the first real run produced six
    #: denials that could not afterwards be explained: none of them reproduced
    #: when replayed by hand, and the message that would have said why had been
    #: thrown away. A refusal without its reason is an unfalsifiable claim about
    #: the tool layer.
    reason: str = ""


@dataclass
class AgentOutcome:
    """What the agent said, and what it actually did while saying it.

    `calls` is the record of tool use, which is a different thing from the
    `evidence` an agent reports having checked. The gap between them is where a
    fabricated justification shows up -- and one holdout proposer did once
    justify a correct answer with a claim it had never verified.
    """

    text: str
    calls: list[ToolCall] = field(default_factory=list)
    turns: int = 0

    @property
    def denied_calls(self) -> int:
        return sum(1 for call in self.calls if call.denied)


def _in_thread(coroutine_factory: Callable[[], Any]) -> Any:
    """Run an async call on its own loop, in its own thread.

    `asyncio.run` would be enough from a script and would raise from inside a
    Temporal Activity, which is already running a loop. A dedicated thread works
    from both, and this is called once per proposer -- a thread per agent run is
    not a cost worth designing around.
    """

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coroutine_factory())).result()


def build_claude_runner(
    sandbox: Path | SandboxReader,
    *,
    model: str | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
) -> Callable[[str], str]:
    """Return an `AgentRunner` bound to one sandbox, with no other reach.

    The returned callable takes a prompt and returns the agent's final text, so
    it drops straight into `proposer.collect(..., proposers={"name": runner})`.
    Use :func:`run_agent` instead when the tool transcript is wanted.
    """

    reader = sandbox if isinstance(sandbox, SandboxReader) else SandboxReader(Path(sandbox).resolve())

    def run(prompt: str) -> str:
        return run_agent(prompt, reader, model=model, max_turns=max_turns).text

    return run


def run_agent(
    prompt: str,
    sandbox: Path | SandboxReader,
    *,
    model: str | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
) -> AgentOutcome:
    """Run one agent over one sandbox and return its answer and its transcript."""

    if type(prompt) is not str or not prompt.strip():
        raise ValueError("Agent prompt must be a non-empty string")
    if type(max_turns) is not int or max_turns < 1:
        raise ValueError("max_turns must be a positive integer")
    reader = sandbox if isinstance(sandbox, SandboxReader) else SandboxReader(Path(sandbox).resolve())
    return _in_thread(lambda: _run(prompt, reader, model, max_turns))


async def _run(
    prompt: str, reader: SandboxReader, model: str | None, max_turns: int
) -> AgentOutcome:
    try:
        from claude_agent_sdk import (  # type: ignore[import-not-found]
            ClaudeAgentOptions,
            create_sdk_mcp_server,
            query,
            tool,
        )
    except ImportError as error:  # pragma: no cover - depends on the environment
        raise AgentUnavailable(
            "claude-agent-sdk is not installed. `pip install claude-agent-sdk`, and note "
            "it drives the `claude` CLI, which must also be on PATH and logged in."
        ) from error

    outcome = AgentOutcome(text="")

    def handler(name: str):
        async def call(arguments: dict[str, Any]) -> dict[str, Any]:
            supplied = dict(arguments)
            try:
                text = reader.dispatch(name, supplied)
            except SandboxDenied as error:
                outcome.calls.append(ToolCall(name, supplied, denied=True, reason=str(error)))
                # Returned to the model rather than raised: a refusal is
                # information it should act on, and an exception escaping a tool
                # aborts the whole run instead of redirecting it.
                return {"content": [{"type": "text", "text": f"DENIED: {error}"}], "isError": True}
            outcome.calls.append(ToolCall(name, supplied, denied=False))
            return {"content": [{"type": "text", "text": text}]}

        return call

    # Schemas come from `sandbox_tools`, which states them as plain JSON Schema
    # so that the tool surface is not expressed in any one vendor's types. The
    # SDK's shorthand cannot express an optional argument, so the full schema is
    # passed through instead -- with the shorthand, a model omitting `include`
    # produced a call this side then refused, which is a self-inflicted failure.
    from .sandbox_tools import tool_specifications

    tools = [
        tool(spec["name"], spec["description"], spec["input_schema"])(handler(spec["name"]))
        for spec in tool_specifications()
    ]
    server = create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=tools)

    options_kwargs: dict[str, Any] = {
        # `tools=[]` removes every built-in: no Read, no Bash, no Glob, no
        # WebFetch. Verified by experiment, not taken from documentation -- a
        # model told to read a planted answer file outside the sandbox had
        # nothing that could, and the attempt it did make was refused here.
        "tools": [],
        # No settings file may re-enable what `tools=[]` removed.
        "setting_sources": [],
        "mcp_servers": {SERVER_NAME: server},
        "allowed_tools": list(ALLOWED_TOOLS),
        "permission_mode": "dontAsk",
        "max_turns": max_turns,
        # The sandbox is the working directory so relative paths in the model's
        # tool calls mean what the prompt says they mean.
        "cwd": str(reader.root),
    }
    if model is not None:
        options_kwargs["model"] = model

    chunks: list[str] = []
    async for message in query(prompt=prompt, options=ClaudeAgentOptions(**options_kwargs)):
        outcome.turns += 1
        for block in getattr(message, "content", None) or []:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                chunks.append(text)
        result = getattr(message, "result", None)
        if isinstance(result, str):
            chunks.append(result)

    outcome.text = "\n".join(chunks).strip()
    if not outcome.text:
        raise AgentUnavailable(
            "The agent returned no text at all. That is a runtime failure rather than a "
            "bad proposal, and must not be recorded as one."
        )
    return outcome

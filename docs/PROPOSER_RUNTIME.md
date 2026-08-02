# Which agent runtime runs the proposers

Decided 2026-08-02 by measurement, not by argument. Records a deliberate
divergence from [`docs/ADK_PIPELINE_PLAN.md`](ADK_PIPELINE_PLAN.md), which
specifies `google-adk[db]==2.5.0`.

## The seam

`proposer.py` has always declared `AgentRunner = Callable[[str], str]` and never
had one. The prompt, the sandbox, the parsing and the adversarial verifier were
all built; the agent call in the middle was a human pasting into a chat window.
That is the single thing standing between "the agents can port this" and "the
pipeline ports this", and it is why no run has ever produced its own candidates.

What the callable must do is narrow: take one long prompt, navigate a 1.7 GiB
decompiled decode read-only, and return JSON matching a fixed schema. It needs
three verbs and no more, and it must not be able to reach the rest of the
machine, because this repository holds the resolved answer for every version
already ported.

## Three runtimes, priced against that seam

| | packages | size | confinement | independence of k runs | schema |
|---|---|---|---|---|---|
| Anthropic API direct | 16 | 28 MiB | your own tools | structural | server-enforced |
| Claude Agent SDK | ~20 | — | your own tools, verified | per-query, structural | prompt-level |
| `google-adk[db]` | 69 (+55) | 160 MiB (+143) | your own tools | **opt-in, fails silently** | prompt-level |

**ADK was rejected for this seam.** Not on taste: a minimal program is 67 lines
against the alternative's 66, so the +143 MiB buys nothing here. It ships
FastAPI, uvicorn, OpenTelemetry, gRPC and — through the `[db]` extra the plan
pins — a Cloud Spanner client, for a persistence layer this pipeline must not
use, next to a Temporal ledger that is already the authority. It offers no
confinement help; its only path-checking toolset is `@experimental` and bundles
a subprocess executor and three write tools.

The fact that actually decided it: **independence across k runs is opt-in and
fails silently.** Measured with a counting stub — same `Runner`, same
`session_id`, three invocations saw 1, then 11, then 21 prior content items.
Reuse the id and proposer k is conditioned on proposers 1..k−1, which surfaces
as *agreement* rather than as an error. This project has already shipped an
inert hook that three agreeing proposers endorsed; a runtime whose default
manufactures agreement is the wrong default here.

**The Anthropic API directly** is the smallest correct thing and needs an API
key this machine does not have.

**The Claude Agent SDK** wins on that one fact: `ANTHROPIC_API_KEY` is unset and
`~/.claude/.credentials.json` exists, so it works today with no billing setup.

## Confinement was verified, not read

The claim that `tools=[]` removes every built-in is load-bearing — one
successful read of this repo invalidates the experiment — so it was tested
rather than cited. With `tools=[]`, `setting_sources=[]`, `permission_mode`
`dontAsk` and only the three sandbox tools allowed, a model was explicitly
instructed to read a planted answer file outside the sandbox, plus a home
dotfile and `/etc/hostname`, and told to try `..` traversal and symlinks if
refused. Every attempt arrived at `SandboxReader` and was refused; the planted
secret never appeared in its output; it had no other tool to try.

Two honest limits. The model also *declined* on its own judgement, which is not
evidence of confinement — the evidence is the refusal plus the absence of any
other tool, backed by unit tests that try traversal, symlink chains and prefix
collisions directly. And `SandboxReader` resolves-then-opens, so it does not
defend against a same-uid attacker swapping a symlink in between; that threat is
outside this pipeline's trust boundary and is stated rather than implied.

## Two things measurement changed

**`search` was completely broken** and no unit test would have caught it: the
argv named a constant that did not exist, so every call raised `NameError`. It
surfaced the first time a real agent tried to use it, because the smoke test had
only exercised the paths that raise. Any test that calls `search` and asserts a
successful result now closes that class of bug.

**`grep --max-count` is per file, not overall.** On the real 439 decode
`invoke-static` produces 202 MB of matches — all of it buffered by
`subprocess.run` in order to return 200 lines. Reading the pipe incrementally
and killing grep at the cap took that search from 3.6 s and 202 MB to **0.01 s
and 15 MB**, and made the broadest searches the fastest rather than the slowest.

## When to revisit

ADK's Anthropic support is first-class and its multi-agent composition is real.
If this seam grows into what ADK is actually for — agent transfer, hierarchical
delegation, shared state across specialist agents — it becomes the right answer.
`ADK_PIPELINE_PLAN.md:97` already says specialist agents wait for demonstrated
failure clusters. Until the problem has that shape, ADK is 143 MiB of runtime
for a loop the SDK already ships.

If it is adopted later, three things bite: pin `google-adk[extensions]`, not
`[db]` (you want `anthropic`, not Spanner); pass `Claude(model=...)` as an
instance, because the registry regexes are `claude-3-.*` and `claude-.*-4.*` and
do not match post-4 names; and give every run a distinct `session_id`.

## Cost of the coupling this does add

The Agent SDK drives the `claude` CLI, so the pipeline now depends on that
binary being installed and logged in. For a single-user tool that is a smaller
cost than a second billing account, but it is a real dependency on an
interactively-authenticated program, and it is the reason `AgentRunner` stayed a
plain callable: a second backend is one more function in `agent_runner.py` and
no change anywhere else.

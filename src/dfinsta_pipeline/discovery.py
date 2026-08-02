"""Stage 5a, wired to the driver: when nothing points at a host, go and ask.

Everything this needs already existed and none of it was joined. `proposer.py`
builds the sandbox and writes the two prompts, `agent_runner.py` turns a prompt
into an answer, `proposals.py` decides what k answers are worth, `evidence.py`
files what they proved. Between them sat a human running the agents by hand and
passing the result back in with ``--proposals``. This is the join, and it is the
last thing between the driver and an unattended port.

It is deliberately **opt-in**. Running it needs the network, spends the user's
quota and takes minutes per proposer, and the deterministic path resolves six of
the seven hooks without it. A flag that quietly costs money is the wrong default,
so ``port(discovery=None)`` — which is what every existing caller gets — behaves
exactly as it did before this module existed.

Four things it will not do, each of which is a way this project has already been
burned:

**It will not tell one proposer what another said.** The prompt is built once,
inside `proposer.collect_hosts`, and handed to every proposer unchanged. This
module runs those calls *concurrently* rather than one after another, because
each takes minutes and there are k of them — but concurrency shares nothing
except a string every proposer was going to be given anyway. See
:func:`concurrently` for the one invariant that makes that safe, and the guard
that holds when it does not.

**It will not retry a bad answer.** `collect_hosts` drops a proposer whose output
does not parse and records why. k−1 real answers is a smaller sample; a retried
agent is a correlated one, and correlation reads as agreement.

**It will not force a resolution through.** Disagreement produces no host at all,
so the hook stays ``NEEDS_AGENT`` and the run stops at Resolve. A refutation
produces the agreed host *and* a failed ``adversarial_verified`` claim, so the
hook resolves and then stalls at the evidence gate with the finding attached —
which is the more useful of the two, because the human gets a readiness report
naming the class and the objection rather than only an absence. Neither path
lets discovery decide that its own answer was good enough.

**It will not spend without saying so.** Every agent invocation is counted
against a cap, a hook is attempted only if the whole of its budget is available,
and a hook the cap could not cover is reported as *skipped* rather than as
*unresolved*. Silent truncation reads as "covered everything", which is the one
thing a budget report must never do.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Callable, Collection, Iterator, Mapping, Sequence

from .evidence import (
    HOST_ONLY,
    NO_PROPOSER,
    EvidenceClaim,
    EvidenceKind,
    EvidenceLedger,
    Producer,
    Subject,
    Verdict,
    agreement_claim,
)
from .hook_manifest import Hook
from .proposals import HostAgreement, host_agreement
from .proposer import AgentRunner, HostRun, build_sandbox, collect_hosts
from .resolve import HookResolution, Outcome, ResolveReport

#: Three proposers, because the measurement this whole route rests on was
#: 2-of-3: two reached the hard 439 settings host and the third failed outright.
#: Two proposers cannot distinguish "agreed" from "the only one who answered",
#: and `host_agreement` refuses a group of one for exactly that reason.
DEFAULT_K = 3

#: One adversarial verifier. It is the expensive, load-bearing check — the one
#: that caught a shipped inert hook that three agreeing proposers and every
#: static check said to ship — but a second one adds a vote to nothing: any
#: refutation at all stalls the hook.
DEFAULT_VERIFIERS = 1

#: Enough for the two `ui`-tier hooks at k=3 with one verifier each, and not a
#: third hook's worth. The plan's budget rule is "at most two generations per
#: unresolved intent"; this is that rule expressed as a number a run cannot
#: exceed by accident.
DEFAULT_MAX_AGENT_CALLS = 2 * (DEFAULT_K + DEFAULT_VERIFIERS)

#: The share of proposers that must reach one host. `host_agreement` additionally
#: requires two DISTINCT proposers in the winning group, which is the condition
#: that actually bites: one proposer answering while two abstain is a share of
#: 1.0.
DEFAULT_THRESHOLD = 0.5

#: Recorded as the actor on the agreement claim. Not a proposer and not a
#: verifier: agreement is a statistic over answers, and `evidence.ALLOWED_PRODUCERS`
#: only lets `Producer.STATISTICS` produce it.
AGREEMENT_ACTOR = "dfinsta_pipeline.discovery.host_agreement"

#: Proposer and verifier ids come from these prefixes and nowhere else, so a
#: verifier can never share a name with a proposer. That matters twice over:
#: `collect_hosts` skips a verifier that also proposed (correctly — it is not
#: independent evidence), and a skipped verifier that this module had already
#: dispatched would be money spent on an answer nothing reads.
PROPOSER_PREFIX = "proposer"
VERIFIER_PREFIX = "verifier"

#: Builds one agent runner, given the name it will be recorded under and the
#: sandbox it may read. Injected so that a test needs no model, no API key and no
#: `claude` CLI — the suite must never depend on any of the three.
RunnerFactory = Callable[[str, Path], AgentRunner]


def _print(line: str) -> None:  # pragma: no cover - trivial
    print(line, flush=True)


# --------------------------------------------------------------- configuration


@dataclass(frozen=True)
class Discovery:
    """Everything the discovery loop needs, as one value rather than six flags.

    Deliberately not a set of keyword arguments on ``port``. ``port`` has exactly
    one boolean switch — ``require_evidence`` — and `tests/test_driver.py` pins
    that mechanically, because a second boolean on that signature is how a second
    way past the evidence gate would arrive without anyone noticing. Discovery is
    not such a switch, and bundling it keeps the property checkable.
    """

    #: The version label the proposer prompt names, e.g. ``"439"``. The same
    #: label the cost ledger is keyed by, so a port cannot record what it spent
    #: under a different name than it asked under.
    version: str
    k: int = DEFAULT_K
    verifiers: int = DEFAULT_VERIFIERS
    #: Passed to the runtime. ``None`` means the runtime's own default.
    model: str | None = None
    max_agent_calls: int = DEFAULT_MAX_AGENT_CALLS
    #: Deliberately NOT a command-line flag. Lowering the bar for what counts as
    #: agreement, from a command line, at the moment a run refuses, is how a gate
    #: gets defeated — and it would leave a recorded claim saying the proposers
    #: agreed. Settable here so a test can state the bar it is testing.
    threshold: float = DEFAULT_THRESHOLD
    #: Where the hardlinked sandbox goes. ``None`` picks a fresh per-run
    #: directory under the system temporary directory. It must not exist —
    #: `proposer.build_sandbox` refuses to reuse a root, because a stale sandbox
    #: may hold an answer — and it must be outside this repository, which holds
    #: the resolved anchors for every version ported so far.
    sandbox_root: Path | None = None
    #: Keep the sandbox after the run instead of removing it. For looking at what
    #: an agent could see; not for reuse, which `build_sandbox` refuses anyway.
    keep_sandbox: bool = False
    runner: RunnerFactory | None = None

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError(
                "discovery needs the version label the proposers are being asked about; "
                "it names the app in the prompt and keys what the run cost"
            )
        if self.k < 2:
            raise ValueError(
                f"k={self.k} cannot corroborate anything. Agreement across independent "
                "proposers is a required item of evidence and a single answer cannot "
                "supply it, however good it looks."
            )
        if self.verifiers < 1:
            raise ValueError(
                "discovery needs at least one adversarial verifier; an unrefuted claim "
                "is treated downstream as evidence, so a claim nobody tried to break "
                "must not reach the gate looking like one that survived"
            )
        if self.max_agent_calls < self.k + self.verifiers:
            raise ValueError(
                f"a cap of {self.max_agent_calls} cannot cover one hook at k={self.k} "
                f"with {self.verifiers} verifier(s), so every hook would be reported as "
                "skipped and no agent would ever run"
            )
        if not 0 < self.threshold <= 1:
            raise ValueError(
                f"an agreement threshold of {self.threshold} is not a share of the "
                "proposers who were asked"
            )

    @property
    def per_hook(self) -> int:
        """What one hook costs at most: k proposers and every verifier."""
        return self.k + self.verifiers

    def preflight(self) -> None:
        """Refuse before spending anything if the runtime cannot answer at all.

        Without this, a machine with no agent runtime installed hardlinks the
        decode, drops k proposers that each failed to start, and files a claim
        saying no proposals were produced — which reads as "the agents looked and
        could not find it" and is instead "no agent ran". Those two look
        identical in a results file and mean opposite things about whether the
        run measured anything, which is the distinction
        `agent_runner.AgentUnavailable` exists to keep.
        """
        if self.runner is not None:
            return
        if find_spec("claude_agent_sdk") is None:
            from .agent_runner import AgentUnavailable  # noqa: PLC0415

            raise AgentUnavailable(
                "no agent runtime: `pip install 'dfinsta-pipeline[proposers]'`, and note "
                "it drives the `claude` CLI, which must also be on PATH and logged in. "
                "Nothing was asked and nothing was spent — this is not a finding about "
                "the app."
            )

    def runner_for(self, name: str, sandbox_decode: Path) -> AgentRunner:
        if self.runner is not None:
            return self.runner(name, sandbox_decode)
        from .agent_runner import build_claude_runner  # noqa: PLC0415

        # One runner per name, each its own query with its own tool server, so
        # independence is structural rather than a thing to remember.
        return build_claude_runner(sandbox_decode, model=self.model)

    def proposers(self, sandbox_decode: Path) -> dict[str, AgentRunner]:
        return {
            f"{PROPOSER_PREFIX}-{ordinal}": self.runner_for(
                f"{PROPOSER_PREFIX}-{ordinal}", sandbox_decode
            )
            for ordinal in range(1, self.k + 1)
        }

    def verifier_runners(self, sandbox_decode: Path) -> dict[str, AgentRunner]:
        return {
            f"{VERIFIER_PREFIX}-{ordinal}": self.runner_for(
                f"{VERIFIER_PREFIX}-{ordinal}", sandbox_decode
            )
            for ordinal in range(1, self.verifiers + 1)
        }


# ------------------------------------------------------------------ the target


def needs_a_host(item: HookResolution) -> bool:
    """Did this hook escalate BECAUSE nothing mechanical points at its host?

    Decided structurally, never by reading ``item.reason``: `resolve._classify`
    writes several different ``NEEDS_AGENT`` prose strings and a new one would be
    matched as whichever it happened to resemble. The branches below mirror
    `agent_cost._needs`, which answers the same question for the cost ledger —
    an unfilled capture is a ``capture:`` need and a matched anchor with a shape
    payload is a ``patch`` need, and an agent asked *which class* answers
    neither of them.

    That mirroring is the point of asking here rather than reusing the private
    helper: the two must agree, and a test asserts they do.
    """
    if item.outcome is not Outcome.NEEDS_AGENT:
        return False
    unfilled = [name for supply in item.supplies if not supply.ok for name in supply.missing]
    if unfilled:
        # It has a host already; what it cannot render is a payload value.
        return False
    return any(search.kind == "by_agent" and not search.candidates for search in item.searches)


# ------------------------------------------------------------------ the budget


@dataclass
class Budget:
    """Agent invocations, counted rather than estimated.

    Counted at the point of dispatch by :func:`concurrently`, so the number is
    what was actually spent and not what a formula predicted. A formula would go
    wrong silently the first time `collect_hosts` skipped a call.
    """

    cap: int
    spent: int = 0

    @property
    def left(self) -> int:
        return max(self.cap - self.spent, 0)

    def affords(self, calls: int) -> bool:
        return self.spent + calls <= self.cap

    def spend(self, calls: int) -> None:
        self.spent += calls


# ------------------------------------------------------------- the concurrency


@contextmanager
def concurrently(
    runners: Mapping[str, AgentRunner],
    on_dispatch: Callable[[int], None] | None = None,
) -> Iterator[dict[str, AgentRunner]]:
    """Wrap runners so a consumer that calls them one at a time runs them at once.

    `proposer.collect_hosts` asks each proposer in turn. Each answer takes
    minutes and there are k of them, so in sequence a run costs k times what it
    needs to. The wrapper turns the first call into a dispatch of *every* runner
    and hands each caller its own result.

    That is sound because of one invariant `collect_hosts` states and holds: the
    prompt is built once, before the loop, and every proposer gets it unchanged —
    which is also what makes the k-of-n measurement mean anything. A caller that
    ever varied the prompt per runner would otherwise be handed another agent's
    question, so the guard below runs such a call by itself instead. Nothing is
    shared between the runners except that one string, which each of them was
    going to be given anyway; no answer, no session and no state crosses between
    them.

    Lazy on purpose. A batch nobody calls dispatches nothing, which is what keeps
    a verifier from being run — and paid for — when no proposal parsed.
    """
    if not runners:
        yield dict(runners)
        return

    pool = ThreadPoolExecutor(max_workers=len(runners))
    futures: dict[str, Future[str]] = {}
    guard = threading.Lock()
    batch_prompt: list[str] = []

    def wrap(name: str) -> AgentRunner:
        def run(prompt: str) -> str:
            with guard:
                if not batch_prompt:
                    batch_prompt.append(prompt)
                    for other, runner in runners.items():
                        futures[other] = pool.submit(runner, prompt)
                    if on_dispatch is not None:
                        on_dispatch(len(runners))
                mine = futures.get(name) if prompt == batch_prompt[0] else None
            if mine is None:
                if on_dispatch is not None:
                    on_dispatch(1)
                return runners[name](prompt)
            return mine.result()

        return run

    try:
        yield {name: wrap(name) for name in runners}
    finally:
        for future in futures.values():
            future.cancel()
        pool.shutdown(wait=True)


# ------------------------------------------------------------------ the record


@dataclass(frozen=True)
class HookDiscovery:
    """What discovery did about one hook, and what it cost."""

    hook_id: str
    attempted: bool
    reason: str
    descriptor: str | None = None
    refuted_by: tuple[str, ...] = ()
    run: HostRun | None = None

    @property
    def agreed(self) -> bool:
        return self.descriptor is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook_id,
            "attempted": self.attempted,
            "agreed": self.agreed,
            "descriptor": self.descriptor,
            "refuted_by": list(self.refuted_by),
            "reason": self.reason,
            "run": self.run.to_dict() if self.run is not None else None,
        }


@dataclass(frozen=True)
class DiscoveryReport:
    """Everything one discovery pass did, spent, and did not get to."""

    version: str
    sandbox: str
    cap: int
    spent: int
    hooks: tuple[HookDiscovery, ...] = ()

    @property
    def hosts(self) -> dict[str, str]:
        """The agreed host per hook. A hook without one contributes nothing."""
        return {
            item.hook_id: item.descriptor
            for item in self.hooks
            if item.descriptor is not None
        }

    @property
    def skipped(self) -> tuple[str, ...]:
        return tuple(item.hook_id for item in self.hooks if not item.attempted)

    @property
    def cap_bound(self) -> bool:
        """Did the cap stop a hook from being attempted at all?

        Named separately from ``skipped`` because this is the thing that must be
        said out loud: a run that quietly stopped asking reads exactly like a run
        that had nothing left to ask about.
        """
        return bool(self.skipped)

    @property
    def notice(self) -> str:
        if not self.cap_bound:
            return ""
        return (
            f"the agent invocation cap ({self.cap}) bound after {self.spent} call(s): "
            f"{', '.join(self.skipped)} was not attempted at all. That is a budget "
            "stop, not a finding about the app — raise --max-agent-calls to cover it."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "sandbox": self.sandbox,
            "cap": self.cap,
            "spent": self.spent,
            "cap_bound": self.cap_bound,
            "notice": self.notice,
            "hosts": self.hosts,
            "skipped": list(self.skipped),
            "hooks": [item.to_dict() for item in self.hooks],
        }


# ---------------------------------------------------------------- the evidence


def file_host_evidence(
    ledger: EvidenceLedger,
    hook_id: str,
    run: HostRun,
    agreement: HostAgreement,
    threshold: float = DEFAULT_THRESHOLD,
) -> tuple[str, ...]:
    """Record what the proposers and the verifiers produced. Returns who refuted.

    Two claims, both from producers that are not the proposer, which is the whole
    reason the ledger distinguishes them:

    ``proposer_agreement`` is computed from :attr:`HostAgreement.votes` — one
    answer per proposer — and never from the raw proposals. The claim counts what
    it is given, so uncollapsed proposals would record one repeated agent as a
    consensus. It is asked as :data:`~dfinsta_pipeline.evidence.HOST_ONLY`,
    because a host answer judged by the whole-patch shape is scored on an anchor
    nobody asked it for and comes back ``not_exercised`` — a clean agreement
    filed as no agreement at all.

    ``adversarial_verified`` is one claim per verifier, ``passed`` only when that
    verifier looked and could not break the claim. A verifier that crashed or
    produced nothing usable has already been turned into a refutation by
    `proposer.parse_verdict`, so "I could not check" cannot arrive here looking
    like "it is fine".

    Nothing here decides anything. It records; the gate decides.
    """
    authors = {proposal.proposer.strip() for proposal in run.proposals}
    proposed_by = agreement.group[0].proposer if agreement.group else NO_PROPOSER
    ledger.register(
        Subject(
            hook_id,
            "agent",
            descriptor=agreement.agreed_descriptor,
            proposed_by=proposed_by,
        )
    )
    ledger.record(
        agreement_claim(
            hook_id,
            [proposal.to_dict() for proposal in agreement.votes],
            actor=AGREEMENT_ACTOR,
            threshold=threshold,
            asked=HOST_ONLY,
        )
    )
    refuted: list[str] = []
    for refutation in run.refutations:
        if refutation.verifier.strip() in authors:
            # A verifier that also proposed is not independent evidence.
            # `collect_hosts` already refuses to run one, and the ledger would
            # refuse the claim; this is the third guard because every one of them
            # is cheap and the failure it prevents is a proposer clearing its own
            # work.
            continue
        ledger.record(
            EvidenceClaim(
                hook_id=hook_id,
                kind=EvidenceKind.ADVERSARIAL_VERIFIED,
                verdict=Verdict.FAILED if refutation.refuted else Verdict.PASSED,
                producer=Producer.VERIFIER_AGENT,
                actor=refutation.verifier,
                summary=refutation.finding,
                detail={"checked": list(refutation.checked)},
            )
        )
        if refutation.refuted:
            refuted.append(refutation.verifier)
    return tuple(refuted)


# --------------------------------------------------------------------- the run


def discover_hosts(
    report: ResolveReport,
    hooks: Sequence[Hook],
    decode: Path,
    ledger: EvidenceLedger,
    settings: Discovery,
    skip: Collection[str] = (),
    log: Callable[[str], None] = _print,
) -> DiscoveryReport:
    """Ask k agents which class each host-less hook belongs in, and file the answers.

    *skip* names hooks whose evidence someone else already registered — a hook
    supplied through ``--full-proposals`` and refused by `proposals.assess` is
    still escalated, and re-registering its subject under a different proposer is
    an error the ledger raises rather than a state it merges. The human's own
    evidence stands.

    The sandbox is built **once per run** and hardlinked, so k proposers and the
    verifiers all read one copy at no extra disk, and it is removed afterwards.
    `build_sandbox` refuses a root that already exists and a root inside this
    repository; both refusals are load-bearing and neither is worked around here.
    """
    targets = [
        item
        for item in report.resolutions
        if needs_a_host(item) and item.hook_id not in set(skip)
    ]
    if not targets:
        return DiscoveryReport(settings.version, "", settings.max_agent_calls, 0)
    settings.preflight()

    by_id = {hook.hook_id: hook for hook in hooks}
    budget = Budget(settings.max_agent_calls)
    root, remove = _sandbox_root(settings)
    # Whether the root was already there decides whether this run may delete it.
    # `build_sandbox` refuses to reuse an existing root, and cleaning up after
    # that refusal would remove a directory this run never created — turning a
    # safety refusal into data loss.
    preexisting = root.exists()
    log(
        f"[discover] {len(targets)} hook(s) need a host; k={settings.k}, "
        f"verifiers={settings.verifiers}, cap={settings.max_agent_calls}"
    )
    results: list[HookDiscovery] = []
    try:
        sandbox_decode = build_sandbox(decode, root)
        log(f"[discover] sandbox {sandbox_decode}")
        for item in targets:
            hook = by_id.get(item.hook_id)
            if hook is None:  # pragma: no cover - the report came from these hooks
                continue
            if not budget.affords(settings.per_hook):
                results.append(
                    HookDiscovery(
                        item.hook_id,
                        attempted=False,
                        reason=(
                            f"not attempted: {budget.left} of the {settings.max_agent_calls} "
                            f"agent invocation(s) remain and this hook needs "
                            f"{settings.per_hook}"
                        ),
                    )
                )
                log(f"[discover] {item.hook_id}: SKIPPED, the cap would not cover it")
                continue
            results.append(
                _discover_one(hook, sandbox_decode, ledger, settings, budget, log)
            )
    finally:
        if settings.keep_sandbox:
            log(f"[discover] keeping the sandbox at {root}")
        elif preexisting:
            log(f"[discover] left {root} alone; it existed before this run")
        else:
            # Hardlinks: unlinking these removes the links, never the decode they
            # point at. And only ever a path this run created, never one it was
            # handed and refused.
            shutil.rmtree(remove, ignore_errors=True)

    outcome = DiscoveryReport(
        settings.version,
        str(root),
        settings.max_agent_calls,
        budget.spent,
        tuple(results),
    )
    if outcome.cap_bound:
        log(f"[discover] {outcome.notice}")
    return outcome


def _discover_one(
    hook: Hook,
    sandbox_decode: Path,
    ledger: EvidenceLedger,
    settings: Discovery,
    budget: Budget,
    log: Callable[[str], None],
) -> HookDiscovery:
    """One hook: k proposers, agreement, verifiers, and the evidence for all of it."""
    proposers = settings.proposers(sandbox_decode)
    verifiers = settings.verifier_runners(sandbox_decode)
    with concurrently(proposers, budget.spend) as asking, concurrently(
        verifiers, budget.spend
    ) as checking:
        run = collect_hosts(hook, sandbox_decode, settings.version, asking, checking)
    for failure in run.failures:
        log(f"[discover] {hook.hook_id}: dropped {failure}")

    agreement = host_agreement(run.proposals, settings.threshold)
    refuted_by = file_host_evidence(ledger, hook.hook_id, run, agreement, settings.threshold)

    if not agreement.agreed:
        log(f"[discover] {hook.hook_id}: no agreed host — {agreement.reason}")
        return HookDiscovery(
            hook.hook_id, attempted=True, reason=agreement.reason, run=run
        )
    if refuted_by:
        # The host is still put forward. It is what k proposers agreed on, and
        # the refutation is a separate item of evidence the gate reads: the hook
        # resolves and then stalls at the gate with the finding attached, which
        # tells a human far more than an unresolved hook does. Nothing here
        # weakens the gate — a failed `adversarial_verified` claim is not
        # satisfiable by anything short of a human waiver.
        log(
            f"[discover] {hook.hook_id}: {agreement.agreed_descriptor} agreed but "
            f"REFUTED by {', '.join(refuted_by)}; it will stall at the evidence gate"
        )
    else:
        log(
            f"[discover] {hook.hook_id}: {agreement.agreed_descriptor} — {agreement.reason}"
        )
    return HookDiscovery(
        hook.hook_id,
        attempted=True,
        reason=agreement.reason,
        descriptor=agreement.agreed_descriptor,
        refuted_by=refuted_by,
        run=run,
    )


#: Names the per-run temporary directory, so an abandoned one is obvious in a
#: listing of the system temporary directory rather than being one more `tmpXXXX`.
_PREFIX = "dfinsta-discovery-"


def _sandbox_root(settings: Discovery) -> tuple[Path, Path]:
    """Where the sandbox goes, and what has to be removed afterwards.

    A caller-supplied root is used as given and removed as given. One this module
    invents lives inside a per-run directory of its own, so two runs can never
    collide on the name and the whole of it can be removed without reasoning
    about what else might be in the system temporary directory.
    `build_sandbox` refuses a root that exists at all, which is the behaviour
    being respected rather than worked around: a stale sandbox may hold an
    answer.
    """
    if settings.sandbox_root is not None:
        root = Path(settings.sandbox_root)
        return root, root
    owned = Path(tempfile.mkdtemp(prefix=_PREFIX))
    return owned / "sandbox", owned

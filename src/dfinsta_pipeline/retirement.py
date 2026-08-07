"""The case for retiring a hook — assembled by a machine, closed only by a human.

    python -m dfinsta_pipeline.retirement candidates --version 441
    python -m dfinsta_pipeline.retirement case --version 441 --hook <id> \
        --investigation <json> --out <case.json>
    python -m dfinsta_pipeline.retirement rule --case <case.json> \
        --verdict retire --rationale "..." --ruled-by arnav --out <ruling.json>
    python -m dfinsta_pipeline.retirement publish --case <case.json> --ruling <ruling.json>

`dfinsta_pipeline.expectation` asserts that every hook release-ready on N-1 is
release-ready on N, and refuses to let that bar fall without a recorded
retirement. It has consumed `manifest/retirements.jsonl` since the day it was
written and **nothing has ever produced one**, so the only way past a drop was to
fix the hook. That is the right default and the wrong end state: a hook whose
surface Instagram has genuinely removed would fail the expectation for ever.

This is the producer. It is deliberately the most expensive path in the project.

===============================================================================
  WHY THIS IS NOT A GATE INSIDE THE PORT
===============================================================================

A port does not wait on this and cannot be unblocked by it. Retirement lands in
the manifest for the **next** port, never the one that raised the question.

The reason is an incentive rather than an architecture. If a red build could be
turned green by approving a retirement, then approving a retirement is the
cheapest thing a tired person can do at the end of a long port — and the gate
would reliably be answered "yes" precisely when the evidence for "yes" is weakest.
Making the answer arrive a version late costs nothing real: a hook that should be
retired is not urgent, because the thing it patched is already not working.

===============================================================================
  WHAT THIS REFUSES TO DO
===============================================================================

**It never decides.** An investigation carries a `recommendation`, and nothing
reads it as a verdict — `rule()` requires a verdict and a rationale from a named
human, and `Retirement.from_dict` in `expectation` refuses `ruled_by: agent`
outright. An agent may assemble every fact and still not close the case, because
the thing being measured must not get to rule that the measurement no longer
applies.

**`effective_from` is derived, never supplied.** It is always the version *after*
the one the case was built from. A retirement that could name its own effective
version could be backdated onto the port that exposed the drop, which is the same
"approve your way out of a red build" failure wearing a date. There is no flag for
it.

**It will not build a case it cannot reproduce.** A case names the version and the
hook and derives everything else from committed evidence, so the same two
arguments give byte-identical bytes — that is what lets a human sign a hash
somebody else's machine can check. If the evidence changes between raising the
case and answering it, the hashes disagree and the ruling is refused rather than
applied to a picture nobody saw.

**A drop and a dormancy are different cases and are labelled differently.** A hook
that was release-ready last version and is not now is a *regression*, and the
first response to a regression is to fix it. A hook that has never been
release-ready on any version in the series is the honest retirement candidate. The
case says which, in those words, because "not release-ready" flattens them.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .contracts import canonical_sha256
from .expectation import (
    RETIREMENTS,
    ExpectationError,
    evidence_files,
    Retirement,
    port_report,
    read_retirements,
    versions_with_evidence,
)
from .history import BASELINE_VERSION, _NUMERIC

__all__ = [
    "RetirementError",
    "VERDICTS",
    "Investigation",
    "Standing",
    "RetirementCase",
    "Ruling",
    "standings",
    "candidates",
    "build_case",
    "case_sha256",
    "rule",
    "validate_ruling",
    "publish",
    "main",
]


class RetirementError(RuntimeError):
    """Raised when a case cannot honestly be built, ruled on, or published."""


#: What a human may answer. `defer` is not a polite `keep`: it records that the
#: case was read and found insufficient, which is the answer an agent's draft
#: most often deserves, and it leaves the hook expected.
VERDICTS = ("retire", "keep", "defer")

#: What an investigation may recommend. Deliberately NOT the same tuple as
#: `VERDICTS`: an agent cannot `defer` (that is a statement about a human's
#: attention) and its `unclear` has no counterpart in a ruling, so the two
#: vocabularies cannot be silently interchanged by code that treats a
#: recommendation as an answer.
RECOMMENDATIONS = ("retire", "keep", "unclear")


def _string_list(value: object, label: str) -> tuple[str, ...]:
    """A JSON array of strings, or a refusal that names the field.

    `tuple(str(x) for x in value)` raises a bare `TypeError` on `null`, which
    `main` does not catch — so a drafting tool that found nothing and wrote
    `"findings": null` got a traceback and exit 1 where the contract is
    `refused:` and exit 2. A *string* is worse than an error: it iterates, and
    "no surface" silently becomes eleven single-character findings.
    """

    if value is None:
        raise RetirementError(f"{label} is null; use [] for none")
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise RetirementError(
            f"{label} must be a JSON array of strings, got {type(value).__name__}"
        )
    return tuple(str(item) for item in value)


@dataclass(frozen=True)
class Investigation:
    """What an agent found. Evidence for a human, and never a verdict.

    `recommendation` is advisory and is carried into the case so a human can
    disagree with it on the record. Nothing in this module reads it: `rule()`
    takes a verdict as an argument and `validate_ruling` never consults the
    recommendation, so an investigation that recommends `retire` and a human who
    rules `keep` produce a `keep`, with both positions preserved.
    """

    investigated_by: str
    summary: str
    findings: tuple[str, ...] = ()
    recommendation: str = "unclear"

    def __post_init__(self) -> None:
        if not self.investigated_by.strip():
            raise RetirementError("an investigation must name who ran it")
        if not self.summary.strip():
            raise RetirementError("an investigation with no summary is not evidence")
        if self.recommendation not in RECOMMENDATIONS:
            raise RetirementError(
                f"unknown recommendation {self.recommendation!r}; expected one of "
                f"{', '.join(RECOMMENDATIONS)}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "investigated_by": self.investigated_by,
            "summary": self.summary,
            "findings": list(self.findings),
            "recommendation": self.recommendation,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Investigation":
        if not isinstance(data, dict):
            raise RetirementError(
                f"an investigation must be a JSON object, got {type(data).__name__}"
            )
        unknown = sorted(set(data) - {"investigated_by", "summary", "findings", "recommendation"})
        if unknown:
            raise RetirementError(f"investigation has unknown keys: {', '.join(unknown)}")
        return cls(
            investigated_by=str(data.get("investigated_by", "")),
            summary=str(data.get("summary", "")),
            findings=_string_list(data.get("findings", ()), "findings"),
            recommendation=str(data.get("recommendation", "unclear")),
        )


@dataclass(frozen=True)
class Standing:
    """One hook's release-readiness across the whole series.

    The record a human needs in order to tell a regression from a dormancy, which
    is the distinction that decides whether retiring is sensible or is a way of
    not fixing something.
    """

    hook_id: str
    #: Versions where every required post-build kind passed, in release order.
    release_ready_on: tuple[str, ...]
    #: Versions whose evidence could be read at all, in release order. A version
    #: absent from BOTH tuples was never computable — 439 has no static evidence,
    #: so no hook has a standing there, and reading that as a failure would make
    #: every hook look permanently broken.
    assessed_on: tuple[str, ...]

    @property
    def never_release_ready(self) -> bool:
        return not self.release_ready_on

    @property
    def last_release_ready(self) -> str | None:
        return self.release_ready_on[-1] if self.release_ready_on else None

    def dropped_at(self) -> str | None:
        """The first assessed version after the last good one, or None.

        `None` covers both "still passing" and "never passed", which are opposite
        situations — read `never_release_ready` alongside it rather than treating
        a null here as good news.
        """

        last = self.last_release_ready
        if last is None:
            return None
        after = [v for v in self.assessed_on if int(v) > int(last)]
        return after[0] if after else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook_id,
            "release_ready_on": list(self.release_ready_on),
            "assessed_on": list(self.assessed_on),
            "never_release_ready": self.never_release_ready,
            "last_release_ready": self.last_release_ready,
            "dropped_at": self.dropped_at(),
        }


def standings(
    root: Path | str = ".",
    *,
    baseline: str = BASELINE_VERSION,
    ceiling: str | None = None,
) -> dict[str, Standing]:
    """Every hook's release-readiness across every assessable version.

    `ceiling` stops the series at a version, and a case built from version N sets
    it to N. Without it a `Standing` described the whole series including versions
    *after* the one the case was about, so porting 442 silently changed what a
    441 case said — and a docket recorded before that port could no longer be
    re-derived after it, which is the one property the whole run-keyed design
    exists to provide. It failed closed rather than admitting anything wrong, and
    a gate that can only be answered before the next port is still a broken gate.

    Assembled from `expectation.port_report`, which is `final_report`, which is
    the `EvidenceLedger` — the same answer the release gate reads, reached the
    same way. A second derivation of readiness here would agree with the first
    until one of them was edited.
    """

    root = Path(root)
    series = versions_with_evidence(root, baseline=baseline)
    if ceiling is not None:
        if not _NUMERIC.fullmatch(ceiling):
            raise RetirementError(f"ceiling {ceiling!r} is not a version number")
        series = [item for item in series if int(item) <= int(ceiling)]
    ready_by_version: dict[str, set[str]] = {}
    seen_by_version: dict[str, set[str]] = {}
    for index, version in enumerate(series):
        previous = series[index - 1] if index else None
        # Absent and unreadable are different facts, and conflating them is how a
        # corrupt corpus reads as a quiet one. A version whose evidence file does
        # not exist is skipped: 439 has runtime evidence and no static evidence,
        # because `static_verified` had no producer until 440, so its readiness is
        # unknowable rather than zero — recording it as zero would make every hook
        # look like it had been failing since the start of the series and turn the
        # whole manifest into retirement candidates.
        #
        # A file that exists and cannot be read is a REFUSAL. This was found the
        # only way it could be: a test corpus with the wrong `producer` on every
        # runtime claim was rejected by the ledger, every version was skipped, and
        # the result was the cheerful "every assessed hook is release-ready".
        # Silence that under-requires, in the module whose output is a list of
        # hooks somebody may decide to stop expecting.
        missing = [
            path for path in evidence_files(root, version, previous) if not path.is_file()
        ]
        try:
            report = port_report(root, version, previous)
        except ExpectationError:
            if missing:
                continue
            raise
        ready_by_version[version] = set(report.ready)
        seen_by_version[version] = set(report.hooks)

    out: dict[str, Standing] = {}
    for hook in sorted({h for hooks in seen_by_version.values() for h in hooks}):
        out[hook] = Standing(
            hook_id=hook,
            release_ready_on=tuple(
                v for v in series if hook in ready_by_version.get(v, ())
            ),
            assessed_on=tuple(v for v in series if hook in seen_by_version.get(v, ())),
        )
    return out


def candidates(
    root: Path | str = ".", *, version: str, baseline: str = BASELINE_VERSION
) -> list[Standing]:
    """Hooks a retirement case could reasonably be built for, at `version`.

    Not release-ready at `version`, and not already retired. Being a candidate is
    not an argument for retiring: the list exists so nobody has to remember which
    hooks are quietly failing, and a hook that dropped last week belongs on it
    exactly so that somebody looks at it.
    """

    root = Path(root)
    if not _NUMERIC.fullmatch(version):
        raise RetirementError(f"{version!r} is not a version number")
    retired = set(read_retirements(root))
    out = []
    # Bounded at the version being asked about, so the answer does not change
    # when a later port lands. See `standings`.
    for standing in standings(root, baseline=baseline, ceiling=version).values():
        if standing.hook_id in retired:
            continue
        if version not in standing.assessed_on:
            continue
        if version in standing.release_ready_on:
            continue
        out.append(standing)
    return out


@dataclass(frozen=True)
class RetirementCase:
    """What a human is being asked to rule on, and every fact behind it.

    Reproducible from `(version, hook_id)` plus the committed evidence plus the
    investigation. Two machines with the same repository and the same
    investigation produce byte-identical bytes, which is what makes signing its
    hash mean anything.
    """

    schema_version: int
    hook_id: str
    #: The version whose evidence this case was built from.
    version: str
    #: The first version that would stop expecting the hook. DERIVED as
    #: `version + 1` and never an argument -- see the module docstring.
    effective_from: str
    standing: Standing
    investigation: Investigation
    #: Straight from `hooks.json`, so the case says what the hook is *for*
    #: without a reader going to look it up. A retirement argued purely from red
    #: numbers is a retirement argued without knowing what is being lost.
    intent: str
    tier: str
    status: str

    def __post_init__(self) -> None:
        """The derivation, enforced on the OBJECT and not only on the file.

        It lived in `from_dict` alone, so the CLI was safe and the library was
        not: constructing a `RetirementCase` directly with
        `version="441", effective_from="441"` and handing it to `publish` wrote a
        row that excused the very port which exposed the drop. `publish` is the
        only writer of the file, so that was the whole invariant, reachable in
        one line. `Investigation` validated in `__post_init__` and this did not,
        which is what made the asymmetry look accidental rather than considered.
        """

        if not _NUMERIC.fullmatch(self.version):
            raise RetirementError(f"case version {self.version!r} is not a version number")
        expected = str(int(self.version) + 1)
        if self.effective_from != expected:
            raise RetirementError(
                f"case says effective_from {self.effective_from!r}; a case built from "
                f"{self.version} takes effect at {expected}. It is derived, not chosen — "
                "a retirement that named its own date could excuse the port that "
                "exposed the drop."
            )
        if self.hook_id != self.standing.hook_id:
            raise RetirementError("case and standing name different hooks")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "hook_id": self.hook_id,
            "version": self.version,
            "effective_from": self.effective_from,
            "standing": self.standing.to_dict(),
            "investigation": self.investigation.to_dict(),
            "intent": self.intent,
            "tier": self.tier,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RetirementCase":
        if not isinstance(data, dict):
            raise RetirementError(f"a case must be a JSON object, got {type(data).__name__}")
        if data.get("schema_version") != 1:
            raise RetirementError(f"unsupported case schema {data.get('schema_version')!r}")
        standing = data.get("standing")
        if not isinstance(standing, dict):
            raise RetirementError("case has no standing")
        version = str(data.get("version", ""))
        if not _NUMERIC.fullmatch(version):
            raise RetirementError(f"case version {version!r} is not a version number")
        # The `effective_from` and hook-agreement checks that used to live here
        # are now in `__post_init__`, so they hold for every construction path
        # rather than only for a file. Constructing below is what runs them.
        return cls(
            schema_version=1,
            hook_id=str(data.get("hook_id", "")),
            version=version,
            effective_from=str(data.get("effective_from", "")),
            standing=Standing(
                hook_id=str(standing.get("hook_id", "")),
                release_ready_on=_string_list(
                    standing.get("release_ready_on", ()), "release_ready_on"
                ),
                assessed_on=_string_list(standing.get("assessed_on", ()), "assessed_on"),
            ),
            investigation=Investigation.from_dict(data.get("investigation") or {}),
            intent=str(data.get("intent", "")),
            tier=str(data.get("tier", "")),
            status=str(data.get("status", "")),
        )



def case_sha256(case: RetirementCase) -> str:
    """The subject hash a human signs. Pure, and the same on any machine."""

    return canonical_sha256(case.to_dict())


def build_case(
    root: Path | str = ".",
    *,
    hook_id: str,
    version: str,
    investigation: Investigation,
    baseline: str = BASELINE_VERSION,
) -> RetirementCase:
    """Assemble the case for one hook at one version."""

    root = Path(root)
    if not _NUMERIC.fullmatch(version):
        raise RetirementError(f"{version!r} is not a version number")

    manifest = json.loads(
        (root / "manifest" / "hooks.json").read_text(encoding="utf-8")
    )
    declared = {hook["hook_id"]: hook for hook in manifest["hooks"]}
    if hook_id not in declared:
        raise RetirementError(
            f"{hook_id} is not in the hook manifest. A hook that is already gone "
            "needs its evidence dealt with, not a retirement"
        )
    already = read_retirements(root).get(hook_id)
    if already is not None:
        raise RetirementError(
            f"{hook_id} was already retired at {already.effective_from} by "
            f"{already.ruled_by} ({already.decision_id})"
        )

    found = standings(root, baseline=baseline, ceiling=version).get(hook_id)
    if found is None:
        raise RetirementError(
            f"{hook_id} has no assessable evidence on any version at or after "
            f"{baseline}, so there is nothing to build a case from"
        )
    if version not in found.assessed_on:
        raise RetirementError(
            f"{hook_id} was not assessed on {version}; assessed on "
            f"{', '.join(found.assessed_on) or 'no version'}"
        )

    hook = declared[hook_id]
    return RetirementCase(
        schema_version=1,
        hook_id=hook_id,
        version=version,
        effective_from=str(int(version) + 1),
        standing=found,
        investigation=investigation,
        intent=str(hook.get("intent", "")),
        tier=str(hook.get("tier", "")),
        status=str(hook.get("status", "active")),
    )


@dataclass(frozen=True)
class Ruling:
    """A human's answer to one case, bound to the exact bytes they read."""

    schema_version: int
    hook_id: str
    verdict: str
    rationale: str
    ruled_by: str
    #: The case this answers. A ruling that did not name its subject could be
    #: replayed against a later, different case for the same hook.
    case_sha256: str
    decision_id: str
    ruled_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "hook_id": self.hook_id,
            "verdict": self.verdict,
            "rationale": self.rationale,
            "ruled_by": self.ruled_by,
            "case_sha256": self.case_sha256,
            "decision_id": self.decision_id,
            "ruled_at": self.ruled_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Ruling":
        if not isinstance(data, dict):
            raise RetirementError(f"a ruling must be a JSON object, got {type(data).__name__}")
        if data.get("schema_version") != 1:
            raise RetirementError(f"unsupported ruling schema {data.get('schema_version')!r}")
        return cls(
            schema_version=1,
            hook_id=str(data.get("hook_id", "")),
            verdict=str(data.get("verdict", "")),
            rationale=str(data.get("rationale", "")),
            ruled_by=str(data.get("ruled_by", "")),
            case_sha256=str(data.get("case_sha256", "")),
            decision_id=str(data.get("decision_id", "")),
            ruled_at=str(data.get("ruled_at", "")),
        )


def _decision_id(
    case: RetirementCase, *, verdict: str, rationale: str, ruled_by: str, ruled_at: str
) -> str:
    """A ruling's identity, derived from its content.

    One function, called by `rule()` when it mints an id and by `validate_ruling`
    when it checks one. Two derivations agree only while nobody edits either; the
    authority recomputing what the minter computed is what makes "two identical
    answers deduplicate and two different ones cannot collide" true of every
    ruling rather than of the ones this module happened to produce.

    The verdict leads the id because a `keep` whose id began `retire-` was a
    small lie in a permanent record.
    """

    digest = canonical_sha256(
        {
            "case_sha256": case_sha256(case),
            "hook_id": case.hook_id,
            "rationale": rationale,
            "ruled_at": ruled_at,
            "ruled_by": ruled_by,
            "verdict": verdict,
        }
    )
    return f"{verdict}-{case.hook_id}-{digest[:12]}"


def validate_ruling(
    case: RetirementCase, ruling: Ruling, *, decision_id: str | None = None
) -> None:
    """Everything that must hold before a ruling may be published.

    Called by `rule()` when the ruling is made **and** by `publish()` before
    anything is written, deliberately twice. The first is a fast, legible refusal
    at the point a human can fix it; the second is the authority, and it runs even
    when the ruling arrives as a file from somewhere else. Where this project has
    split a check into a filter and an authority before, the authority checked
    *less* than the filter and "who may answer" ended up resting on the filter
    alone — so there is one function and both call it.
    """

    if ruling.hook_id != case.hook_id:
        raise RetirementError(
            f"ruling is for {ruling.hook_id} and the case is for {case.hook_id}"
        )
    if ruling.verdict not in VERDICTS:
        raise RetirementError(
            f"unknown verdict {ruling.verdict!r}; expected one of {', '.join(VERDICTS)}"
        )
    if not ruling.rationale.strip():
        raise RetirementError(
            "a ruling needs a rationale. The row it writes is permanent and the "
            "next reader is somebody asking why the bar moved"
        )
    if not ruling.ruled_by.strip():
        raise RetirementError("a ruling must name who made it")
    if ruling.ruled_by.strip().lower() == "agent":
        raise RetirementError(
            "ruled_by is 'agent'. An agent assembles the case; a human closes it. "
            "Otherwise the cheapest way past a failing check is for the thing being "
            "measured to rule that the measurement no longer applies"
        )
    if not ruling.decision_id.strip():
        raise RetirementError("a ruling must carry a decision id")
    # `ruled_at` and `decision_id` are checked HERE and not only in `rule()`.
    # This function's own docstring says it "runs even when the ruling arrives as
    # a file from somewhere else", and it did not hold: a hand-written ruling.json
    # carrying the printed subject hash, `"ruled_at": ""` and any decision id at
    # all published a row stamped with nothing. When this project last split a
    # check into a filter and an authority, the authority checked *less* than the
    # filter and "who may answer" came to rest on the filter alone.
    if not ruling.ruled_at.strip():
        raise RetirementError(
            "a ruling needs a timestamp. A record stamped by whoever happened to run "
            "it is one no reader can order"
        )
    # The binding. Re-derived from the case in hand rather than compared to a
    # value carried alongside it, so a ruling cannot be replayed onto a case that
    # has since changed -- new evidence, a different investigation, a later
    # version. The human answered a specific set of bytes or they answered
    # nothing.
    expected = case_sha256(case)
    if ruling.case_sha256 != expected:
        raise RetirementError(
            f"ruling answers case {ruling.case_sha256[:12]}… and this case is "
            f"{expected[:12]}…. The evidence changed after it was read; rebuild the "
            "case and rule again"
        )

    # AFTER the binding, deliberately. A changed case changes both digests, and
    # the id mismatch is a symptom while the stale subject is the cause — running
    # this first reported "decision id is not this answer's" for a case somebody
    # had edited, which is true and sends the reader to the wrong file.
    # `decision_id=` is supplied by exactly one caller: the consumer of an
    # ADMITTED gate ruling, which reads the id out of the ledger row the admitting
    # Activity wrote. That id is content-derived too — `submission.decision_identity`
    # computes it — just by a different function, and it is the durable link back
    # to the decision a human signed. It is not caller-chosen: a caller who could
    # pass any string here would reopen the hole this check closed.
    expected_id = decision_id or _decision_id(
        case,
        verdict=ruling.verdict,
        rationale=ruling.rationale,
        ruled_by=ruling.ruled_by,
        ruled_at=ruling.ruled_at,
    )
    if ruling.decision_id != expected_id:
        raise RetirementError(
            f"decision id {ruling.decision_id!r} is not this answer's. An id derived "
            "from anything but the answer cannot deduplicate a retry or distinguish "
            f"two different answers; expected {expected_id!r}"
        )


def rule(
    case: RetirementCase,
    *,
    verdict: str,
    rationale: str,
    ruled_by: str,
    ruled_at: str,
) -> Ruling:
    """Make a ruling against a case, deriving its identity from its content.

    `decision_id` is a digest of the answer rather than a supplied string, so two
    identical answers deduplicate and two different ones cannot collide — the same
    rule `submission.py` follows for a gate decision.
    """

    if not ruled_at.strip():
        # Also checked by `validate_ruling` below. Kept because the message here
        # can say the second half — that this layer must not read the clock for
        # itself, so a replay rewrites the line already on disk rather than
        # minting a new one — which is advice for a caller, not for an auditor.
        raise RetirementError(
            "a ruling needs a timestamp, and this layer must not read the clock: a "
            "record stamped by whoever happened to run it is one no reader can order"
        )
    ruling = Ruling(
        schema_version=1,
        hook_id=case.hook_id,
        verdict=verdict,
        rationale=rationale,
        ruled_by=ruled_by,
        case_sha256=case_sha256(case),
        decision_id=_decision_id(
            case,
            verdict=verdict,
            rationale=rationale,
            ruled_by=ruled_by,
            ruled_at=ruled_at,
        ),
        ruled_at=ruled_at,
    )
    validate_ruling(case, ruling)
    return ruling


def publish(
    case: RetirementCase,
    ruling: Ruling,
    *,
    root: Path | str = ".",
    path: Path | str | None = None,
    decision_id: str | None = None,
) -> Path | None:
    """Append the retirement row, if the verdict was to retire.

    Returns the file written, or `None` for a `keep` or `defer` — those are
    answers and not non-events, but they change nothing the expectation reads, and
    writing a row that says "still expected" would put a hook in a file whose only
    meaning is "no longer expected".

    `root`/`path` exist because the destination is a tracked file. A durable store
    whose writer has no seam is one every test writes to; this project shipped 36
    rows of fixture data into the committed evidence corpus that way.
    """

    validate_ruling(case, ruling, decision_id=decision_id)
    if ruling.verdict != "retire":
        return None

    location = Path(path) if path is not None else Path(root) / RETIREMENTS
    row = Retirement(
        hook_id=case.hook_id,
        effective_from=case.effective_from,
        decision_id=ruling.decision_id,
        ruled_by=ruling.ruled_by,
        rationale=ruling.rationale,
        recorded_at=ruling.ruled_at,
    )
    # Round-tripped before it is written, through the reader that will consume it.
    # The two modules agree today; a field renamed on either side would otherwise
    # produce a file that publishes cleanly and is refused on read, which is the
    # both-ends disconnection this project keeps shipping.
    Retirement.from_dict(row.to_dict())

    # `path=` wins over `root=` in the reader, so the check reads the file that is
    # about to be appended to and not a different one two directories up.
    existing = read_retirements(root, path=location)
    if case.hook_id in existing:
        raise RetirementError(
            f"{case.hook_id} already has a retirement at "
            f"{existing[case.hook_id].effective_from}; appending a second row cannot "
            "change it, because the earliest effective_from wins"
        )

    location.parent.mkdir(parents=True, exist_ok=True)
    with open(location, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row.to_dict(), sort_keys=True) + "\n")
    return location


def render_candidates(found: Sequence[Standing], version: str) -> str:
    lines = [
        f"RETIREMENT CANDIDATES at {version}",
        "=" * 60,
        "",
    ]
    if not found:
        lines.append("  None. Every assessed hook is release-ready at this version.")
        return "\n".join(lines)

    regressions = [s for s in found if not s.never_release_ready]
    dormant = [s for s in found if s.never_release_ready]

    if regressions:
        lines.append(f"  REGRESSIONS ({len(regressions)}) — fix these, do not retire them:")
        for standing in regressions:
            lines.append(
                f"    ! {standing.hook_id}"
                f"   last release-ready on {standing.last_release_ready}"
                f", dropped at {standing.dropped_at()}"
            )
        lines.append(
            "    A hook that was working one version ago is a regression. Retiring one "
            "is how a"
        )
        lines.append(
            "    project loses a feature it still wants; build the case only after the "
            "fix has failed."
        )
        lines.append("")

    if dormant:
        lines.append(f"  NEVER RELEASE-READY ({len(dormant)}) — the honest candidates:")
        for standing in dormant:
            seen = ", ".join(standing.assessed_on) or "no version"
            lines.append(f"    · {standing.hook_id}   assessed on {seen}, passed on none")
        lines.append(
            "    Dormant is not broken. A hook can be un-exercised because the "
            "walkthrough never"
        )
        lines.append(
            "    reaches its surface, which is a gap in measurement and not a reason "
            "to retire."
        )

    lines += [
        "",
        "  A candidate is a hook worth looking at, never an argument for retiring it.",
        f"  Build a case:  python -m dfinsta_pipeline.retirement case --version {version} "
        "--hook <id> …",
    ]
    return "\n".join(lines)


def render_case(case: RetirementCase) -> str:
    standing = case.standing
    lines = [
        f"RETIREMENT CASE  {case.hook_id}",
        "=" * 60,
        "",
        f"  built from        Instagram {case.version}",
        f"  would take effect {case.effective_from}   (derived: the version AFTER "
        f"{case.version}, never chosen)",
        f"  subject           {case_sha256(case)}",
        "",
        f"  what it does      {case.intent}",
        f"  tier / status     {case.tier} / {case.status}",
        "",
        "  standing",
        f"    assessed on       {', '.join(standing.assessed_on) or '—'}",
        f"    release-ready on  {', '.join(standing.release_ready_on) or 'NO VERSION'}",
    ]
    if standing.never_release_ready:
        lines.append(
            "    Never release-ready on any assessed version. Dormant coverage and a "
            "genuinely"
        )
        lines.append(
            "    removed surface look identical from here — the investigation is what "
            "separates them."
        )
    else:
        lines.append(f"    Last good on {standing.last_release_ready}, dropped at "
                     f"{standing.dropped_at()}.")
        lines.append(
            "    THIS IS A REGRESSION, not a dormancy. Retiring it discards a feature "
            "that worked"
        )
        lines.append("    one version ago.")

    lines += ["", f"  investigation by {case.investigation.investigated_by}", ""]
    for line in case.investigation.summary.splitlines():
        lines.append(f"    {line}")
    if case.investigation.findings:
        lines.append("")
        for finding in case.investigation.findings:
            lines.append(f"    - {finding}")
    lines += [
        "",
        f"    recommendation: {case.investigation.recommendation}   "
        "(advisory — nothing reads this as a verdict)",
        "",
        "  To answer: python -m dfinsta_pipeline.retirement rule --case <this file> \\",
        "      --verdict retire|keep|defer --rationale '…' --ruled-by <your name>",
        "",
        "  Only a human may rule. The rationale is permanent and the next reader is "
        "somebody",
        "  asking why the bar moved.",
    ]
    return "\n".join(lines)


def _load(path: Path, label: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RetirementError(f"no {label} at {path}") from error
    except json.JSONDecodeError as error:
        raise RetirementError(f"{path}: {error}") from error
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--baseline", default=BASELINE_VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("candidates", help="hooks not release-ready at a version")
    listing.add_argument("--version", required=True)
    listing.add_argument("--json", action="store_true")

    building = sub.add_parser("case", help="assemble the case for one hook")
    building.add_argument("--version", required=True)
    building.add_argument("--hook", required=True)
    building.add_argument(
        "--investigation",
        type=Path,
        required=True,
        help="JSON {investigated_by, summary, findings[], recommendation}. Required: "
        "a case with no investigation is a red number and a request to act on it",
    )
    building.add_argument("--out", type=Path, help="write the case JSON here")

    ruling_parser = sub.add_parser("rule", help="answer a case (humans only)")
    ruling_parser.add_argument("--case", type=Path, required=True)
    ruling_parser.add_argument("--verdict", required=True, choices=VERDICTS)
    ruling_parser.add_argument("--rationale", required=True)
    ruling_parser.add_argument("--ruled-by", required=True)
    ruling_parser.add_argument(
        "--ruled-at",
        required=True,
        help="ISO 8601. Supplied, never read from the clock here, so a replay "
        "rewrites the line already on disk rather than minting a new one",
    )
    ruling_parser.add_argument("--out", type=Path)

    publishing = sub.add_parser("publish", help="append the retirement row")
    publishing.add_argument("--case", type=Path, required=True)
    publishing.add_argument("--ruling", type=Path, required=True)
    publishing.add_argument(
        "--retirements",
        type=Path,
        help=f"where to append (default <root>/{RETIREMENTS})",
    )

    args = parser.parse_args(argv)

    try:
        if args.command == "candidates":
            found = candidates(args.root, version=args.version, baseline=args.baseline)
            if args.json:
                print(json.dumps([s.to_dict() for s in found], indent=2))
            else:
                print(render_candidates(found, args.version))
            return 0

        if args.command == "case":
            case = build_case(
                args.root,
                hook_id=args.hook,
                version=args.version,
                investigation=Investigation.from_dict(_load(args.investigation, "investigation")),
                baseline=args.baseline,
            )
            print(render_case(case))
            if args.out:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_text(
                    json.dumps(case.to_dict(), indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                print(f"\n  case written to {args.out}")
            return 0

        if args.command == "rule":
            case = RetirementCase.from_dict(_load(args.case, "case"))
            ruling = rule(
                case,
                verdict=args.verdict,
                rationale=args.rationale,
                ruled_by=args.ruled_by,
                ruled_at=args.ruled_at,
            )
            payload = json.dumps(ruling.to_dict(), indent=2, sort_keys=True)
            print(payload)
            if args.out:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_text(payload + "\n", encoding="utf-8")
            return 0

        case = RetirementCase.from_dict(_load(args.case, "case"))
        ruling = Ruling.from_dict(_load(args.ruling, "ruling"))
        written = publish(case, ruling, root=args.root, path=args.retirements)
        if written is None:
            print(
                f"{case.hook_id}: ruled {ruling.verdict} by {ruling.ruled_by}. Nothing "
                "written — the hook stays expected."
            )
        else:
            print(
                f"{case.hook_id}: retired from {case.effective_from}, recorded in "
                f"{written}.\nCommit it: the expectation reads the committed file, and "
                "an uncommitted row works here and vanishes on clone."
            )
        return 0
    except (RetirementError, ExpectationError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    except (ValueError, OSError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

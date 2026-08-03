"""The last evidence kind with no producer: does this version fail where the last one passed?

`EvidenceKind.DIFFERENTIAL` has been required of every hook, at every
provenance, since the ledger was written — and nothing has ever produced one.
Its own one-line description says what it is for:

    "a port regression, told apart from a broken probe"

Both halves matter, and the second is the hard one. When a hook that worked on
version N-1 shows nothing on version N, there are two explanations that look
identical from a single capture:

* the patch is inert on N — a genuine port regression, the thing this kind
  exists to catch; or
* the *probe* is blind on N, because the log line it counts was renamed, or the
  surface moved, or the account got routed past the feature.

Reporting the second as the first is how a working port gets held back. Reporting
the first as the second is how an inert hook ships. This module refuses to
collapse them: it asserts a regression only when the current capture can be shown
to have been *capable* of seeing the signal, and says "the probe went blind"
otherwise, naming what would settle it.

===============================================================================
  WHAT MAKES A CAPTURE CAPABLE, PER PROBE SHAPE
===============================================================================

Every runtime-probe claim already records enough to answer that, so nothing new
has to be measured. The three shapes `probes.py` produces:

* **delta** — a two-directional toggle probe. The instrument demonstrably worked
  if the signal was seen at all, in either direction. Zero on both sides is a
  capture in which the string never appeared, which is exactly the ambiguous case.
* **absence** — an assertion that something is NOT there, carrying its own
  positive control. `control_found` *is* this predicate, already recorded,
  already for this reason.
* **identity** — `DFInstaProbe: <hook_id>`, emitted by DFInsta's own class. This
  one is special and it is why the shapes are ranked: **its signal cannot rot,
  because we emit it.** If any hook announced itself in a capture then the
  instrument in that capture provably works, so a hook that stayed silent in it
  stayed silent for a reason of its own. That is the sharpest discrimination
  available between a dead hook and a dead probe, and it is why identity is
  preferred over delta and delta over absence when a hook has more than one.

===============================================================================
  WHY A BASELINE THAT DID NOT PASS CANNOT YIELD A PASS
===============================================================================

The question is "does this version fail where the last one passed". With no
baseline pass there is no *where*, and the honest verdict is `inconclusive` — not
`passed`, however good the current result looks. Letting a current pass satisfy
the differential on its own would make this kind a second copy of
`runtime_probe`, and a gate learns nothing from being told the same fact twice.

The cost is real and is meant to be: the **first** port of any hook can never
satisfy `DIFFERENTIAL` mechanically, because there is nothing behind it. That is
what `waiver` is for — a human records "first port of this hook" with their
authority on it — and it keeps the difference between "compared and unchanged"
and "never compared" visible at the gate instead of dissolving into a pass.

===============================================================================
  AND WHY COMPARING A VERSION WITH ITSELF IS REFUSED OUTRIGHT
===============================================================================

Two builds of one version are byte-identical here — measured, when the
unattended 439 run's output was compared against the hand-verified one — so a
differential between them is vacuously "identical" and would enter the ledger as
evidence that a comparison happened. :func:`differential_claims` raises rather
than returning that, for the same reason a probe that could not run raises
`ProbeNotTaken` instead of returning a zero.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .evidence import (
    EvidenceClaim,
    EvidenceError,
    EvidenceKind,
    Producer,
    Verdict,
)

__all__ = [
    "DifferentialError",
    "ProbeShape",
    "ShapedClaim",
    "probe_shape",
    "instrument_worked",
    "shaped_claims",
    "compare",
    "differential_claims",
    "main",
]


class DifferentialError(EvidenceError):
    """Raised when a differential is asked for that cannot mean anything."""


class ProbeShape(str, Enum):
    """The three shapes of runtime-probe claim, ranked by how well each
    distinguishes a dead hook from a dead probe.

    The order of declaration is the order of preference in :func:`compare`, and
    it is not arbitrary: identity is the only signal DFInsta emits itself, so it
    is the only one that cannot be broken by Instagram renaming a log line.
    """

    IDENTITY = "identity"
    DELTA = "delta"
    ABSENCE = "absence"


#: Most discriminating first. Used to pick which pair to judge when a hook has
#: results of more than one shape on both sides.
SHAPE_PREFERENCE: tuple[ProbeShape, ...] = (
    ProbeShape.IDENTITY,
    ProbeShape.DELTA,
    ProbeShape.ABSENCE,
)


def probe_shape(claim: EvidenceClaim) -> ProbeShape | None:
    """Which shape of probe produced this claim, read from what it recorded.

    Keyed on detail fields rather than on a stored label because the claims this
    has to read were written before this module existed. The three key sets are
    disjoint in `probes.py`, and a claim matching none of them returns ``None``
    rather than being forced into a shape — an unrecognised probe must not be
    compared as though it were a familiar one.
    """
    if claim.kind is not EvidenceKind.RUNTIME_PROBE:
        return None
    detail = claim.detail
    if "executed" in detail:
        return ProbeShape.IDENTITY
    if "control_found" in detail:
        return ProbeShape.ABSENCE
    if "requires_two_directional_delta" in detail:
        return ProbeShape.DELTA
    return None


def instrument_worked(claim: EvidenceClaim, shape: ProbeShape) -> bool:
    """Could this capture have shown the signal at all?

    The whole discrimination rests here. False means the capture is silent in a
    way that says nothing about the hook, so a difference from the baseline must
    not be reported as a regression.
    """
    detail = claim.detail
    if shape is ProbeShape.IDENTITY:
        # Any hook announcing itself proves `com.dfinstagram.probe` is loaded and
        # logging in this capture. Note this is true even when the hook under
        # examination is the one that stayed quiet — which is the point.
        return bool(detail.get("hooks_that_ran"))
    if shape is ProbeShape.ABSENCE:
        return bool(detail.get("control_found"))
    enabled = detail.get("enabled_observations", 0)
    disabled = detail.get("disabled_observations", 0)
    try:
        return (int(enabled) + int(disabled)) > 0
    except (TypeError, ValueError):
        # A non-numeric count is a malformed claim, not a working instrument.
        return False


@dataclass(frozen=True)
class ShapedClaim:
    """A runtime-probe claim with its shape worked out once."""

    claim: EvidenceClaim
    shape: ProbeShape

    @property
    def instrument_worked(self) -> bool:
        return instrument_worked(self.claim, self.shape)

    @property
    def attribution_shared(self) -> bool:
        """Was this downgraded because more than one hook fits the observation?

        `probes.attribute` sets this. A claim inconclusive for *that* reason has
        not measured a hook that stopped working; it has measured an observation
        that stopped being attributable, which is a different fact.
        """
        return self.claim.detail.get("attribution") == "shared"


def shaped_claims(claims: Iterable[EvidenceClaim]) -> dict[str, dict[ProbeShape, ShapedClaim]]:
    """Index runtime-probe claims by hook and shape, keeping the LAST of each.

    Last wins because the ledger is append-only and a later claim supersedes an
    earlier one — the same rule `readiness()` applies. Claims of other kinds are
    ignored rather than rejected, so a whole run's ledger can be handed in.
    """
    out: dict[str, dict[ProbeShape, ShapedClaim]] = {}
    for claim in claims:
        shape = probe_shape(claim)
        if shape is None:
            continue
        out.setdefault(claim.hook_id, {})[shape] = ShapedClaim(claim, shape)
    return out


def _claim(
    hook_id: str,
    verdict: Verdict,
    actor: str,
    summary: str,
    detail: Mapping[str, Any],
) -> EvidenceClaim:
    return EvidenceClaim(
        hook_id=hook_id,
        kind=EvidenceKind.DIFFERENTIAL,
        verdict=verdict,
        producer=Producer.DEVICE,
        actor=actor,
        summary=summary,
        detail=dict(detail),
    )


def compare(
    hook_id: str,
    baseline: Mapping[ProbeShape, ShapedClaim],
    current: Mapping[ProbeShape, ShapedClaim],
    *,
    baseline_version: str,
    current_version: str,
    actor: str,
) -> EvidenceClaim:
    """One differential claim for one hook, from the most decisive shape both sides have.

    Every branch below returns something. There is deliberately no fall-through
    to a default pass: a case nobody thought of must surface as an inconclusive
    with its reason written down, never as a hook advancing on silence.
    """
    base_detail: dict[str, Any] = {
        "baseline_version": baseline_version,
        "current_version": current_version,
        "baseline_shapes": sorted(shape.value for shape in baseline),
        "current_shapes": sorted(shape.value for shape in current),
    }

    if not baseline:
        return _claim(
            hook_id,
            Verdict.INCONCLUSIVE,
            actor,
            f"no runtime result for {hook_id} on {baseline_version}, so there is nothing "
            "to compare against. A first port has no differential; waive it at the gate "
            "rather than reading the absence as agreement.",
            {**base_detail, "comparison": "no_baseline"},
        )
    if not current:
        return _claim(
            hook_id,
            Verdict.INCONCLUSIVE,
            actor,
            f"no runtime result for {hook_id} on {current_version}. A measurement that was "
            "not taken is not a defect in the hook.",
            {**base_detail, "comparison": "no_current"},
        )

    shared = [shape for shape in SHAPE_PREFERENCE if shape in baseline and shape in current]
    if not shared:
        return _claim(
            hook_id,
            Verdict.INCONCLUSIVE,
            actor,
            f"{hook_id} was probed differently on the two versions "
            f"({', '.join(sorted(s.value for s in baseline))} on {baseline_version} vs "
            f"{', '.join(sorted(s.value for s in current))} on {current_version}), and "
            "results of different shapes are not comparable.",
            {**base_detail, "comparison": "shapes_disjoint"},
        )

    shape = shared[0]
    before = baseline[shape]
    after = current[shape]
    detail = {
        **base_detail,
        "shape": shape.value,
        "baseline_verdict": before.claim.verdict.value,
        "current_verdict": after.claim.verdict.value,
        "baseline_actor": before.claim.actor,
        "current_actor": after.claim.actor,
        "current_instrument_worked": after.instrument_worked,
        "also_compared": [item.value for item in shared[1:]],
    }

    if before.claim.verdict is not Verdict.PASSED:
        return _claim(
            hook_id,
            Verdict.INCONCLUSIVE,
            actor,
            f"the {baseline_version} {shape.value} result for {hook_id} was "
            f"{before.claim.verdict.value}, not a pass, so this version cannot be shown to "
            "have broken anything that was working.",
            {**detail, "comparison": "baseline_not_a_pass"},
        )

    if after.claim.verdict is Verdict.PASSED:
        return _claim(
            hook_id,
            Verdict.PASSED,
            actor,
            f"{hook_id} passed its {shape.value} probe on {baseline_version} and passes it "
            f"again on {current_version} — no port regression.",
            {**detail, "comparison": "held"},
        )

    if after.claim.verdict is Verdict.FAILED:
        return _claim(
            hook_id,
            Verdict.FAILED,
            actor,
            f"port regression: {hook_id} passed its {shape.value} probe on "
            f"{baseline_version} and FAILED it on {current_version}.",
            {**detail, "comparison": "regressed"},
        )

    # Everything below is a current verdict of inconclusive over a baseline pass:
    # the case this module exists to take apart.
    if after.attribution_shared:
        return _claim(
            hook_id,
            Verdict.INCONCLUSIVE,
            actor,
            f"{hook_id} passed on {baseline_version}, and on {current_version} the same "
            "observation fits more than one hook. Attribution was lost, not necessarily "
            "the behaviour — this cannot be read either way.",
            {**detail, "comparison": "attribution_lost"},
        )

    if not after.instrument_worked:
        return _claim(
            hook_id,
            Verdict.INCONCLUSIVE,
            actor,
            f"{hook_id} passed on {baseline_version} and its {shape.value} probe saw "
            f"nothing at all on {current_version} — in a capture that cannot be shown to "
            "have been able to see it. An inert hook and a probe whose signal no longer "
            "exists look identical here; the identity probe is what tells them apart.",
            {**detail, "comparison": "probe_went_blind"},
        )

    return _claim(
        hook_id,
        Verdict.FAILED,
        actor,
        f"port regression: {hook_id} passed its {shape.value} probe on {baseline_version}, "
        f"and on {current_version} the probe demonstrably still works but no longer shows "
        "this hook acting.",
        {**detail, "comparison": "regressed_instrument_live"},
    )


def differential_claims(
    baseline: Iterable[EvidenceClaim],
    current: Iterable[EvidenceClaim],
    *,
    baseline_version: str,
    current_version: str,
    actor: str,
    hook_ids: Sequence[str] | None = None,
    baseline_build: str | None = None,
    current_build: str | None = None,
) -> list[EvidenceClaim]:
    """One `DIFFERENTIAL` claim per hook, comparing two versions' runtime results.

    ``hook_ids`` fixes which hooks are expected. Without it the set is the union
    of what the two ledgers mention, which quietly omits a hook that vanished
    from both — pass the manifest's active hooks to get an explicit
    "no result on either version" instead of a shorter report.
    """
    if not str(baseline_version).strip() or not str(current_version).strip():
        raise DifferentialError(
            "both versions must be named; an unlabelled differential cannot be read later"
        )
    if str(baseline_version).strip() == str(current_version).strip():
        raise DifferentialError(
            f"refusing a differential of {current_version} against itself. Two builds of one "
            "version are byte-identical here, so the comparison is vacuous — and a vacuous "
            "'identical' in the ledger reads as evidence that a comparison happened."
        )
    if baseline_build is not None and baseline_build == current_build:
        raise DifferentialError(
            f"refusing a differential between two runs of the same build ({current_build}); "
            "labelling one of them a different version does not make the comparison mean "
            "anything."
        )
    if not actor.strip():
        raise DifferentialError(
            "a differential is a device-produced claim and must name the device that "
            "produced the current measurement"
        )

    before = shaped_claims(baseline)
    after = shaped_claims(current)
    names = list(hook_ids) if hook_ids is not None else sorted(set(before) | set(after))
    return [
        compare(
            hook_id,
            before.get(hook_id, {}),
            after.get(hook_id, {}),
            baseline_version=str(baseline_version).strip(),
            current_version=str(current_version).strip(),
            actor=actor,
        )
        for hook_id in names
    ]


# ----------------------------------------------------------------------- I/O


def read_claims(path: Path | str) -> list[EvidenceClaim]:
    """Every claim in an evidence JSONL, re-validated through `from_dict`."""
    path = Path(path)
    out: list[EvidenceClaim] = []
    with open(path, "r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                out.append(EvidenceClaim.from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError, ValueError) as error:
                raise DifferentialError(f"{path}:{number}: unreadable claim: {error}") from error
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--baseline", type=Path, required=True, help="version N-1 evidence JSONL")
    parser.add_argument("--baseline-version", required=True, help="e.g. 439")
    parser.add_argument("--current", type=Path, required=True, help="version N evidence JSONL")
    parser.add_argument("--current-version", required=True, help="e.g. 440")
    parser.add_argument(
        "--actor",
        required=True,
        help="the device that produced the current measurement, e.g. device:P3227J000775",
    )
    parser.add_argument("--baseline-build", help="sha256 of the N-1 APK, to refuse a self-compare")
    parser.add_argument("--current-build", help="sha256 of the N APK")
    parser.add_argument("--out", type=Path, help="append the claims to this JSONL")
    parser.add_argument("--json", action="store_true", help="print the claims as JSON")
    args = parser.parse_args(argv)

    try:
        claims = differential_claims(
            read_claims(args.baseline),
            read_claims(args.current),
            baseline_version=args.baseline_version,
            current_version=args.current_version,
            actor=args.actor,
            baseline_build=args.baseline_build,
            current_build=args.current_build,
        )
    except DifferentialError as error:
        print(f"error: {error}")
        return 2

    if args.out is not None:
        with open(args.out, "a", encoding="utf-8") as handle:
            for claim in claims:
                handle.write(json.dumps(claim.to_dict(), sort_keys=True) + "\n")

    if args.json:
        print(json.dumps([claim.to_dict() for claim in claims], indent=2, sort_keys=True))
    else:
        width = max((len(claim.hook_id) for claim in claims), default=1)
        for claim in claims:
            print(
                f"{claim.hook_id:{width}s}  {claim.verdict.value:12s} "
                f"{claim.detail.get('comparison', '')}"
            )
            print(f"{'':{width}s}  {claim.summary}")
    # A regression is the finding this exists to surface, so it must not be
    # reported through a zero exit that a script would ignore.
    return 1 if any(claim.verdict is Verdict.FAILED for claim in claims) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

"""Stage 5: resolve every hook in the manifest against one decoded APK.

This is the first caller of :mod:`dfinsta_pipeline.hook_manifest`. It joins the
three pieces that already existed separately — the Index (where classes are),
the manifest engine (what an anchor means), and the decode (the truth) — and
produces, per hook, either an operation the applier can run or a stated reason
it must escalate.

Two things here are load-bearing and both come from the same failure mode: a
stage that reports success it did not earn.

**Already-applied is not failure, and it outranks resolution.** Written naively
as ``if not resolution.resolved: fail()``, a second run over a decode this
pipeline already patched reports every hook broken. Worse, host search returns
several candidate classes for the Reels hooks, and on a patched decode the
*decoys* still resolve cleanly while the real host carries the marker. Ranking
"resolved" first would therefore patch a second, wrong class on every re-run.
So ``ALREADY_APPLIED`` beats ``RESOLVED``, and only the marker — which nothing
but this pipeline writes — can produce it.

**A half-patched decode is a hard stop.** A marker present but at the wrong
count means a previous run died mid-patch, and so does a marker found in two
different candidates. Neither is a state any other conclusion can be drawn
through, so both raise ``CONFLICT`` ahead of everything else rather than being
folded into "did not resolve".

Everything the stage decides is recorded per candidate, including the rejected
ones, because a gate needs to see what was considered and why it lost — not a
score.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .hook_index import HookIndex, IndexUnusable
from .hook_manifest import (
    Hook,
    HostFingerprint,
    ManifestError,
    Resolution,
    assert_distinct,
    load_manifest,
    resolve_in_source,
)


class Outcome(str, Enum):
    """What the stage concluded about one hook. Ordered by precedence, worst first."""

    CONFLICT = "conflict"  # decode is half-patched; no other conclusion is safe
    ALREADY_APPLIED = "already_applied"  # our marker is present at full count
    RESOLVED = "resolved"  # exactly one candidate matched; operation ready
    AMBIGUOUS = "ambiguous"  # several matched; the fingerprint does not discriminate
    UNRESOLVED = "unresolved"  # candidates existed, none matched the anchor
    NOT_FOUND = "not_found"  # no candidate host at all
    NEEDS_AGENT = "needs_agent"  # by_agent fingerprint with no proposal supplied

    @property
    def escalates(self) -> bool:
        """Does this outcome need a human or an agent before the port can continue?"""
        return self not in {Outcome.RESOLVED, Outcome.ALREADY_APPLIED}


@dataclass(frozen=True)
class CandidateReport:
    """One class that was considered as a host, and what came of it."""

    descriptor: str
    path: str
    found_by: str
    resolved: bool = False
    already_applied: bool = False
    marker_count: int | None = None
    occurrences: int = 0
    reason: str = ""
    bindings: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "descriptor": self.descriptor,
            "path": self.path,
            "found_by": self.found_by,
            "resolved": self.resolved,
            "already_applied": self.already_applied,
            # Serialised because a CONFLICT is decided by this number: without it
            # a gate would have to parse "1/2" out of the reason prose.
            "marker_count": self.marker_count,
            "occurrences": self.occurrences,
            "reason": self.reason,
            "bindings": dict(self.bindings),
        }


@dataclass(frozen=True)
class HostSearch:
    """Which classes one fingerprint proposed, and the evidence for proposing them."""

    kind: str
    candidates: tuple[str, ...]
    evidence: Mapping[str, Any]
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "candidates": list(self.candidates),
            "evidence": dict(self.evidence),
            "reason": self.reason,
        }


@dataclass
class HookResolution:
    """One hook resolved against one decode, with every candidate it considered."""

    hook_id: str
    outcome: Outcome
    reason: str = ""
    descriptor: str | None = None
    resolution: Resolution | None = None
    searches: tuple[HostSearch, ...] = ()
    candidates: tuple[CandidateReport, ...] = ()

    @property
    def escalates(self) -> bool:
        return self.outcome.escalates

    def as_operation(self, hook: Hook) -> dict[str, Any]:
        """The applier-shaped operation, only when there is one to run."""
        if self.outcome is not Outcome.RESOLVED or self.resolution is None:
            raise ManifestError(
                f"{self.hook_id}: no operation to emit ({self.outcome.value}: {self.reason})"
            )
        return self.resolution.as_operation(hook)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook_id,
            "outcome": self.outcome.value,
            "escalates": self.escalates,
            "reason": self.reason,
            "descriptor": self.descriptor,
            "searches": [search.to_dict() for search in self.searches],
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


@dataclass
class ResolveReport:
    """The whole manifest resolved against one decode."""

    decode: str
    index_decode: str
    index_content_hash: str
    resolutions: tuple[HookResolution, ...]

    @property
    def escalations(self) -> tuple[HookResolution, ...]:
        return tuple(item for item in self.resolutions if item.escalates)

    @property
    def complete(self) -> bool:
        """Every hook is either ready to apply or already applied.

        An empty resolution set is NOT complete. "Nothing escalated" is trivially
        true when nothing was resolved at all, and a manifest whose hooks are all
        retired would otherwise exit 0 and write an empty operation list — a
        build with no hooks applied, reported as a success.
        """
        return bool(self.resolutions) and not self.escalations

    def operations(self, hooks: Iterable[Hook]) -> list[dict[str, Any]]:
        """Operations for the hooks that resolved, skipping those already applied.

        Raises if any hook escalated: emitting a partial operation list would
        produce a build missing hooks nobody was told about.
        """
        by_id = {hook.hook_id: hook for hook in hooks}
        if self.escalations:
            names = ", ".join(item.hook_id for item in self.escalations)
            raise ManifestError(f"cannot emit operations while {names} still escalate")
        out = []
        for item in self.resolutions:
            if item.outcome is not Outcome.RESOLVED:
                continue
            hook = by_id.get(item.hook_id)
            if hook is None:
                raise ManifestError(
                    f"{item.hook_id} resolved but was not among the hooks passed to "
                    "operations(); the caller is holding a different manifest than the "
                    "one this report was produced from"
                )
            out.append(item.as_operation(hook))
        return out

    def to_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for item in self.resolutions:
            counts[item.outcome.value] = counts.get(item.outcome.value, 0) + 1
        return {
            "decode": self.decode,
            "index_decode": self.index_decode,
            "index_content_hash": self.index_content_hash,
            "complete": self.complete,
            "counts": counts,
            "resolutions": [item.to_dict() for item in self.resolutions],
        }


# --------------------------------------------------------------------- search


def search_hosts(
    hook: Hook,
    fingerprint: HostFingerprint,
    index: HookIndex,
    proposals: Sequence[str] = (),
) -> HostSearch:
    """Ask the index which classes this fingerprint points at, in this version."""
    if fingerprint.kind == "named":
        descriptor = fingerprint.descriptor
        assert descriptor is not None
        if index.has(descriptor):
            return HostSearch("named", (descriptor,), {"descriptor": descriptor})
        return HostSearch(
            "named",
            (),
            {"descriptor": descriptor},
            reason=(
                f"{descriptor} does not exist in this version. A stable named type that "
                "disappeared is a real change, not a lookup failure."
            ),
        )

    if fingerprint.kind == "by_literal":
        required = fingerprint.required_literals
        per_literal = {
            literal: len(index.descriptors_with_literal(literal)) for literal in required
        }
        missing = [
            literal for literal in required if not index.literal_is_indexed(literal)
        ]
        candidates = index.descriptors_with_all_literals(required)
        evidence: dict[str, Any] = {
            "literals": list(required),
            "classes_per_literal": per_literal,
            "co_located": len(candidates),
        }
        if missing:
            # Distinguishable from "the app dropped this endpoint": the index only
            # holds strings that look like API paths, so an unindexed literal is
            # usually a manifest authoring error.
            evidence["not_indexed"] = missing
            return HostSearch(
                "by_literal",
                candidates,
                evidence,
                reason=(
                    f"literal(s) {missing} are absent from the index. Either this version "
                    "dropped the endpoint, or the string is not API-path shaped and was "
                    "never indexed; the index cannot tell those apart."
                ),
            )
        if not candidates:
            return HostSearch(
                "by_literal",
                (),
                evidence,
                reason=(
                    "no single class contains all of "
                    f"{list(required)}; each exists but they are no longer co-located, "
                    "so the host must be re-established rather than guessed"
                ),
            )
        return HostSearch("by_literal", candidates, evidence)

    # by_agent: nothing mechanical points at the host, so a proposal must arrive
    # from outside. It is still checked against the index and then against the
    # decode, exactly like a mechanically-found candidate.
    known = tuple(descriptor for descriptor in proposals if index.has(descriptor))
    unknown = tuple(descriptor for descriptor in proposals if not index.has(descriptor))
    evidence = {"proposed": list(proposals), "not_in_index": list(unknown)}
    reason = ""
    if not proposals:
        reason = (
            f"{hook.hook_id} has no mechanical fingerprint; a proposed host is required"
        )
    elif unknown:
        reason = (
            f"proposed host(s) {list(unknown)} do not exist in this version. Obfuscated "
            "names are recycled, so a descriptor carried over from another version "
            "resolves to an unrelated class or to nothing."
        )
    return HostSearch("by_agent", known, evidence, reason=reason)


# ------------------------------------------------------------------- resolving


def resolve_hook(
    hook: Hook,
    index: HookIndex,
    decode: Path,
    proposals: Sequence[str] = (),
) -> HookResolution:
    """Resolve one hook: find candidate hosts, then check each against the decode."""
    decode = Path(decode)
    searches: list[HostSearch] = []
    ordered: list[tuple[str, str]] = []  # (descriptor, found_by)
    seen: set[str] = set()
    for fingerprint in hook.hosts:
        search = search_hosts(hook, fingerprint, index, proposals)
        searches.append(search)
        for descriptor in search.candidates:
            if descriptor not in seen:
                seen.add(descriptor)
                ordered.append((descriptor, search.kind))

    reports: list[CandidateReport] = []
    for descriptor, found_by in ordered:
        path = index.path_for(descriptor)
        if path is None:  # pragma: no cover - search only yields indexed descriptors
            reports.append(
                CandidateReport(descriptor, "", found_by, reason="no path in the index")
            )
            continue
        source = decode / path
        try:
            body = source.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            reports.append(
                CandidateReport(
                    descriptor,
                    path,
                    found_by,
                    reason=f"the index names {path} but the decode cannot read it: {error}",
                )
            )
            continue
        resolution = resolve_in_source(hook, descriptor, body)
        reports.append(
            CandidateReport(
                descriptor,
                path,
                found_by,
                resolved=resolution.resolved,
                already_applied=resolution.already_applied,
                marker_count=body.count(hook.marker),
                occurrences=resolution.occurrences,
                reason=resolution.reason,
                bindings=dict(resolution.bindings),
            )
        )

    return _classify(hook, searches, reports, index, decode)


def _classify(
    hook: Hook,
    searches: list[HostSearch],
    reports: list[CandidateReport],
    index: HookIndex,
    decode: Path,
) -> HookResolution:
    """Turn per-candidate results into one outcome, worst-first."""
    frozen_searches = tuple(searches)
    frozen_reports = tuple(reports)

    def make(outcome: Outcome, reason: str, descriptor: str | None = None) -> HookResolution:
        return HookResolution(
            hook.hook_id,
            outcome,
            reason=reason,
            descriptor=descriptor,
            searches=frozen_searches,
            candidates=frozen_reports,
        )

    # 1. A half-patched decode. Nothing else can be concluded through it.
    partial = [
        report
        for report in reports
        if report.marker_count
        and not report.already_applied
        and report.marker_count != hook.expected_marker_count
    ]
    if partial:
        detail = ", ".join(
            f"{report.descriptor} has {report.marker_count}/{hook.expected_marker_count}"
            for report in partial
        )
        return make(
            Outcome.CONFLICT,
            f"marker {hook.marker!r} is present at the wrong count ({detail}); a previous "
            "run patched this decode partially and it must be re-extracted",
        )

    applied = [report for report in reports if report.already_applied]
    if len(applied) > 1:
        names = ", ".join(report.descriptor for report in applied)
        return make(
            Outcome.CONFLICT,
            f"marker {hook.marker!r} is fully present in more than one class ({names}); "
            "this hook was applied twice and the decode must be re-extracted",
        )

    # 2. Already applied outranks resolved. On a re-run the real host carries the
    #    marker while the decoy candidates still match the anchor, so ranking
    #    "resolved" first would patch a second, wrong class every time.
    if applied:
        return make(
            Outcome.ALREADY_APPLIED,
            f"{applied[0].descriptor} already carries the patch",
            descriptor=applied[0].descriptor,
        )

    resolved = [report for report in reports if report.resolved]
    if len(resolved) == 1:
        winner = resolved[0]
        path = index.path_for(winner.descriptor)
        assert path is not None
        body = (decode / path).read_text(encoding="utf-8", errors="replace")
        resolution = resolve_in_source(hook, winner.descriptor, body)
        resolution.smali_path = path
        return HookResolution(
            hook.hook_id,
            Outcome.RESOLVED,
            reason=f"{winner.descriptor} matched the anchor exactly once",
            descriptor=winner.descriptor,
            resolution=resolution,
            searches=frozen_searches,
            candidates=frozen_reports,
        )

    if len(resolved) > 1:
        names = ", ".join(report.descriptor for report in resolved)
        return make(
            Outcome.AMBIGUOUS,
            f"the anchor matched in {len(resolved)} candidate classes ({names}); the "
            "fingerprint does not discriminate between them, so the host must be chosen "
            "with more evidence rather than by order",
        )

    if not reports:
        if any(search.kind == "by_agent" and not search.candidates for search in searches):
            reason = "; ".join(
                search.reason for search in searches if search.reason
            ) or "no host proposed"
            return make(Outcome.NEEDS_AGENT, reason)
        reason = "; ".join(search.reason for search in searches if search.reason)
        return make(Outcome.NOT_FOUND, reason or "no candidate host in this version")

    detail = "; ".join(f"{report.descriptor}: {report.reason}" for report in reports)
    return make(
        Outcome.UNRESOLVED,
        f"{len(reports)} candidate host(s) found, none matched the anchor — {detail}",
    )


def resolve_manifest(
    hooks: Iterable[Hook],
    index: HookIndex,
    decode: Path | str,
    proposals: Mapping[str, Sequence[str]] | None = None,
) -> ResolveReport:
    """Resolve every active hook against one decode.

    *proposals* maps ``hook_id`` to agent-proposed host descriptors, for the
    hooks no mechanical fingerprint can reach.
    """
    decode = Path(decode)
    index.assert_matches(decode)
    hooks = list(hooks)
    # Two hooks sharing a marker each report the other's patch as their own and
    # both drop out of the build, with nothing failing. Refuse the set up front.
    assert_distinct(hooks)
    proposals = proposals or {}
    resolutions = tuple(
        resolve_hook(hook, index, decode, proposals.get(hook.hook_id, ()))
        for hook in hooks
        if hook.status == "active"
    )
    return ResolveReport(
        decode=str(decode.resolve()),
        index_decode=index.decode_path,
        index_content_hash=index.content_hash,
        resolutions=resolutions,
    )


# ------------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("decode", type=Path, help="decoded APK directory")
    parser.add_argument("--index", type=Path, required=True, help="index directory")
    parser.add_argument(
        "--manifest", type=Path, default=Path("manifest/hooks.json"), help="hook manifest"
    )
    parser.add_argument(
        "--proposals",
        type=Path,
        help='JSON {"hook_id": ["LX/0Di2;"]} of agent-proposed hosts for by_agent hooks',
    )
    parser.add_argument("--json", type=Path, help="write the full report here")
    parser.add_argument(
        "--operations", type=Path, help="write applier operations here (only if complete)"
    )
    args = parser.parse_args(argv)

    hooks = load_manifest(args.manifest)
    try:
        index = HookIndex.for_decode(args.index, args.decode)
    except IndexUnusable as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    proposals = (
        json.loads(args.proposals.read_text(encoding="utf-8")) if args.proposals else {}
    )
    report = resolve_manifest(hooks, index, args.decode, proposals)

    for item in report.resolutions:
        mark = " " if not item.escalates else "!"
        target = item.descriptor or "-"
        print(f"{mark} {item.outcome.value:16s} {item.hook_id:38s} {target}")
        if item.escalates:
            print(f"    {item.reason}")

    if args.json:
        args.json.write_text(
            json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    if args.operations:
        if not report.complete:
            print(
                "refusing to write operations: "
                f"{len(report.escalations)} hook(s) still escalate",
                file=sys.stderr,
            )
            return 1
        args.operations.write_text(
            json.dumps(report.operations(hooks), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    return 0 if report.complete else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

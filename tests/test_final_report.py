"""Stage 11: the report that says what a port can be *shown* to have achieved.

`final_report.py` is the last stage of `pipeline_flowchart.md` and the only one
that had never been started, so until this file existed nothing in the tree
imported it. What makes it worth testing is not its size — it is 300 lines and
most of them are string formatting — but that it is the document a human reads
instead of reading the ledger, and a report is exactly the kind of code that can
be wrong in a direction nobody notices: too generous, too quiet, too tidy.

The properties pinned here are the ones where being wrong is invisible:

**It computes nothing about a hook.** :class:`LedgerAgreementTests` builds an
`EvidenceLedger` by hand and requires `build_report` to agree with it hook for
hook, escalation dict for escalation dict. The discriminating cases are the two a
re-derivation gets wrong: a `waived` item makes a hook ready even though nothing
measured it, and a `runtime_probe` that reached `passed` only after an
`inconclusive` does NOT make a hook ready even though its latest verdict is a
pass. Any reporter that decided readiness for itself — "ready iff every
post-build claim passed" is the obvious way to write it — passes the ordinary
cases and gets both of those backwards.

**Provenance decides what a hook owes.** :class:`ProvenanceTests`. The same
claims are release-ready under `already_applied` and escalating under
`mechanical`, because `requirements_for` differs. An unrecognised value must be
refused rather than quietly picking the smaller set, which is `requirements_for`'s
own stated rule and is reachable from the CLI by a typo.

**Version filtering is visible.** :class:`VersionFilteringTests`. A claim about
another port is excluded from readiness *and* named in `foreign_versions`, and a
claim with no version at all is included because absence means "recorded before
attribution existed", not "belongs to nobody". The positive control is the one
that matters: a report over only foreign claims must raise, because a
`PortReport` with no hooks has no escalations, and no escalations is `complete`
— an empty success that exits 0.

**The build-coverage line.** :class:`BuildCoverageTests`, the honesty-critical
part. One digest printed in a header reads as "all of this was measured against
that APK", and on 440 it is true of 7 claims out of 51: the device evidence names
a serial and the differential deliberately names none because it spans two
builds. Three states — no build, one build with its coverage, two or more with a
warning naming each — and the multi-build case must not quietly pick one.

**Undated evidence says so.** :class:`RecordedSpanTests`. The line is printed
with "(undated — claims predate attribution)" rather than omitted, the same
three-state discipline `verify_build` uses for `required_strings`: said-and-true,
said-and-false, never silently absent.

**Reading claims is refusal-shaped.** :class:`ReadClaimsTests`. A missing file
names the path instead of contributing nothing, because a report assembled from
three files of which one silently contributed zero reads as "this hook has no
runtime evidence" when the truth is "nobody looked in the right place".

**The exit code is the release gate.** :class:`CliTests`. Exit 1 when incomplete
is the whole interface for a script; a report that printed five escalations and
exited 0 would be worse than no report.

===============================================================================
  FIXTURE PROVENANCE
===============================================================================

`CLAIMS_440_*` is the real 440 port, distilled but not invented. Every hook id,
verdict, actor, summary, timestamp and digest below was copied out of the three
files the module's own docstring names:

* `work/440-attributed/evidence.jsonl` — 21 rows, 7 hooks x
  {anchor_unique, registers_safe, static_verified}, all `passed`, all stamped
  `2026-08-06T18:52:42Z`, the post-build ones naming build `742fee81…`.
* `manifest/runtime_evidence/440.jsonl` — 23 rows, three walkthroughs plus a
  launch probe and a Tigon delta probe, none carrying a version or a timestamp.
* `manifest/differentials/439-440.jsonl` — 7 rows, each carrying `version` and no
  build, because a differential spans two of them.

They are inlined rather than read: `work/` is gitignored, and a test that skips
when its fixture is absent stops running the moment the tree is cloned.
`tests/test_static_verified.py` made the same call for the same reason. When it
was copied in, `render(build_report("440", claims_440()))` was checked to be
**byte-identical** to the output of the real three-file invocation in the module
docstring. Nothing in the suite can re-check that — the inputs are not in the
repository — so :meth:`RealPortTests.
test_the_fixture_reproduces_the_report_the_real_files_give` states the four
numbers that pin it instead: 51 claims, 7 naming the build, 2 of 7 ready, exit 1.
An edit that drifts from the real report fails there rather than quietly
re-baselining every other assertion in the class.

`OTHER_BUILD` is also real: `work/440-clean/dfinsta.apk`, the second 440 artifact
of the same day. The two differ by exactly one ZIP timestamp on `classes21.dex`
and are byte-identical in every entry, which is the incident the module docstring
recounts and the reason the multi-build warning exists at all.

===============================================================================
  MUTATION RESULTS
===============================================================================

Ten mutations were re-attacked out of tree, one at a time against a fresh copy of
`src`, with the unmutated copy passing first as the control. Every one was
caught; the count in brackets is how many tests here failed:

* filter with `claim.version == version`, dropping unattributed claims [32] →
  :meth:`VersionFilteringTests.
  test_a_claim_with_no_version_is_included_because_it_predates_attribution`
  and the whole of :class:`RealPortTests`, because every runtime probe on 440 is
  unattributed and the port drops to 0 of 7 ready
* the multi-build branch prints `builds[0]` like the single-build branch [3] →
  :class:`BuildCoverageTests`
* `main` returns 0 unconditionally [6] → :meth:`CliTests.
  test_an_incomplete_report_exits_one_because_a_release_script_gates_on_it`
* `read_claims` returns `[]` for a missing file [3] → :class:`ReadClaimsTests`
  and :meth:`CliTests.
  test_a_missing_evidence_file_is_refused_on_stderr_with_exit_two`
* `claims_naming_a_build` counts every claim rather than those with a hash [7] →
  :meth:`BuildCoverageTests.
  test_the_coverage_counts_claims_that_name_the_build_not_all_claims`
* the undated line is omitted instead of printed [1] → :meth:`RecordedSpanTests.
  test_undated_evidence_says_so_rather_than_omitting_the_line`
* `foreign` is always empty, so exclusions go unreported [3] →
  :class:`VersionFilteringTests`
* the "not tied to any artifact" caveat is printed unconditionally [1] →
  :meth:`BuildCoverageTests.
  test_the_caveat_is_dropped_when_every_claim_names_the_build`
* `read_claims` returns `[]` rather than refusing an empty claim set [1] →
  :meth:`ReadClaimsTests.
  test_no_claims_at_all_is_refused_rather_than_reported_as_nothing_to_do`
* `ledger.report()` instead of `ledger.report(POST_BUILD)`, so the release phase
  re-asks the pre-apply kinds [17] → :class:`LedgerAgreementTests`

===============================================================================
  KNOWN GAPS
===============================================================================

:class:`KnownGapTests` pins three defects found by writing this file. Each
records what the module does today, so a fix fails loudly here instead of
silently changing what a release script is told.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import Iterable, Sequence

from dfinsta_pipeline import final_report
from dfinsta_pipeline.evidence import (
    ALREADY_APPLIED_REQUIREMENTS,
    MECHANICAL_REQUIREMENTS,
    POST_BUILD,
    PRE_APPLY,
    EvidenceClaim,
    EvidenceError,
    EvidenceKind,
    EvidenceLedger,
    Subject,
    Verdict,
    requirements_for,
    waiver,
)
from dfinsta_pipeline.final_report import (
    PortReport,
    ReportError,
    build_report,
    read_claims,
    render,
)
from tests.test_evidence import claim_for

# --------------------------------------------------------------- the real 440 run

VERSION = "440"

#: `work/440-attributed/dfinsta.apk`, the artifact the 440 device evidence was
#: taken against and the digest its `static_verified` claims name.
BUILD = "742fee81f77bf0e48e73ce2e4402bf90ad8fa9dbd5108adceb39dcf331bcc285"

#: `work/440-clean/dfinsta.apk`. A different 440 build of the same day, from the
#: same sources — the two differ by one ZIP timestamp on `classes21.dex` and
#: nothing else. It is here because "the digests differ" and "the APKs differ"
#: are not the same statement, and the report's job is to make the argument
#: rather than to skip the check.
OTHER_BUILD = "017219734fccce90154ea3096847ae65e0d6bde07bf621b2d4263c9a5082470c"

STAMP = "2026-08-06T18:52:42Z"
DEVICE = "device:P3227J000775"
VERIFIER = "tools/verify/verify_build.py"
RESOLVER = "dfinsta_pipeline.resolve"
VALIDATOR = "tools/resolver/validate_candidates.py"

#: The seven hooks of the 440 manifest, in the order `work/440-attributed`
#: recorded them. `build_report` sorts, so this order exists only to keep the
#: fixture readable against the real file.
HOOKS = (
    "set_app_context",
    "tigon_url_block",
    "replace_reels_discover_endpoint",
    "replace_reels_homecoming_endpoint",
    "replace_reels_stream_endpoint",
    "install_settings_long_click",
    "install_settings_long_click_actionbar",
)

#: The two hooks the real report calls release-ready. Named rather than derived,
#: so a fixture edit that makes five hooks pass fails instead of re-baselining.
READY_440 = ("set_app_context", "tigon_url_block")

NOT_ANNOUNCED = (
    " never announced execution. Its site may not have been reached by this "
    "walkthrough, or the patch may be inert; these are different and this run "
    "cannot tell them apart."
)
ANNOUNCED = " announced its own execution"
SHAPES_DISJOINT = (
    " was probed differently on the two versions ({} on 439 vs identity on 440), "
    "and results of different shapes are not comparable. No 439 result for it was "
    "a pass either, so there would have been nothing to regress from even if the "
    "shapes had lined up."
)

#: `(hook, summary)` for the four pre-apply rows that differ per hook. The
#: descriptors are the real resolved hosts of the 440 port.
ANCHORS_440 = {
    "set_app_context": "Lcom/instagram/app/InstagramAppShell;",
    "tigon_url_block": "Lcom/instagram/api/tigon/TigonServiceLayer;",
    "replace_reels_discover_endpoint": "LX/4Sj;",
    "replace_reels_homecoming_endpoint": "LX/4Sj;",
    "replace_reels_stream_endpoint": "LX/4Sj;",
    "install_settings_long_click": "LX/DVk;",
    "install_settings_long_click_actionbar": "LX/DHo;",
}

REGISTERS_440 = {
    "set_app_context": "payload writes no register",
    "tigon_url_block": "payload writes no register",
    "replace_reels_discover_endpoint": "no conflicting read found in the following window",
    "replace_reels_homecoming_endpoint": "no conflicting read found in the following window",
    "replace_reels_stream_endpoint": "no conflicting read found in the following window",
    "install_settings_long_click": "all written registers are rewritten before any read",
    "install_settings_long_click_actionbar": (
        "no conflicting read found in the following window"
    ),
}

#: The 23 runtime probe rows, in file order. Per-hook order is what matters —
#: `install_settings_long_click` is `inconclusive` then `passed` twice, which is
#: the retry the ledger flags and the only reason that hook escalates on a kind
#: whose latest verdict is a pass.
PROBES_440: tuple[tuple[str, Verdict, str], ...] = (
    ("set_app_context", Verdict.PASSED, "started and stayed foreground with no linkage "
     "error, in a capture that demonstrably covers this app starting"),
    ("set_app_context", Verdict.PASSED, "set_app_context" + ANNOUNCED),
    ("tigon_url_block", Verdict.PASSED, "tigon_url_block" + ANNOUNCED),
    ("replace_reels_discover_endpoint", Verdict.INCONCLUSIVE,
     "replace_reels_discover_endpoint" + NOT_ANNOUNCED),
    ("replace_reels_homecoming_endpoint", Verdict.INCONCLUSIVE,
     "replace_reels_homecoming_endpoint" + NOT_ANNOUNCED),
    ("replace_reels_stream_endpoint", Verdict.PASSED,
     "replace_reels_stream_endpoint" + ANNOUNCED),
    ("install_settings_long_click", Verdict.INCONCLUSIVE,
     "install_settings_long_click" + NOT_ANNOUNCED),
    ("install_settings_long_click_actionbar", Verdict.INCONCLUSIVE,
     "install_settings_long_click_actionbar" + NOT_ANNOUNCED),
    ("tigon_url_block", Verdict.PASSED,
     "feed_tab: 'java.io.IOException: Blocked by DFInsta setting' present 10 time(s) "
     "enabled and 0 disabled — a two-directional delta"),
    ("set_app_context", Verdict.PASSED, "set_app_context" + ANNOUNCED),
    ("tigon_url_block", Verdict.PASSED, "tigon_url_block" + ANNOUNCED),
    ("replace_reels_discover_endpoint", Verdict.INCONCLUSIVE,
     "replace_reels_discover_endpoint" + NOT_ANNOUNCED),
    ("replace_reels_homecoming_endpoint", Verdict.INCONCLUSIVE,
     "replace_reels_homecoming_endpoint" + NOT_ANNOUNCED),
    ("replace_reels_stream_endpoint", Verdict.PASSED,
     "replace_reels_stream_endpoint" + ANNOUNCED),
    ("install_settings_long_click", Verdict.PASSED,
     "install_settings_long_click" + ANNOUNCED),
    ("install_settings_long_click_actionbar", Verdict.INCONCLUSIVE,
     "install_settings_long_click_actionbar" + NOT_ANNOUNCED),
    ("set_app_context", Verdict.PASSED, "set_app_context" + ANNOUNCED),
    ("tigon_url_block", Verdict.PASSED, "tigon_url_block" + ANNOUNCED),
    ("replace_reels_discover_endpoint", Verdict.INCONCLUSIVE,
     "replace_reels_discover_endpoint" + NOT_ANNOUNCED),
    ("replace_reels_homecoming_endpoint", Verdict.INCONCLUSIVE,
     "replace_reels_homecoming_endpoint" + NOT_ANNOUNCED),
    ("replace_reels_stream_endpoint", Verdict.PASSED,
     "replace_reels_stream_endpoint" + ANNOUNCED),
    ("install_settings_long_click", Verdict.PASSED,
     "install_settings_long_click" + ANNOUNCED),
    ("install_settings_long_click_actionbar", Verdict.INCONCLUSIVE,
     "install_settings_long_click_actionbar" + NOT_ANNOUNCED),
)

#: The 7 differential rows. Two passed, five `inconclusive` because the two
#: versions were probed in different shapes.
DIFFERENTIALS_440: tuple[tuple[str, Verdict, str], ...] = (
    ("install_settings_long_click", Verdict.INCONCLUSIVE,
     "install_settings_long_click" + SHAPES_DISJOINT.format("absence")),
    ("install_settings_long_click_actionbar", Verdict.INCONCLUSIVE,
     "install_settings_long_click_actionbar" + SHAPES_DISJOINT.format("absence")),
    ("replace_reels_discover_endpoint", Verdict.INCONCLUSIVE,
     "replace_reels_discover_endpoint" + SHAPES_DISJOINT.format("delta")),
    ("replace_reels_homecoming_endpoint", Verdict.INCONCLUSIVE,
     "replace_reels_homecoming_endpoint" + SHAPES_DISJOINT.format("delta")),
    ("replace_reels_stream_endpoint", Verdict.INCONCLUSIVE,
     "replace_reels_stream_endpoint" + SHAPES_DISJOINT.format("delta")),
    ("set_app_context", Verdict.PASSED,
     "set_app_context passed its absence probe on 439 and passes it again on 440 "
     "— no port regression."),
    ("tigon_url_block", Verdict.PASSED,
     "tigon_url_block passed its delta probe on 439 and passes it again on 440 "
     "— no port regression."),
)


# ------------------------------------------------------------------- claim makers


def anchor(hook: str, **extra: object) -> EvidenceClaim:
    return claim_for(
        hook,
        EvidenceKind.ANCHOR_UNIQUE,
        actor=RESOLVER,
        summary=f"{ANCHORS_440.get(hook, 'LX/0Fake;')} matched the anchor exactly once",
        version=VERSION,
        recorded_at=STAMP,
        **extra,
    )


def registers(hook: str, **extra: object) -> EvidenceClaim:
    return claim_for(
        hook,
        EvidenceKind.REGISTERS_SAFE,
        actor=VALIDATOR,
        summary=REGISTERS_440.get(hook, "payload writes no register"),
        version=VERSION,
        recorded_at=STAMP,
        **extra,
    )


def static(
    hook: str,
    verdict: Verdict = Verdict.PASSED,
    *,
    summary: str | None = None,
    **extra: object,
) -> EvidenceClaim:
    """A `static_verified` claim: the only 440 kind that names an artifact."""
    extra.setdefault("build_sha256", BUILD)
    extra.setdefault("version", VERSION)
    extra.setdefault("recorded_at", STAMP)
    return claim_for(
        hook,
        EvidenceKind.STATIC_VERIFIED,
        verdict,
        actor=VERIFIER,
        summary=summary or f"{hook}: 2 DFInsta symbol(s) present in the built DEX",
        **extra,
    )


def probe(
    hook: str,
    verdict: Verdict = Verdict.PASSED,
    *,
    summary: str | None = None,
    **extra: object,
) -> EvidenceClaim:
    """A device probe: no version, no timestamp, no build — exactly as recorded.

    `manifest/runtime_evidence/440.jsonl` predates attribution, so every row in it
    reaches `build_report` with `version=None`. That is not an oversight in the
    fixture; it is the case the version filter has to get right.
    """
    return claim_for(
        hook,
        EvidenceKind.RUNTIME_PROBE,
        verdict,
        actor=DEVICE,
        summary=summary or f"{hook}{ANNOUNCED}",
        **extra,
    )


def differential(
    hook: str,
    verdict: Verdict = Verdict.PASSED,
    *,
    summary: str | None = None,
    **extra: object,
) -> EvidenceClaim:
    """A differential: carries a version, never a build, because it spans two."""
    extra.setdefault("version", VERSION)
    return claim_for(
        hook,
        EvidenceKind.DIFFERENTIAL,
        verdict,
        actor=DEVICE,
        summary=summary or f"{hook} did not regress from 439",
        **extra,
    )


def complete_hook(hook: str, **extra: object) -> list[EvidenceClaim]:
    """The three post-build kinds, all passing. The shape of a shippable hook."""
    return [static(hook, **extra), probe(hook), differential(hook)]


# ---------------------------------------------------------------- the 440 fixture


def run_claims_440() -> list[EvidenceClaim]:
    """`work/440-attributed/evidence.jsonl`: 21 rows, versioned and stamped."""
    rows: list[EvidenceClaim] = []
    for hook in HOOKS:
        rows.extend([anchor(hook), registers(hook), static(hook)])
    return rows


def device_claims_440() -> list[EvidenceClaim]:
    """`manifest/runtime_evidence/440.jsonl`: 23 rows, unattributed."""
    return [probe(hook, verdict, summary=text) for hook, verdict, text in PROBES_440]


def differential_claims_440() -> list[EvidenceClaim]:
    """`manifest/differentials/439-440.jsonl`: 7 rows, versioned, no build."""
    return [
        differential(hook, verdict, summary=text)
        for hook, verdict, text in DIFFERENTIALS_440
    ]


def claims_440() -> list[EvidenceClaim]:
    """All 51, in the order the three `--evidence` flags supply them."""
    return run_claims_440() + device_claims_440() + differential_claims_440()


# ------------------------------------------------------------------- test helpers


def ledger_over(
    claims: Iterable[EvidenceClaim],
    *,
    provenance: dict[str, str] | None = None,
    default: str = "mechanical",
) -> EvidenceLedger:
    """The same claims in a hand-built ledger, for comparison with `build_report`.

    Deliberately written out here rather than obtained from the module under
    test: a test that asked `final_report` for the ledger it used could not tell
    delegation from a second opinion that happens to agree today.
    """
    ledger = EvidenceLedger()
    for hook in sorted({claim.hook_id for claim in claims}):
        ledger.register(Subject(hook, (provenance or {}).get(hook, default)))
    for claim in claims:
        ledger.record(claim)
    return ledger


def reasons_by_hook(report: PortReport) -> dict[str, list[str]]:
    return {item["hook_id"]: list(item["reasons"]) for item in report.escalations}


class ReportTestCase(unittest.TestCase):
    """A temp directory, a JSONL writer and a way to run `main` in-process."""

    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)

    def write(self, name: str, claims: Iterable[EvidenceClaim]) -> Path:
        path = self.tmp / name
        path.write_text(
            "".join(
                json.dumps(claim.to_dict(), sort_keys=True) + "\n" for claim in claims
            ),
            encoding="utf-8",
        )
        return path

    def run_main(self, *args: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = final_report.main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def files_440(self) -> list[str]:
        """The three `--evidence` arguments of the real invocation."""
        return [
            "--evidence", str(self.write("evidence.jsonl", run_claims_440())),
            "--evidence", str(self.write("runtime.jsonl", device_claims_440())),
            "--evidence", str(self.write("differential.jsonl", differential_claims_440())),
        ]


# ============================================================ it computes nothing


class LedgerAgreementTests(unittest.TestCase):
    """Every verdict is `EvidenceLedger.report(POST_BUILD)`'s, and nothing else's.

    A reporter that re-derived readiness would be a second opinion on the one
    question the ledger exists to answer, and the two would agree until one was
    edited. These tests are the "until": each compares the report against a
    ledger built by hand from the same claims.
    """

    def test_the_escalation_objects_are_the_ledgers_own_dicts(self):
        """Not "equivalent to" — identical, key for key, including `statuses`.

        The strongest available statement of delegation. `build_report` stores
        `report["escalations"]` verbatim, so a human reading an escalation sees
        the ledger's own `catches` text, actor and per-kind verdicts rather than
        a reporter's summary of them. Anything reformatted here would be a second
        vocabulary for a gate to learn.
        """
        claims = complete_hook("ready_hook") + [
            static("sad_hook", Verdict.FAILED, summary="a symbol was missing"),
            probe("sad_hook"),
            differential("sad_hook"),
        ]

        report = build_report(VERSION, claims)
        expected = ledger_over(claims).report(POST_BUILD)

        self.assertEqual(list(report.escalations), expected["escalations"])
        self.assertEqual(report.complete, expected["complete"])
        self.assertEqual(
            sorted(report.ready) + sorted(item["hook_id"] for item in report.escalations),
            sorted(expected["hooks"]),
        )

    def test_ready_is_exactly_the_hooks_the_ledger_calls_ready(self):
        """Verdict for verdict, over a mixture, in one assertion.

        `ready` is computed as "every hook not in the escalation list", which is
        only the same thing as the ledger's own `ready` flag while the ledger
        escalates precisely the not-ready hooks. Two derivations of one fact, so
        one test holds them together.
        """
        claims = (
            complete_hook("ready_hook")
            + [static("no_probe"), differential("no_probe")]
            + [static("bad_diff"), probe("bad_diff"),
               differential("bad_diff", Verdict.FAILED, summary="regressed from 439")]
        )

        report = build_report(VERSION, claims)
        ledger = ledger_over(claims)

        self.assertEqual(
            {hook: hook in report.ready for hook in report.hooks},
            {hook: ledger.readiness(hook, POST_BUILD).ready for hook in report.hooks},
        )
        self.assertEqual(report.ready, ("ready_hook",))

    def test_a_failed_claim_escalates_with_the_ledgers_wording(self):
        """`failed` is a measured negative, and the reason quotes the summary.

        The summary is the only place the *specific* finding survives — which
        symbol was absent, which endpoint moved the wrong way. A reason that said
        "static_verified: failed" and stopped would send a reader back to the
        artifact to find out what happened.
        """
        claims = [
            static(
                "sad_hook",
                Verdict.FAILED,
                summary="sad_hook: missing classes3.dex Lcom/dfinstagram/probe; h_sad",
            ),
            probe("sad_hook"),
            differential("sad_hook"),
        ]

        report = build_report(VERSION, claims)
        ledger = ledger_over(claims)

        self.assertEqual(report.ready, ())
        self.assertEqual(
            reasons_by_hook(report)["sad_hook"],
            list(ledger.readiness("sad_hook", POST_BUILD).reasons),
        )
        self.assertEqual(
            reasons_by_hook(report)["sad_hook"],
            [
                "static_verified: failed — sad_hook: missing classes3.dex "
                "Lcom/dfinstagram/probe; h_sad"
            ],
        )

    def test_a_human_waiver_makes_a_hook_ready_though_nothing_measured_it(self):
        """The case a re-derivation gets wrong in the generous direction.

        `Verdict.WAIVED.satisfies` is True: a human at a gate may decide to
        proceed without an item, and that decision is recorded rather than
        hidden. A reporter checking "did every post-build claim pass" would
        escalate this hook and quietly overrule the gate — the report would
        disagree with the ledger about a decision a person actually made.
        """
        waived = replace(
            waiver(
                "waived_hook",
                EvidenceKind.RUNTIME_PROBE,
                decision_id="dec-2026-08-06-01",
                actor="sam@dfinsta",
                rationale="the surface needs a second account; shipping on the "
                "differential and the static proof",
            ),
            version=VERSION,
        )
        claims = [static("waived_hook"), waived, differential("waived_hook")]

        report = build_report(VERSION, claims)

        self.assertEqual(report.ready, ("waived_hook",))
        self.assertEqual(report.escalations, ())
        self.assertIs(report.complete, True)
        # The ledger agrees, and the waiver is visibly a waiver rather than a pass.
        status = {
            item.kind: item
            for item in ledger_over(claims).readiness("waived_hook", POST_BUILD).statuses
        }
        self.assertIs(status[EvidenceKind.RUNTIME_PROBE].verdict, Verdict.WAIVED)

    def test_a_probe_that_went_green_on_a_retry_is_not_ready(self):
        """The case a re-derivation gets wrong in the permissive direction.

        The latest verdict is `passed`. "Re-run it until it goes green" is the
        obvious way to defeat a ledger, so `recovered_from_failure` withholds
        readiness and sends the sequence to a human — and this is the real shape
        of `install_settings_long_click` on 440, not a hypothetical.
        """
        claims = [
            static("retried_hook"),
            probe("retried_hook", Verdict.INCONCLUSIVE, summary="never announced"),
            probe("retried_hook", Verdict.PASSED),
            differential("retried_hook"),
        ]

        report = build_report(VERSION, claims)

        self.assertEqual(report.ready, ())
        reason = reasons_by_hook(report)["retried_hook"][0]
        self.assertIn("reached passed only after a failure (2 attempts)", reason)
        self.assertEqual(
            reasons_by_hook(report)["retried_hook"],
            list(ledger_over(claims).readiness("retried_hook", POST_BUILD).reasons),
        )
        # The control: without the earlier inconclusive, the same hook is ready.
        clean = [claim for claim in claims if claim.verdict is not Verdict.INCONCLUSIVE]
        self.assertEqual(build_report(VERSION, clean).ready, ("retried_hook",))

    def test_a_hook_with_no_claims_of_a_kind_escalates_as_not_exercised(self):
        """Absence is never a pass, and the report must not launder it into one.

        A hook that reaches the report with two of three post-build kinds has not
        been shown to work; it has been shown to have been looked at twice.
        """
        claims = [static("half_done"), probe("half_done")]

        report = build_report(VERSION, claims)

        self.assertEqual(report.ready, ())
        self.assertEqual(len(report.escalations), 1)
        self.assertIn("differential: no claim recorded", reasons_by_hook(report)["half_done"][0])

    def test_the_phase_is_post_build_so_a_pre_apply_gap_does_not_escalate(self):
        """`report(POST_BUILD)`, not `report()`. Deliberate, and worth pinning.

        `anchor_unique` and `registers_safe` are required of a mechanical hook at
        the release phase, and the 440 run has both — but they are pre-apply
        facts that gate the *apply*, and a final report that re-asked them would
        be reporting on a decision two stages upstream. What this file must never
        do is drop a post-build kind, so the control is the other half: the same
        hook missing `differential` escalates.
        """
        claims = complete_hook("post_build_only")
        self.assertEqual(build_report(VERSION, claims).ready, ("post_build_only",))
        self.assertEqual(
            ledger_over(claims).report(PRE_APPLY)["escalations"][0]["hook_id"],
            "post_build_only",
        )

        without = [claim for claim in claims if claim.kind is not EvidenceKind.DIFFERENTIAL]
        self.assertEqual(build_report(VERSION, without).ready, ())

    def test_confidence_moves_no_verdict_here_either(self):
        """The ledger records confidence and never reads it; nor may the report.

        Pinned at this layer too because a report is exactly where a number
        between 0 and 1 becomes tempting to render, and rendering it is one edit
        away from ranking on it.
        """
        low = complete_hook("scored_hook")
        low = [replace(claim, confidence=0.01) for claim in low]
        high = [replace(claim, confidence=1.0) for claim in complete_hook("scored_hook")]

        self.assertEqual(build_report(VERSION, low).ready, ("scored_hook",))
        self.assertEqual(build_report(VERSION, high).ready, ("scored_hook",))
        self.assertNotIn("confidence", render(build_report(VERSION, low)))


# ================================================================== provenance


class ProvenanceTests(unittest.TestCase):
    """What a hook owes follows from how this run resolved it, never from a guess."""

    def test_the_three_provenances_genuinely_require_different_kinds(self):
        """The premise. Without it every test below would pass vacuously.

        `already_applied` drops `registers_safe` — liveness cannot be re-derived
        once the payload is in place — and `agent` adds the two kinds that exist
        to corroborate a proposal. All three keep the post-build kinds in full.
        """
        mechanical = requirements_for("mechanical")
        already = requirements_for("already_applied")
        agent = requirements_for("agent")

        self.assertNotEqual(mechanical, already)
        self.assertNotEqual(mechanical, agent)
        self.assertEqual(mechanical - already, {EvidenceKind.REGISTERS_SAFE})
        self.assertEqual(
            agent - mechanical,
            {EvidenceKind.ADVERSARIAL_VERIFIED, EvidenceKind.PROPOSER_AGREEMENT},
        )

    def test_the_same_claims_give_different_readiness_under_two_provenances(self):
        """One claim set, two answers, and the difference is `requirements_for`.

        A mechanically resolved hook owes `registers_safe`; an already-applied one
        cannot produce it. The report must not hold a re-run to a standard the
        re-run makes unsatisfiable, and must not relax it for a hook this run
        genuinely applied. Pre-apply kinds only reach the *release* phase, so the
        difference is shown against the ledger's release report — the post-build
        answers are identical, which is itself the point: what changes is the
        requirement set, not the reporter's opinion.
        """
        claims = [static("h"), probe("h"), differential("h"), anchor("h")]

        mechanical = ledger_over(claims, default="mechanical")
        already = ledger_over(claims, default="already_applied")

        self.assertIs(mechanical.report()["complete"], False)
        self.assertIs(already.report()["complete"], True)
        self.assertEqual(
            [kind.value for kind in mechanical.readiness("h").missing], ["registers_safe"]
        )
        # And `build_report` carries the choice through to the post-build phase,
        # where both are complete because both require all three post-build kinds.
        self.assertIs(build_report(VERSION, claims).complete, True)
        self.assertIs(
            build_report(
                VERSION, claims, default_provenance="already_applied"
            ).complete,
            True,
        )

    def test_a_missing_post_build_kind_escalates_under_every_provenance(self):
        """The post-build kinds are the same three whatever the provenance.

        `ALREADY_APPLIED_REQUIREMENTS` relaxes a pre-apply item and nothing else,
        so "the decode already had the patch" can never become a route to
        shipping something the device never saw. This is the property that makes
        `--provenance` safe to expose at all.
        """
        claims = [static("h"), differential("h")]  # no runtime probe

        for kind in ("mechanical", "already_applied"):
            with self.subTest(provenance=kind):
                report = build_report(VERSION, claims, default_provenance=kind)
                self.assertEqual(report.ready, ())
                self.assertIn(
                    "runtime_probe: no claim recorded", reasons_by_hook(report)["h"][0]
                )
        self.assertIn(EvidenceKind.RUNTIME_PROBE, ALREADY_APPLIED_REQUIREMENTS)
        self.assertIn(EvidenceKind.RUNTIME_PROBE, MECHANICAL_REQUIREMENTS)

    def test_the_override_applies_to_one_hook_and_not_its_neighbours(self):
        """`--provenance` is per hook because a run mixes them.

        A re-run resolves some hooks from the decode's existing markers and
        applies others afresh. An override that leaked across hooks would relax
        the requirement for a hook this run really did patch.
        """
        claims = [
            anchor("applied"), static("applied"), probe("applied"), differential("applied"),
            static("rerun"), probe("rerun"), differential("rerun"), anchor("rerun"),
        ]

        ledger = ledger_over(claims, provenance={"rerun": "already_applied"})

        self.assertEqual(
            [kind.value for kind in ledger.readiness("applied").missing], ["registers_safe"]
        )
        self.assertEqual(ledger.readiness("rerun").missing, ())

    def test_an_unknown_provenance_is_refused_rather_than_defaulted(self):
        """`requirements_for`'s own rule, reachable from a CLI flag.

        "An unrecognised provenance must not silently pick the smaller
        requirement set." A typo in `--provenance hook=mechnical` that fell back
        to the four-kind set would relax a requirement by misspelling a word.
        """
        claims = complete_hook("h")

        for bad in ("mechnical", "MECHANICAL", "", "already applied", "human"):
            with self.subTest(provenance=bad):
                with self.assertRaises(EvidenceError) as caught:
                    build_report(VERSION, claims, provenance={"h": bad})
                self.assertIn("unknown provenance", str(caught.exception))

    def test_an_unknown_default_provenance_is_refused_too(self):
        """The same rule on the other door into the same argument.

        `default_provenance` is a keyword rather than a flag today, so nothing in
        the CLI can reach it — which is exactly why it needs a test: a future
        `--default-provenance` would otherwise inherit whatever this does.
        """
        with self.assertRaises(EvidenceError):
            build_report(VERSION, complete_hook("h"), default_provenance="whatever")

    def test_the_default_is_mechanical_because_seven_of_seven_resolve_that_way(self):
        """Pinned so a change of default is a deliberate, visible act.

        Every hook of 430, 439 and 440 resolves mechanically. Defaulting to
        `already_applied` would silently drop `registers_safe` from every report;
        defaulting to `agent` cannot work at all (see :class:`KnownGapTests`).
        """
        claims = [static("h"), probe("h"), differential("h"), anchor("h")]
        self.assertEqual(
            ledger_over(claims).readiness("h").missing,
            ledger_over(claims, default="mechanical").readiness("h").missing,
        )
        self.assertEqual(
            [kind.value for kind in ledger_over(claims).readiness("h").missing],
            ["registers_safe"],
        )


class ProvenanceCliTests(ReportTestCase):
    """`--provenance HOOK=KIND`, parsed and refused at the boundary."""

    def test_a_pair_with_no_equals_sign_is_refused_with_exit_two(self):
        """Parsed, not guessed. `--provenance mechanical` names no hook.

        Treating the whole token as a hook id would register a subject nothing
        has claims about, and the flag would appear to have been honoured.
        """
        code, stdout, stderr = self.run_main(
            "--version", VERSION, *self.files_440(), "--provenance", "mechanical"
        )

        self.assertEqual(code, 2)
        self.assertTrue(stderr.startswith("refused: "), stderr)
        self.assertIn("wants HOOK=KIND", stderr)
        self.assertEqual(stdout, "")

    def test_a_garbled_kind_is_refused_with_exit_two(self):
        """The refusal has to reach the process boundary, not just the library.

        `build_report` raises `EvidenceError`; `main` has to catch it and turn it
        into `refused:` and a 2. A traceback would also be non-zero, but it exits
        1 in most shells and would read to a release script as "incomplete".
        """
        code, stdout, stderr = self.run_main(
            "--version", VERSION, *self.files_440(),
            "--provenance", "set_app_context=mechnical",
        )

        self.assertEqual(code, 2)
        self.assertIn("refused: ", stderr)
        self.assertIn("unknown provenance 'mechnical'", stderr)
        self.assertEqual(stdout, "")

    def test_a_valid_override_changes_nothing_post_build_and_is_accepted(self):
        """The positive control for the two refusals above.

        Without it, "exit 2 on a bad value" would be satisfied by a flag that
        refuses every value. `already_applied` is legal, so the run completes —
        and reports the same 2 of 7, because the kind it relaxes is pre-apply.
        """
        code, stdout, _ = self.run_main(
            "--version", VERSION, *self.files_440(),
            "--provenance", "set_app_context=already_applied",
        )

        self.assertEqual(code, 1)  # still 5 escalations, for post-build reasons
        self.assertIn("RELEASE-READY      2 of 7", stdout)


# ============================================================== version filtering


class VersionFilteringTests(unittest.TestCase):
    """Claims about another port are excluded from readiness and named, not dropped."""

    def test_a_foreign_claim_confers_no_readiness(self):
        """The whole reason `version` was added to a claim in the first place.

        Until 2026-08-06 the version of a claim was knowable only from the
        filename a human chose — in the path, not the data — so a 439 runtime
        probe combined into a 440 report would have satisfied 440's requirement.
        """
        claims = [
            static("h"),
            probe("h", version="439"),
            differential("h"),
        ]

        report = build_report(VERSION, claims)

        self.assertEqual(report.ready, ())
        self.assertIn("runtime_probe: no claim recorded", reasons_by_hook(report)["h"][0])
        # The control: the identical probe attributed to 440 makes the hook ready.
        native = [static("h"), probe("h", version=VERSION), differential("h")]
        self.assertEqual(build_report(VERSION, native).ready, ("h",))

    def test_the_foreign_versions_are_reported_rather_than_silently_dropped(self):
        """A reader must be able to see that something was set aside.

        Silence here is the failure mode: a report that quietly discarded half
        its input would show the same "2 of 7" as one that had all of it, and the
        difference — that somebody passed the 439 file by mistake — would never
        surface.
        """
        claims = complete_hook("h") + [
            probe("other_a", version="439"),
            probe("other_b", version="430"),
            probe("other_c", version="439"),
        ]

        report = build_report(VERSION, claims)

        self.assertEqual(report.foreign, ("430", "439"))
        self.assertEqual(report.hooks, ("h",))
        self.assertIn("other versions     430, 439 (excluded)", render(report))
        self.assertEqual(report.to_dict()["foreign_versions"], ["430", "439"])

    def test_no_foreign_versions_prints_no_line_at_all(self):
        """The control for the line above: it appears because something was excluded.

        Unlike the build and date lines, this one is genuinely conditional —
        there is no honest three-state story to tell about a set that is empty
        because nothing was excluded.
        """
        report = build_report(VERSION, complete_hook("h"))

        self.assertEqual(report.foreign, ())
        self.assertNotIn("other versions", render(report))

    def test_a_claim_with_no_version_is_included_because_it_predates_attribution(self):
        """Absent means "recorded before attribution existed", not "belongs to nobody".

        Every runtime probe of the real 440 run is unattributed, so a filter
        written as `claim.version == version` would drop 23 of 51 claims and
        report 0 of 7 hooks ready on a port that shipped. `manifest/` still holds
        those files unchanged.
        """
        claims = [static("h"), probe("h", version=None), differential("h")]

        report = build_report(VERSION, claims)

        self.assertEqual(report.ready, ("h",))
        self.assertEqual(report.claim_counts["runtime_probe"], 1)
        self.assertEqual(report.foreign, ())

    def test_a_foreign_claim_is_absent_from_the_counts_and_the_builds(self):
        """Exclusion means excluded everywhere, not only from readiness.

        A 439 claim counted in `claim_counts` inflates the coverage denominator,
        and a 439 build hash in `builds` triggers the multi-APK warning about an
        artifact this report was never about. Partial exclusion is worse than
        none: it produces a warning nobody can act on.
        """
        claims = complete_hook("h") + [
            static("h", version="439", build_sha256=OTHER_BUILD),
        ]

        report = build_report(VERSION, claims)

        self.assertEqual(report.builds, (BUILD,))
        self.assertEqual(report.claim_counts["static_verified"], 1)
        self.assertEqual(report.claims_naming_a_build, 1)
        self.assertEqual(report.foreign, ("439",))

    def test_a_report_over_only_foreign_claims_raises(self):
        """The positive control, and the reason the guard exists.

        A `PortReport` with no hooks has no escalations, and no escalations is
        `complete` — so without this refusal, pointing the tool at the wrong
        version's file prints a clean report and exits 0. The empty success is
        constructed below to show it is real rather than theoretical.
        """
        with self.assertRaises(ReportError) as caught:
            build_report(VERSION, [probe("h", version="439")])
        self.assertIn("no claims for version 440", str(caught.exception))

        empty = PortReport(
            version=VERSION,
            hooks=(),
            ready=(),
            escalations=(),
            builds=(),
            recorded_span=None,
            claim_counts={},
            foreign=("439",),
        )
        self.assertIs(empty.complete, True)  # what the refusal prevents

    def test_the_version_must_match_exactly_rather_than_by_prefix(self):
        """"44" is not 440, and "440.0" is not 440 either.

        Instagram versions are dotted quads shortened to their leading number in
        this project ("430", "439", "440"), so prefix or numeric matching is a
        live temptation and both would silently merge two ports. Every claim here
        carries a version, so a match failure is a refusal rather than a report
        over whatever happened to be unattributed.
        """
        versioned = [static("h"), differential("h")]

        for asked in ("44", "4400", "440.0"):
            with self.subTest(version=asked):
                with self.assertRaises(ReportError) as caught:
                    build_report(asked, versioned)
                self.assertIn(f"no claims for version {asked}", str(caught.exception))
        self.assertEqual(build_report(VERSION, versioned).hooks, ("h",))

    def test_an_unversioned_claim_is_inherited_by_whatever_version_is_asked_for(self):
        """The price of "absent means predates attribution", stated out loud.

        A claim with no version joins every report, so asking for 439 over 440's
        files does not refuse — the 23 unattributed device probes answer to any
        version, and only the versioned claims are set aside. What keeps that
        honest is the `other versions` line and the escalations: nothing is
        certified, and the report says which version's claims it excluded. Pinned
        because the alternative reading — "no claims for 439" — is the one a
        reader expects, and this is the behaviour they will actually get.
        """
        report = build_report("439", claims_440())

        self.assertEqual(report.foreign, ("440",))
        self.assertEqual(report.ready, ())
        self.assertEqual(report.claim_counts, {"runtime_probe": 23})
        self.assertIn("other versions     440 (excluded)", render(report))
        self.assertIn("build              (no claim names an APK)", render(report))


# ============================================================== build coverage


class BuildCoverageTests(unittest.TestCase):
    """Which APK was this measured against, and how much of it was.

    The honesty-critical line. One digest under a header reads as "all of this is
    about that APK", and on the real 440 report it is true of 7 claims out of 51.
    """

    def test_no_claim_naming_an_apk_says_so_rather_than_leaving_a_blank(self):
        """Three-state, like `verify_build`'s `required_strings`.

        A report with the build line simply missing reads as one that did not
        think to mention it. "(no claim names an APK)" is a finding: nothing here
        is tied to an artifact, so nothing here says the shipped file works.
        """
        claims = [probe("h"), differential("h")]  # neither kind carries a hash

        report = build_report(VERSION, claims)
        text = render(report)

        self.assertEqual(report.builds, ())
        self.assertEqual(report.claims_naming_a_build, 0)
        self.assertIn("build              (no claim names an APK)", text)
        self.assertNotIn("named by", text)
        self.assertNotIn("DIFFERENT APKs", text)

    def test_one_build_prints_the_digest_and_how_many_claims_name_it(self):
        """The digest alone would overstate what was measured against it.

        `named by 1 of 3 claims` is the difference between "measured against this
        APK" and "measured, somewhere". Both are useful; conflating them is how a
        device result taken on a different build gets read as proof about this
        one.
        """
        report = build_report(VERSION, complete_hook("h"))
        text = render(report)

        self.assertEqual(report.builds, (BUILD,))
        self.assertEqual(report.claims_naming_a_build, 1)
        self.assertIn(f"build              {BUILD}", text)
        self.assertIn("named by 1 of 3 claims", text)
        self.assertIn("the rest are not tied to any artifact", text)

    def test_the_caveat_is_dropped_when_every_claim_names_the_build(self):
        """The control for the sentence above: it is conditional on N < M.

        Printing "the rest are not tied to any artifact" when there is no rest
        would train a reader to skip the line, which costs it its only job.
        """
        claims = [static("h"), static("h2")]

        report = build_report(VERSION, claims)
        text = render(report)

        self.assertEqual(report.claims_naming_a_build, 2)
        self.assertIn("named by 2 of 2 claims", text)
        self.assertNotIn("the rest are not tied", text)

    def test_two_builds_warn_and_list_both_rather_than_picking_one(self):
        """The mutation this line exists to stop: rendering `builds[0]`.

        Both digests below are real 440 artifacts of the same afternoon. Printing
        the first would produce a report that looks exactly like the honest
        single-build one, over claims about two different files — every claim
        individually true, the set describing no artifact that ever existed.
        """
        claims = complete_hook("h") + [static("h2", build_sha256=OTHER_BUILD)]

        report = build_report(VERSION, claims)
        text = render(report)

        self.assertEqual(report.builds, tuple(sorted((BUILD, OTHER_BUILD))))
        self.assertIn("*** 2 DIFFERENT APKs ***", text)
        self.assertIn(BUILD, text)
        self.assertIn(OTHER_BUILD, text)
        self.assertIn("Claims about different artifacts are combined here.", text)
        self.assertIn("Show the builds equivalent or re-measure.", text)

    def test_the_multi_build_case_never_renders_a_single_digest_line(self):
        """Stated as an absence, because the failure is a line that looks right.

        `build              <64 hex>` is the single-build shape. If it appears at
        all when two builds are present, a reader has been told the wrong thing
        confidently, and the warning below it does not undo that.
        """
        claims = complete_hook("h") + [static("h2", build_sha256=OTHER_BUILD)]

        text = render(build_report(VERSION, claims))

        for digest in (BUILD, OTHER_BUILD):
            with self.subTest(digest=digest[:8]):
                self.assertNotIn(f"build              {digest}", text)
        self.assertNotIn("named by", text)

    def test_three_builds_are_all_listed_and_counted(self):
        """The warning is over the set, not a special case for exactly two.

        A run that combined a clean build, an attributed build and a re-graft is
        not less confused than one that combined two.
        """
        third = "a" * 64
        claims = complete_hook("h") + [
            static("h2", build_sha256=OTHER_BUILD),
            static("h3", build_sha256=third),
        ]

        report = build_report(VERSION, claims)
        text = render(report)

        self.assertEqual(len(report.builds), 3)
        self.assertIn("*** 3 DIFFERENT APKs ***", text)
        for digest in report.builds:
            with self.subTest(digest=digest[:8]):
                self.assertIn(f"                       {digest}", text)

    def test_the_coverage_counts_claims_that_name_the_build_not_all_claims(self):
        """Mutation: `claims_naming_a_build = len(mine)`.

        The numerator and the denominator come from different places — one counts
        claims with a hash, the other sums `claim_counts` — and a mutation that
        equated them prints "named by 3 of 3" over a report where one claim names
        an APK and two do not. That is the exact overstatement the line exists to
        prevent, rendered in the format that says it has been checked.
        """
        report = build_report(VERSION, complete_hook("h"))

        self.assertEqual(report.claims_naming_a_build, 1)
        self.assertEqual(sum(report.claim_counts.values()), 3)
        self.assertIn("named by 1 of 3 claims", render(report))

    def test_the_real_440_coverage_is_seven_of_fifty_one(self):
        """The number the module docstring is about, from the whole fixture.

        7 `static_verified` claims name `742fee81…`; the 23 device probes name a
        serial and the 7 differentials name nothing because they span two builds;
        the 14 pre-apply claims cannot name one at all. A report that said "build
        742fee81…" and stopped would be claiming 51 measurements against an APK
        that saw 7.
        """
        report = build_report(VERSION, claims_440())

        self.assertEqual(report.builds, (BUILD,))
        self.assertEqual(report.claims_naming_a_build, 7)
        self.assertEqual(sum(report.claim_counts.values()), 51)
        self.assertIn("named by 7 of 51 claims", render(report))

    def test_a_pre_apply_claim_can_never_name_a_build(self):
        """Why the coverage can never reach 100% on a real run.

        `EvidenceClaim` refuses a pre-apply claim carrying a hash — the fact was
        established before the artifact existed. So the honest coverage of any
        complete port is bounded below 1, and a reader who expected "named by 51
        of 51" would be expecting an impossible report.
        """
        with self.assertRaises(EvidenceError) as caught:
            anchor("h", build_sha256=BUILD)
        self.assertIn("a pre-apply claim cannot name a build", str(caught.exception))


# ============================================================== the recorded span


class RecordedSpanTests(unittest.TestCase):
    """When the evidence was taken, including when nobody knows."""

    def test_undated_evidence_says_so_rather_than_omitting_the_line(self):
        """Said, not skipped — the same discipline as the build line.

        Undated evidence was the norm until 2026-08-06: all thirty committed
        claims had `recorded_at` empty. A missing line would read as a report
        that did not think to mention dates, rather than one reporting that its
        input cannot be ordered in time.
        """
        claims = [probe("h"), differential("h"), static("h", recorded_at="")]

        report = build_report(VERSION, claims)

        self.assertIsNone(report.recorded_span)
        self.assertIn(
            "evidence recorded  (undated — claims predate attribution)", render(report)
        )

    def test_a_single_stamp_prints_once_rather_than_as_an_empty_range(self):
        """"X .. X" is a range of nothing and reads as a formatting bug."""
        report = build_report(VERSION, [static("h", recorded_at=STAMP)])

        self.assertEqual(report.recorded_span, (STAMP, STAMP))
        self.assertIn(f"evidence recorded  {STAMP}", render(report))
        self.assertNotIn("..", render(report))

    def test_a_span_prints_both_ends(self):
        """Two stamps means two moments, and a gate may care about the gap.

        Evidence spanning days is how a device result ends up describing a build
        that was replaced in between.
        """
        later = "2026-08-07T09:15:00Z"
        claims = [static("h", recorded_at=STAMP), static("h2", recorded_at=later)]

        report = build_report(VERSION, claims)

        self.assertEqual(report.recorded_span, (STAMP, later))
        self.assertIn(f"evidence recorded  {STAMP} .. {later}", render(report))

    def test_a_partly_dated_set_spans_only_the_dated_claims(self):
        """The real 440 shape: 21 stamped rows and 30 unstamped ones.

        An undated claim contributes no bound. Treating `""` as a stamp would
        sort first and print a span starting at the empty string, which is how a
        report ends up with a leading " .. " and no date.
        """
        report = build_report(VERSION, claims_440())

        self.assertEqual(report.recorded_span, (STAMP, STAMP))
        text = render(report)
        self.assertIn(f"evidence recorded  {STAMP}", text)
        self.assertNotIn("undated", text)


# ================================================================== read_claims


class ReadClaimsTests(ReportTestCase):
    """Reading three files is where a report quietly loses a third of its input."""

    def test_a_missing_file_names_the_path_rather_than_contributing_nothing(self):
        """The docstring's own reason, as a test.

        A report assembled from three sources of which one silently contributed
        nothing reads as "this hook has no runtime evidence" when the truth is
        "nobody looked in the right place" — and the two are indistinguishable in
        the output, because both produce `not_exercised`.
        """
        with self.assertRaises(ReportError) as caught:
            read_claims([self.tmp / "not-there.jsonl"])

        self.assertIn("no evidence at", str(caught.exception))
        self.assertIn("not-there.jsonl", str(caught.exception))

    def test_a_missing_file_is_refused_even_when_the_others_have_claims(self):
        """Mutation: return `[]` for a missing file.

        With one file that mutation still raises "no claims in any evidence
        file", so a single-file test cannot see it. With three files — the real
        invocation — it produces a complete-looking report over two thirds of the
        evidence, and the missing third is the runtime file whose absence turns
        every hook into `not_exercised`.
        """
        present = self.write("runtime.jsonl", device_claims_440())

        with self.assertRaises(ReportError) as caught:
            read_claims([present, self.tmp / "differential.jsonl"])

        self.assertIn("differential.jsonl", str(caught.exception))

    def test_a_malformed_line_names_the_file_and_the_line_number(self):
        """A report over three JSONL files cannot say "somewhere in the input".

        The line number is the whole value of the message: the files are 7, 21
        and 23 rows of near-identical JSON, and a human is going to open one of
        them by hand.
        """
        path = self.tmp / "broken.jsonl"
        path.write_text(
            json.dumps(static("h").to_dict()) + "\n"
            + json.dumps(probe("h").to_dict()) + "\n"
            + "{ not json\n",
            encoding="utf-8",
        )

        with self.assertRaises(ReportError) as caught:
            read_claims([path])

        self.assertIn("broken.jsonl:3", str(caught.exception))

    def test_a_line_the_schema_rejects_is_reported_at_its_own_number(self):
        """Not only unparseable JSON: valid JSON the ledger will not accept.

        A claim with no `hook_id`, or from a future schema, is a different fault
        from a truncated write, and both have to be attributable to a line.
        """
        rows = [
            json.dumps(static("h").to_dict()),
            json.dumps({"schema_version": 1, "kind": "runtime_probe", "verdict": "passed",
                        "producer": "device", "actor": "d", "summary": "s"}),
        ]
        no_hook = self.tmp / "no-hook.jsonl"
        no_hook.write_text("\n".join(rows) + "\n", encoding="utf-8")

        with self.assertRaises(ReportError) as caught:
            read_claims([no_hook])
        self.assertIn("no-hook.jsonl:2", str(caught.exception))

        future = self.tmp / "future.jsonl"
        future.write_text(
            json.dumps({**static("h").to_dict(), "schema_version": 99}) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(ReportError) as caught:
            read_claims([future])
        self.assertIn("future.jsonl:1", str(caught.exception))
        self.assertIn("unsupported evidence schema", str(caught.exception))

    def test_a_blank_line_is_not_a_malformed_one(self):
        """JSONL written by an appending ledger picks up trailing newlines.

        Refusing those would make the tool fail on files it wrote itself, and the
        pressure would then be to loosen the malformed-line check instead.
        """
        path = self.tmp / "padded.jsonl"
        path.write_text(
            "\n" + json.dumps(static("h").to_dict()) + "\n\n   \n"
            + json.dumps(probe("h").to_dict()) + "\n\n",
            encoding="utf-8",
        )

        self.assertEqual(len(read_claims([path])), 2)

    def test_no_claims_at_all_is_refused_rather_than_reported_as_nothing_to_do(self):
        """An empty read must not become a clean report.

        `build_report` would raise on the empty list anyway, but with a message
        about the *version* — "no claims for version 440" — which sends a reader
        looking for an attribution bug rather than at the empty file they passed.
        """
        empty = self.tmp / "empty.jsonl"
        empty.write_text("", encoding="utf-8")

        with self.assertRaises(ReportError) as caught:
            read_claims([empty])
        self.assertIn("no claims in any evidence file", str(caught.exception))

    def test_every_file_is_read_and_the_order_given_is_preserved(self):
        """Order is load-bearing: it is what makes a retry visible.

        `readiness` reads `history[-1]` as the latest attempt and flags a pass
        that follows a measured negative. Files concatenated in a different order
        than the run produced them would turn a retry into a clean pass, or
        invent one where there was none.
        """
        first = self.write("a.jsonl", [probe("h", Verdict.INCONCLUSIVE, summary="none")])
        second = self.write("b.jsonl", [probe("h", Verdict.PASSED)])

        forwards = read_claims([first, second])
        backwards = read_claims([second, first])

        self.assertEqual([claim.verdict for claim in forwards],
                         [Verdict.INCONCLUSIVE, Verdict.PASSED])
        self.assertEqual([claim.verdict for claim in backwards],
                         [Verdict.PASSED, Verdict.INCONCLUSIVE])

        # And the order changes what the report says happened. Neither ordering
        # is ready — that is not the point — but only one of them is a retry, and
        # a gate handed the wrong one is told the probe simply never worked.
        rest = [static("h"), differential("h")]
        as_run = reasons_by_hook(build_report(VERSION, forwards + rest))["h"]
        reversed_ = reasons_by_hook(build_report(VERSION, backwards + rest))["h"]

        self.assertIn("reached passed only after a failure (2 attempts)", as_run[0])
        self.assertIn("runtime_probe: inconclusive", reversed_[0])
        self.assertNotIn("attempts", reversed_[0])

    def test_the_real_three_file_invocation_round_trips(self):
        """The fixture survives `to_dict` -> JSONL -> `from_dict` unchanged.

        Every other test here builds claims in memory. If the on-disk form lost
        a field — `version` and `build_sha256` are both omitted-when-absent — the
        CLI would report something the library never would.
        """
        paths = [
            self.write("evidence.jsonl", run_claims_440()),
            self.write("runtime.jsonl", device_claims_440()),
            self.write("differential.jsonl", differential_claims_440()),
        ]

        self.assertEqual(read_claims(paths), claims_440())


# ====================================================================== the CLI


class CliTests(ReportTestCase):
    """The process contract: 0 complete, 1 incomplete, 2 refused."""

    def complete_files(self) -> list[str]:
        return ["--evidence", str(self.write("ok.jsonl", complete_hook("h")))]

    def test_a_complete_report_exits_zero(self):
        """The control for the exit-1 test below."""
        code, stdout, stderr = self.run_main("--version", VERSION, *self.complete_files())

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("RELEASE-READY      1 of 1", stdout)

    def test_an_incomplete_report_exits_one_because_a_release_script_gates_on_it(self):
        """Mutation: `return 0` unconditionally.

        The exit code is the entire machine-readable interface. A report that
        printed five escalations and exited 0 would be worse than no report at
        all: the prose says "needs a human" and the pipeline says "ship it", and
        only one of those is read by a script.
        """
        code, stdout, stderr = self.run_main("--version", VERSION, *self.files_440())

        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        self.assertIn("Needs a human (5)", stdout)

    def test_the_json_form_carries_the_same_exit_code(self):
        """`--json` is a rendering choice, not a different verdict.

        A caller that switched to `--json` for parseability and silently started
        getting 0 would have been given the machine format and lost the machine
        signal.
        """
        code, _, _ = self.run_main("--version", VERSION, *self.files_440(), "--json")
        self.assertEqual(code, 1)

        code, _, _ = self.run_main("--version", VERSION, *self.complete_files(), "--json")
        self.assertEqual(code, 0)

    def test_a_missing_evidence_file_is_refused_on_stderr_with_exit_two(self):
        """Bad input is 2, distinct from "the port is not ready", which is 1.

        Collapsing them would make a typo'd path indistinguishable from a real
        finding, and the typo'd path is the commoner event.
        """
        code, stdout, stderr = self.run_main(
            "--version", VERSION, "--evidence", str(self.tmp / "nope.jsonl")
        )

        self.assertEqual(code, 2)
        self.assertTrue(stderr.startswith("refused: "), stderr)
        self.assertIn("nope.jsonl", stderr)
        self.assertEqual(stdout, "")

    def test_a_report_for_a_version_nothing_claims_is_refused_not_certified(self):
        """Exit 2, not 0. The empty-success path, through the process.

        A `PortReport` over no hooks has no escalations and is therefore
        `complete`, so without the refusal in `build_report` this prints a
        spotless report and exits 0 — the strongest possible statement, made
        about nothing. The files here are the two whose every claim carries a
        version; see
        :meth:`VersionFilteringTests.
        test_an_unversioned_claim_is_inherited_by_whatever_version_is_asked_for`
        for what happens when unattributed claims are in the mix.
        """
        args = [
            "--evidence", str(self.write("evidence.jsonl", run_claims_440())),
            "--evidence", str(self.write("differential.jsonl", differential_claims_440())),
        ]

        code, stdout, stderr = self.run_main("--version", "439", *args)

        self.assertEqual(code, 2)
        self.assertIn("refused: no claims for version 439", stderr)
        self.assertEqual(stdout, "")

    def test_the_json_release_ready_matches_the_rendered_text(self):
        """Two renderings of one report must not be able to disagree.

        Both come from the same `PortReport`, so this is cheap to keep true — and
        it is the assertion that catches a future `--json` that recomputed
        anything, or a `render` that filtered the ready list for display.
        """
        code, text, _ = self.run_main("--version", VERSION, *self.files_440())
        _, raw, _ = self.run_main("--version", VERSION, *self.files_440(), "--json")

        payload = json.loads(raw)

        self.assertEqual(code, 1)
        self.assertIs(payload["complete"], False)
        self.assertEqual(payload["release_ready"], list(READY_440))
        self.assertEqual(
            payload["release_ready"],
            [line.strip().removeprefix("✓ ") for line in text.splitlines()
             if line.strip().startswith("✓ ")],
        )
        self.assertEqual(
            [item["hook_id"] for item in payload["escalations"]],
            [line.strip().removeprefix("✗ ") for line in text.splitlines()
             if line.strip().startswith("✗ ")],
        )

    def test_the_json_carries_the_fields_a_caller_would_join_on(self):
        """A schema version, the build coverage, and the excluded versions.

        `--json` exists so something other than a human can consume this. Dropping
        `claims_naming_a_build` from it would leave the honesty caveat available
        only in prose, which is the same as not having it.
        """
        _, raw, _ = self.run_main("--version", VERSION, *self.files_440(), "--json")
        payload = json.loads(raw)

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["version"], VERSION)
        self.assertEqual(payload["builds"], [BUILD])
        self.assertEqual(payload["claims_naming_a_build"], 7)
        self.assertEqual(payload["recorded_span"], [STAMP, STAMP])
        self.assertEqual(payload["foreign_versions"], [])
        self.assertEqual(sum(payload["claim_counts"].values()), 51)

    def test_out_writes_exactly_what_was_printed(self):
        """The file and the terminal must not be able to drift.

        `--out` is how this ends up attached to a release. A file that differed
        from what the operator read is the worst possible version of that.
        """
        out = self.tmp / "nested" / "report.txt"

        code, stdout, _ = self.run_main(
            "--version", VERSION, *self.files_440(), "--out", str(out)
        )

        self.assertEqual(code, 1)
        self.assertEqual(out.read_text(encoding="utf-8"), stdout)
        self.assertTrue(stdout.endswith("\n"))

    def test_out_writes_the_json_when_json_was_asked_for(self):
        """One `text` variable feeds both, so `--out --json` must save JSON.

        Writing the prose to a file named by a caller who asked for JSON would be
        discovered by whatever tried to parse it, one release later.
        """
        out = self.tmp / "report.json"

        self.run_main("--version", VERSION, *self.files_440(), "--json", "--out", str(out))

        payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(payload["release_ready"], list(READY_440))

    def test_out_creates_its_parent_directory(self):
        """A release directory that does not exist yet is the normal case."""
        out = self.tmp / "release" / "440" / "report.txt"

        self.run_main("--version", VERSION, *self.complete_files(), "--out", str(out))

        self.assertTrue(out.is_file())


# ============================================================ the real 440 port


class RealPortTests(ReportTestCase):
    """The whole 440 run, end to end: 7 hooks, 51 claims, 2 release-ready."""

    def setUp(self) -> None:
        super().setUp()
        self.report = build_report(VERSION, claims_440())
        self.text = render(self.report)

    def test_the_fixture_reproduces_the_report_the_real_files_give(self):
        """The four numbers that tie this file to the run of 2026-08-06.

        7 hooks, 51 claims, 7 of them naming build `742fee81…`, 2 release-ready.
        Anything that drifts here has stopped describing the port that shipped,
        and every other assertion in this class would go on passing about a run
        that never happened.
        """
        self.assertEqual(self.report.hooks, tuple(sorted(HOOKS)))
        self.assertEqual(sum(self.report.claim_counts.values()), 51)
        self.assertEqual(self.report.claims_naming_a_build, 7)
        self.assertEqual(self.report.ready, READY_440)
        self.assertEqual(
            self.report.claim_counts,
            {
                "anchor_unique": 7,
                "registers_safe": 7,
                "static_verified": 7,
                "runtime_probe": 23,
                "differential": 7,
            },
        )

    def test_two_of_seven_are_release_ready_and_five_escalate(self):
        """The headline, and the reason the port did not ship as a whole.

        Five hooks fail on device evidence a clean build cannot supply: three
        Reels hooks and the action-bar settings hook never announced execution,
        and every differential against 439 compares two probe shapes that are not
        comparable.
        """
        self.assertEqual(self.report.ready, ("set_app_context", "tigon_url_block"))
        self.assertEqual(len(self.report.escalations), 5)
        self.assertIs(self.report.complete, False)
        self.assertIn("RELEASE-READY      2 of 7", self.text)
        self.assertIn("Needs a human (5):", self.text)

    def test_every_escalation_reason_is_a_string_the_ledger_produced(self):
        """Nothing in the report is this module's own prose about a hook.

        The reasons a human acts on must come from the ledger, so that the
        vocabulary at a gate, in a driver's output and in this document is one
        vocabulary. A reporter that paraphrased would be a second place to fix
        when the wording of a finding changed, and the two would drift.
        """
        ledger = ledger_over(claims_440())

        for item in self.report.escalations:
            with self.subTest(hook=item["hook_id"]):
                self.assertEqual(
                    list(item["reasons"]),
                    list(ledger.readiness(item["hook_id"], POST_BUILD).reasons),
                )
                for reason in item["reasons"]:
                    self.assertIn(reason, self.text)

    def test_the_reasons_are_the_three_post_build_kinds_and_nothing_else(self):
        """Named exactly, so "five escalated" is not five unexamined failures.

        Four hooks fail on both the probe and the differential; the stream hook's
        probe passed and only its differential is inconclusive. Every hook's
        `static_verified` passed, which is precisely why a static pass is not a
        release.
        """
        reasons = reasons_by_hook(self.report)

        self.assertEqual(
            {hook: [reason.split(":")[0] for reason in items]
             for hook, items in reasons.items()},
            {
                "install_settings_long_click": ["differential", "runtime_probe"],
                "install_settings_long_click_actionbar": ["differential", "runtime_probe"],
                "replace_reels_discover_endpoint": ["differential", "runtime_probe"],
                "replace_reels_homecoming_endpoint": ["differential", "runtime_probe"],
                "replace_reels_stream_endpoint": ["differential"],
            },
        )
        self.assertNotIn("static_verified:", self.text)

    def test_the_retry_on_the_legacy_settings_hook_is_visible(self):
        """One hook escalates on a kind whose latest verdict is `passed`.

        `install_settings_long_click` was `inconclusive` on the first walkthrough
        and announced itself on the next two. That is three attempts with a
        measured negative in the sequence, and the report has to show the
        sequence rather than the last frame of it.
        """
        reason = [
            item for item in reasons_by_hook(self.report)["install_settings_long_click"]
            if item.startswith("runtime_probe")
        ][0]

        self.assertIn("reached passed only after a failure (3 attempts)", reason)
        self.assertIn("Re-running until green is how a ledger gets defeated", reason)
        # The control: the actionbar hook never went green, so it is a plain
        # inconclusive rather than a retry.
        actionbar = reasons_by_hook(self.report)["install_settings_long_click_actionbar"]
        self.assertIn("runtime_probe: inconclusive", " ".join(actionbar))
        self.assertNotIn("attempts", " ".join(actionbar))

    def test_every_hook_appears_exactly_once_in_the_rendered_body(self):
        """Ready or escalated, never both, never neither.

        The two lists are derived from one set difference, so a hook could in
        principle be dropped from both — and a hook nobody printed is a hook
        nobody chases.
        """
        ticks = [line.strip()[2:] for line in self.text.splitlines()
                 if line.strip().startswith("✓ ")]
        crosses = [line.strip()[2:] for line in self.text.splitlines()
                   if line.strip().startswith("✗ ")]

        self.assertEqual(sorted(ticks + crosses), sorted(HOOKS))
        self.assertEqual(len(set(ticks) & set(crosses)), 0)

    def test_the_header_counts_agree_with_the_body(self):
        """A header a reader trusts and a body they skim: they must match.

        `len(ready) of len(hooks)` and the escalation count are formatted
        separately from the lists they describe.
        """
        ticks = sum(1 for line in self.text.splitlines() if line.strip().startswith("✓"))
        crosses = sum(1 for line in self.text.splitlines() if line.strip().startswith("✗"))

        self.assertIn(f"hooks              {len(self.report.hooks)}", self.text)
        self.assertIn(f"RELEASE-READY      {ticks} of {ticks + crosses}", self.text)
        self.assertIn(f"Needs a human ({crosses}):", self.text)

    def test_the_report_disclaims_what_it_cannot_say(self):
        """The last paragraph is load-bearing, not decoration.

        Two hooks are release-ready by evidence. Three inert patches have shipped
        from this project passing everything up to the runtime probe, so a
        document titled "release-ready" that did not say what it is not saying
        would be the most quotable wrong sentence the pipeline produces.
        """
        self.assertIn("It is not a statement that the app works", self.text)
        self.assertIn("every inert patch this project has shipped", self.text)

    def test_no_hook_is_release_ready_when_the_runtime_file_is_left_out(self):
        """The combination that produces the honest zero.

        Somebody assembling this by hand will forget a file, and the failure has
        to be loud in the body rather than a slightly smaller number in the
        header. Without the device evidence every hook is `not_exercised` on
        `runtime_probe`, including the two that shipped.
        """
        report = build_report(VERSION, run_claims_440() + differential_claims_440())

        self.assertEqual(report.ready, ())
        self.assertIn("No hook has complete post-build evidence.", render(report))
        for hook in HOOKS:
            with self.subTest(hook=hook):
                self.assertIn(
                    "runtime_probe: no claim recorded",
                    " ".join(reasons_by_hook(report)[hook]),
                )

    def test_the_full_invocation_through_three_files_exits_one(self):
        """The command in the module docstring, run for real.

        Everything above works on in-memory claims; this is the only test that
        exercises the arguments a person types, the three files, the rendering
        and the exit code together.
        """
        code, stdout, stderr = self.run_main("--version", VERSION, *self.files_440())

        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        self.assertEqual(stdout, self.text + "\n")


# ================================================================== known gaps


class ClosedGapTests(ReportTestCase):
    """Four things this file found while it was being written, all fixed.

    Each was written first as a pin on what the module then did, so a fix
    announced itself here rather than as a quiet change in what a report says.
    """

    def test_an_agent_hook_must_name_its_proposer_and_now_can(self):
        """`--provenance HOOK=agent:PROPOSER`, which is the only way to register one.

        `Subject` refuses an agent-resolved hook that does not name its proposer —
        correctly, since "produced by something other than the proposer" is
        uncheckable without it. So while `--provenance` could only say `agent`,
        an agent-resolved hook could not be put in a report at all.
        """
        claims = complete_hook("h")

        # Bare `agent` is still refused, and that refusal is correct.
        with self.assertRaises(EvidenceError) as caught:
            build_report(VERSION, claims, provenance={"h": "agent"})
        self.assertIn("must name its proposer", str(caught.exception))

        # Naming the proposer registers the hook and the report is produced.
        report = build_report(VERSION, claims, provenance={"h": "agent:claude-x"})
        self.assertEqual(report.hooks, ("h",))

    def test_provenance_changes_no_verdict_after_a_build_and_that_is_the_point(self):
        """The honest limit of `--provenance` in this stage, pinned so it is not overstated.

        `mechanical`, `agent` and `already_applied` require **exactly the same
        three kinds** after a build. Everything provenance decides lives in
        PRE_APPLY, which this stage deliberately does not report — a port's
        pre-apply claims live under gitignored `work/`, so requiring them would
        escalate every hook for a reason of file location rather than of evidence.

        So the flag exists to let a `Subject` be constructed, not to make a hook
        owe more. A reader who takes "reported as agent" to mean the
        agent-specific checks were consulted is wrong, and this test is where that
        is written down. If it ever starts failing, the report grew a pre-apply
        half and the docstring needs revisiting with it.
        """
        claims = complete_hook("h")

        mechanical = build_report(VERSION, claims, provenance={"h": "mechanical"})
        agent = build_report(VERSION, claims, provenance={"h": "agent:claude-x"})
        applied = build_report(VERSION, claims, provenance={"h": "already_applied"})

        self.assertEqual(mechanical.ready, ("h",))
        self.assertEqual(agent.ready, ("h",))
        self.assertEqual(applied.ready, ("h",))

        # And the requirement sets really are identical at this phase, so the
        # agreement above is the rule rather than an accident of the fixture.
        from dfinsta_pipeline.evidence import PHASES, requirements_for  # noqa: PLC0415

        sets = {
            name: frozenset(
                kind for kind in requirements_for(name) if PHASES[kind] == POST_BUILD
            )
            for name in ("mechanical", "agent", "already_applied")
        }
        self.assertEqual(len(set(sets.values())), 1, sets)

    def test_an_override_naming_a_hook_with_no_claims_is_refused(self):
        """A misspelt hook id used to change nothing and say nothing.

        `provenance.get(hook, default)` was consulted per hook *found in the
        claims*, so a key matching none was never read and the operator saw a
        report that looked as though their flag had been honoured. The silence was
        fail-safe only by luck — the default is the larger of the two reachable
        sets — and would under-require the moment that changed.
        """
        claims = [static("h"), probe("h"), differential("h"), anchor("h")]

        with self.assertRaises(ReportError) as caught:
            build_report(VERSION, claims, provenance={"hook_that_does_not_exist": "mechanical"})
        self.assertIn("hook_that_does_not_exist", str(caught.exception))
        self.assertIn("h", str(caught.exception), "it should say which hooks exist")

        code, _, stderr = self.run_main(
            "--version", VERSION,
            "--evidence", str(self.write("ok.jsonl", claims)),
            "--provenance", "hook_that_does_not_exist=mechanical",
        )
        self.assertEqual(code, 2)
        self.assertTrue(stderr.startswith("refused: "), stderr)

        # Control: the correctly spelled key is accepted and the run completes.
        code, _, _ = self.run_main(
            "--version", VERSION,
            "--evidence", str(self.write("ok2.jsonl", claims)),
            "--provenance", "h=mechanical",
        )
        self.assertEqual(code, 0)

    def test_an_unknown_enum_value_is_a_refusal_naming_the_line(self):
        """A kind from a newer pipeline must not exit 1 as a traceback.

        `from_dict` calls `EvidenceKind(...)` and `Verdict(...)`, and an
        unrecognised member raises a plain `ValueError` — which `EvidenceError`
        subclasses but is not. So the one malformation a future schema makes
        likely was the one that did NOT get the file-and-line treatment: it
        escaped as a traceback and exited 1, which a release script reads as
        "this port is not ready" rather than "I could not read the file".
        `EvidenceLedger.load` catches the wider tuple over the identical call.
        """
        path = self.tmp / "future-kind.jsonl"
        path.write_text(
            json.dumps({**static("h").to_dict(), "kind": "quantum_verified"}) + "\n",
            encoding="utf-8",
        )

        with self.assertRaises(ReportError) as caught:
            read_claims([path])
        self.assertIn("future-kind.jsonl", str(caught.exception))
        self.assertIn(":1:", str(caught.exception))

        code, stdout, stderr = self.run_main("--version", VERSION, "--evidence", str(path))
        self.assertEqual(code, 2)
        self.assertTrue(stderr.startswith("refused: "), stderr)
        self.assertEqual(stdout, "")

        # Control: the same file with a known kind reads cleanly, so the refusal
        # is about the enum and not about the fixture.
        good = self.tmp / "known-kind.jsonl"
        good.write_text(json.dumps(static("h").to_dict()) + "\n", encoding="utf-8")
        self.assertEqual(len(read_claims([good])), 1)

    def test_a_present_but_empty_file_is_noted_rather_than_passed_over(self):
        """The guard was on the path and on the total, with a gap between them.

        A file that exists and is empty *alongside* files that are not is what a
        device run that captured nothing leaves behind. It contributed zero rows
        and said nothing, and the report then read "no runtime evidence for any
        hook" rather than "the runtime file is empty" — the exact confusion
        `read_claims` exists to prevent.

        Noted rather than refused, deliberately: a differential file legitimately
        holds nothing when a version is the first of its line, so refusing would
        block a real case. The report is unchanged; the operator is told.
        """
        runtime = self.tmp / "runtime.jsonl"
        runtime.write_text("", encoding="utf-8")
        rest = self.write("evidence.jsonl", run_claims_440())

        stream = io.StringIO()
        with contextlib.redirect_stderr(stream):
            claims = read_claims([rest, runtime])

        self.assertEqual(len(claims), 21)
        self.assertIn("no claims in", stream.getvalue())
        self.assertIn("runtime.jsonl", stream.getvalue())

        # The readiness answer is deliberately unchanged by the note.
        report = build_report(VERSION, claims)
        self.assertEqual(report.ready, ())

    def test_a_full_set_of_files_says_nothing_on_stderr(self):
        """POSITIVE CONTROL: the note must mean something when it appears."""
        stream = io.StringIO()
        with contextlib.redirect_stderr(stream):
            read_claims([self.write("evidence.jsonl", run_claims_440())])
        self.assertEqual(stream.getvalue(), "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

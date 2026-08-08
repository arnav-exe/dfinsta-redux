"""`expectation.py` is the only module in this pipeline that can fail a port.

`final_report` counts what a port can be shown to have achieved and `history`
prints that count beside the previous versions'. Neither *fails* when it falls.
This module makes the one assertion the other two refuse — every hook that was
release-ready on N-1 is release-ready on N — so what has to be tested is not
"does it add up" but the handful of ways an assertion like this stops asserting
anything.

The properties pinned here are the ones where being wrong is invisible:

**A drop must fail loudly, and a vanished hook loudest of all.**
:class:`DropTests` and :class:`VanishedTests`. A hook that regressed has the
ledger's reasons attached; a hook with no claim at all on N has none, and the
emptiness is the finding rather than a quieter version of the same one. The two
are asserted against each other in the same fixtures, because "it printed a
warning" is satisfied by a module that prints the same warning for both.

**The bar comes down exactly one way.** There is one escape hatch — a
recorded *retirement* naming a human and a reason, and it is the only one. It
was deleted on 2026-08-08 with the rest of the decision-correction layer and
rebuilt small the same day, because a ratchet with no release is a trap: when
Instagram removes a surface the hook can never pass again. What these tests
assert is that no *second* way down grows quietly — a lowered bar with no author
is the failure this module exists to prevent, and `tests/test_retirement.py`
owns the refusals in the one authored path.

**A hook's readiness across the whole series is one answer, not two.**
:class:`StandingTests`. `standings` moved here from the deleted `retirement`
module; `roster` is its consumer. The distinction it exists for is the one
`dropped_at` cannot express alone — a hook that never passed and a hook still
passing both have no drop version, and they are opposite situations — so every
test asserts `never_release_ready` beside `dropped_at()`.

**It must not compare across a gap.** :class:`PredecessorTests`. The predecessor
is the version immediately before N in the series, not the newest one that
happens to have files, and the discriminating fixture is four versions deep:
comparing 442 against 441 also requires reading 441's own differential, which is
named for the 440/441 pair. A module that reached for `series[0]` anywhere in
that chain still produces a plausible-looking comparison — with an empty
expectation, which passes.

**A check that cannot run must not read as a check that passed.**
:class:`SweepTests` and :class:`CliTests`. A sweep returns what it skipped as
well as what it compared, a sweep that compared nothing exits 2 rather than 0,
and a drop exits 3 — deliberately not 1, which `final_report` already uses for
"incomplete", the condition that is true on every successful port this project
has ever run.

===============================================================================
  FIXTURE PROVENANCE
===============================================================================

Every corpus here is a temp tree in the real layout —
`manifest/static_evidence/<v>.jsonl`, `manifest/runtime_evidence/<v>.jsonl`,
`manifest/differentials/<prev>-<v>.jsonl`. The rows are the flat on-disk shape
copied from those committed files, not `EvidenceClaim` objects serialised, so the
JSON the module actually parses is the JSON under test.

The version numbers are 439 upward because `BASELINE_VERSION` is 439 and a
fixture below the floor would be filtered out of every series. The hook ids are
the manifest's real ones. The three post-build kinds are spelled out per hook
through :func:`triple`, because "release-ready" is exactly "all three passed" and
a fixture that said `ready=True` would be asserting the module's own conclusion
back at it.

`tests/test_expectation_corpus.py` is the other half of this file and is not
duplicated here: it points the same module at the repository's committed
evidence, which is what makes a real port turn the suite red.

===============================================================================
  MUTATION RESULTS
===============================================================================

Thirty-five mutations were applied one at a time to an out-of-tree copy of the
repository, each against a fresh copy, with the unmutated copy passing first as
the control and again at the end. Every one was caught. The bracketed number is
how many distinct tests in this file failed.

**Eleven of those thirty-five are gone with the code they mutated.** They were
the "lowering the bar" group — the retirement parser, the `effective_from`
gating, the earliest-wins rule — and this note stands in for them rather than
the count being quietly restated, because a mutation figure that shrinks without
saying why reads as a weakened suite. The remaining twenty-four are below and
were re-run against this file after the removal.

Not failing what should fail:

* `EXIT_DROPPED = 0` [1] and `Comparison.met` always True [10] →
  :class:`CliTests` and everything that asserts an outcome
* `Verdict.vanished` always False [5], `vanished` computed from the reasons
  alone without the state [2], the vanished branch dropped from `render` [2] →
  :class:`VanishedTests`, :class:`CliTests`
* `actual` is every hook with a claim rather than the release-ready ones [20],
  the ledger's reasons are not carried onto the verdict [6], a gain is recorded
  as `held` [4] → :class:`DropTests`, :class:`HeldAndGainedTests`
* `sweep` discards the pairs it skipped [3] and `main` renders an empty skip
  list [1] → :class:`SweepTests`, :class:`CliTests`
* `main` drops the "nothing was checked" refusal [3], and accepts `--previous`
  without `--version` [1] → :class:`CliTests`

Reading the wrong evidence:

* `evidence_files` omits the runtime file [37] → nearly every test, because no
  hook is release-ready on either side and every expectation empties
* the differential is required rather than added when present [3] →
  :class:`EvidenceFilesTests`, :class:`PredecessorTests`
* `compare` takes `series[0]` as the predecessor [28], `_predecessor` takes
  `earlier[0]` [3], `compare` accepts `previous == version` [1] →
  :class:`PredecessorTests` and everything downstream of a wrong pairing
* `versions_with_evidence` sorts without `key=int` [1], reads only
  `static_evidence` [2], accepts any filename stem [1], drops the baseline guard
  [1] → :class:`VersionsWithEvidenceTests`
* `port_report` returns an empty report instead of raising [5] →
  :class:`CompareRefusalTests`, :class:`SweepTests`
* the version guard is dropped from `compare` [1] and `main` stops catching
  `ValueError` [1] → :class:`CompareRefusalTests`, :class:`CliTests`

===============================================================================
  KNOWN DEFECTS
===============================================================================

:class:`KnownDefectTests` are `expectedFailure` on purpose — the convention this
suite already uses for a defect its own tests found, and which
`tests/test_history.py`, `tests/test_probes.py` and `tests/test_reaper.py` each
recorded theirs with before closing them. Each asserts what the module's own
docstrings say must happen and what it does not yet do, so the suite stays green
today and reports an *unexpected success* the moment one is fixed.

1. `--json` prints the comparisons and drops the skipped pairs. The rendered
   output has a `NOT CHECKED` block for exactly the reason `sweep`'s docstring
   gives — "a pair nobody checked is never mistaken for a pair that passed" — and
   the machine-readable form a release script would consume has no such block and
   no such field.

(A second defect was recorded here — a retirements row that parsed as JSON but
was not an object raised `AttributeError` rather than refusing by line. It was
fixed, and both it and the file it was about are now deleted.)
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Iterable, Mapping

from dfinsta_pipeline import expectation
from dfinsta_pipeline.expectation import (
    EXIT_DROPPED,
    EXIT_MET,
    EXIT_REFUSED,
    Comparison,
    ExpectationError,
    Standing,
    Verdict,
    compare,
    evidence_files,
    render,
    standings,
    sweep,
    versions_with_evidence,
)
from dfinsta_pipeline.history import BASELINE_VERSION

# ------------------------------------------------------------------- fixtures

#: Real hook ids from the manifest. Which hook is which does not matter to the
#: module, but a fixture named `hook_a` would let a reader believe the ids are
#: opaque tokens when the whole output is addressed to a human who knows them.
CONTEXT = "set_app_context"
TIGON = "tigon_url_block"
SETTINGS = "install_settings_long_click"
REELS = "replace_reels_stream_endpoint"
DISCOVER = "replace_reels_discover_endpoint"

DEVICE = "device:P3227J000775"
VERIFIER = "tools/verify/verify_build.py"
STAMP = "2026-08-07T12:05:00Z"
BUILD = "64ca7eecb4520bb0e7c3667c52be835f2454f9444b8e343fd934fe841ae539b4"

#: The three post-build kinds, and which producer each one allows. Taken from
#: `evidence.ALLOWED_PRODUCERS`: a `static_verified` row naming a device, or a
#: `runtime_probe` naming the verifier, is refused at parse time and the fixture
#: would be testing the schema rather than this module.
PRODUCERS = {
    "static_verified": ("deterministic", VERIFIER),
    "runtime_probe": ("device", DEVICE),
    "differential": ("device", DEVICE),
}


def evidence_row(hook_id: str, kind: str, verdict: str, version: str) -> dict[str, Any]:
    """One claim in the flat on-disk shape `manifest/` actually holds.

    A `differential` row carries no `build_sha256` because it spans two builds,
    which is how the real files are written and is asserted by
    `EvidenceClaim.__post_init__` for the pre-apply kinds rather than this one —
    it is copied here for fidelity, not because a check depends on it.
    """

    producer, actor = PRODUCERS[kind]
    row = {
        "actor": actor,
        "confidence": None,
        "decision_id": None,
        "detail": {},
        "hook_id": hook_id,
        "kind": kind,
        "producer": producer,
        "rationale": "",
        "recorded_at": STAMP,
        "schema_version": 1,
        "summary": f"{hook_id}: {kind} {verdict} on {version}",
        "supersedes": None,
        "verdict": verdict,
        "version": version,
    }
    if kind != "differential":
        row["build_sha256"] = BUILD
    return row


def triple(**overrides: str | None) -> dict[str, str]:
    """A full passing post-build triple for one hook, varied by keyword.

    Release-ready is exactly "all three POST_BUILD kinds passed", so this is what
    a release-ready hook looks like and every unready one is a departure from it.
    A kind set to ``None`` is dropped rather than recorded with a bad verdict —
    the two are different failures and the module tells them apart: a `failed`
    differential is a regression this port caused, a missing one means nobody
    measured.
    """

    kinds: dict[str, str | None] = {
        "static_verified": "passed",
        "runtime_probe": "passed",
        "differential": "passed",
    }
    kinds.update(overrides)
    return {kind: verdict for kind, verdict in kinds.items() if verdict is not None}


class ExpectationTestCase(unittest.TestCase):
    """A temp `manifest/` tree in the real layout, and a way to run `main`."""

    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        self.manifest = self.tmp / "manifest"
        for name in ("static_evidence", "runtime_evidence", "differentials"):
            (self.manifest / name).mkdir(parents=True)

    # ------------------------------------------------------------- the corpus

    def write(self, path: Path, rows: Iterable[Mapping[str, Any]]) -> Path:
        path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )
        return path

    def port(
        self,
        version: str,
        hooks: Mapping[str, Mapping[str, str]],
        *,
        previous: str | None = None,
        runtime_file: bool = True,
        static_file: bool = True,
    ) -> None:
        """One version's durable evidence, split across the three real directories.

        `hooks` maps a hook to its post-build claims — see :func:`triple`. The
        differential file is written only when some hook has a differential row,
        which matches the tree: 439 has no `438-439.jsonl` and never will.
        """

        by_kind: dict[str, list[dict[str, Any]]] = {
            "static_verified": [],
            "runtime_probe": [],
            "differential": [],
        }
        for hook, kinds in hooks.items():
            for kind, verdict in kinds.items():
                by_kind[kind].append(evidence_row(hook, kind, verdict, version))
        if static_file:
            self.write(
                self.manifest / "static_evidence" / f"{version}.jsonl",
                by_kind["static_verified"],
            )
        if runtime_file:
            self.write(
                self.manifest / "runtime_evidence" / f"{version}.jsonl",
                by_kind["runtime_probe"],
            )
        if by_kind["differential"]:
            if previous is None:
                raise AssertionError(
                    f"a differential file is named for a pair; {version} needs a "
                    "`previous=` to be named against"
                )
            self.write(
                self.manifest / "differentials" / f"{previous}-{version}.jsonl",
                by_kind["differential"],
            )

    def baseline_port(self) -> None:
        """439: present in the series, and release-ready in nothing.

        Every fixture needs it, for a structural reason rather than a
        conventional one — 440's differential file is named `439-440.jsonl`, so
        440 can only be release-ready in anything if 439 is in the series before
        it. The first version of a series can never be release-ready itself,
        which is why 439 has no computable readiness at all.
        """

        self.port("439", {CONTEXT: triple(differential=None)})

    def two_ports(
        self,
        on_440: Mapping[str, Mapping[str, str]],
        on_441: Mapping[str, Mapping[str, str]],
    ) -> None:
        """The smallest corpus in which a hook can be release-ready and then lost."""

        self.baseline_port()
        self.port("440", dict(on_440), previous="439")
        self.port("441", dict(on_441), previous="440")

    # -------------------------------------------------------------- shortcuts

    def compare(self, **kwargs: Any) -> Comparison:
        return compare(self.tmp, **kwargs)

    def sweep(self, **kwargs: Any) -> tuple[list[Comparison], list[tuple[str, str]]]:
        return sweep(self.tmp, **kwargs)

    def states(self, comparison: Comparison) -> dict[str, str]:
        return {verdict.hook_id: verdict.state for verdict in comparison.verdicts}

    def verdict_for(self, comparison: Comparison, hook_id: str) -> Verdict:
        for verdict in comparison.verdicts:
            if verdict.hook_id == hook_id:
                return verdict
        raise AssertionError(
            f"no verdict for {hook_id!r}; got {[v.hook_id for v in comparison.verdicts]}"
        )

    def run_main(self, *args: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = expectation.main(["--root", str(self.tmp), *args])
        return code, stdout.getvalue(), stderr.getvalue()


# ==================================================================== the drop


class DropTests(ExpectationTestCase):
    """A hook that was release-ready and is not any more must fail the port.

    This is the whole reason the module exists, so the tests are built to
    separate it from the two things it could be confused with: a hook that never
    passed (nothing is owed) and a hook that vanished (owed, and unmeasured). The
    reasons are asserted to be the ledger's own words rather than this module's,
    because a second opinion on readiness would agree with the first until one
    of them was edited.
    """

    def test_a_hook_whose_differential_regressed_is_named_as_a_drop(self):
        """The ordinary regression: measured on both ports, and worse on the later.

        `tigon_url_block` keeps passing in the same fixture, so a module that
        called every hook dropped would fail here too.
        """
        self.two_ports(
            {CONTEXT: triple(), TIGON: triple()},
            {CONTEXT: triple(differential="failed"), TIGON: triple()},
        )

        comparison = self.compare(version="441")

        self.assertEqual(comparison.dropped, (CONTEXT,))
        self.assertEqual(comparison.held, (TIGON,))
        self.assertFalse(comparison.met)

    def test_the_expectation_is_the_previous_ports_ready_set_and_not_a_count(self):
        """Derived, and derived as a *set*. `4 -> 3` says a port got worse; a name
        says which thing to go and look at, and the module survives the hook set
        changing size, and naming the hook rather than a delta.
        """
        self.two_ports(
            {CONTEXT: triple(), TIGON: triple()},
            {CONTEXT: triple(runtime_probe="inconclusive"), TIGON: triple()},
        )

        comparison = self.compare(version="441")

        self.assertEqual(comparison.expected, (CONTEXT, TIGON))
        self.assertEqual(comparison.actual, (TIGON,))
        self.assertEqual(comparison.previous, "440")

    def test_the_reasons_on_a_drop_are_the_ledgers_own_escalation_text(self):
        """Not paraphrased here, and not recomputed there.

        `compare` reads `PortReport.escalations` and copies the reason strings
        across. A reader is told `runtime_probe: failed — …` in the same words
        `final_report` would print, which is what makes "read the reasons before
        the count" possible at all.
        """
        self.two_ports(
            {CONTEXT: triple()},
            {CONTEXT: triple(runtime_probe="failed")},
        )

        verdict = self.verdict_for(self.compare(version="441"), CONTEXT)

        self.assertEqual(verdict.state, "dropped")
        self.assertTrue(verdict.reasons)
        self.assertTrue(
            any(reason.startswith("runtime_probe: failed") for reason in verdict.reasons),
            verdict.reasons,
        )

    def test_a_hook_that_never_passed_on_either_port_is_not_a_drop(self):
        """Nothing was owed, so nothing was lost. The control for the whole class.

        Three of the seven hooks have never passed a runtime probe on any
        version, so if an unready hook counted as a drop every port would fail
        and the gate would be worthless.
        """
        self.two_ports(
            {CONTEXT: triple(), REELS: triple(runtime_probe="inconclusive")},
            {CONTEXT: triple(), REELS: triple(runtime_probe="inconclusive")},
        )

        comparison = self.compare(version="441")

        self.assertEqual(comparison.dropped, ())
        self.assertTrue(comparison.met)
        self.assertNotIn(REELS, comparison.expected)

    def test_a_missing_required_kind_drops_the_hook_as_surely_as_a_failed_one(self):
        """"Not measured" is not a pass. The ledger's rule, seen from here.

        A walkthrough that never reached this hook's surface loses it, and that
        is the correct reading: absence is never a pass. The reasons distinguish
        the two, which is why the rendered output tells the reader to fix the
        device session rather than the hook. `tigon_url_block` was probed in the
        same session and holds, so the file is not simply empty — a hook missing
        from a populated runtime corpus is the realistic shape.
        """
        self.two_ports(
            {CONTEXT: triple(), TIGON: triple()},
            {CONTEXT: triple(runtime_probe=None), TIGON: triple()},
        )

        verdict = self.verdict_for(self.compare(version="441"), CONTEXT)

        self.assertEqual(verdict.state, "dropped")
        self.assertFalse(verdict.vanished)
        self.assertTrue(
            any("runtime_probe: no claim recorded" in r for r in verdict.reasons),
            verdict.reasons,
        )

    def test_the_rendering_names_the_dropped_hook_the_count_and_the_way_back(self):
        """What a human sees. The count alone is the failure this module fixes.

        Three things have to be in the output: which hook, why, and the fact that
        the bar comes down only through a recorded retirement. The last one matters because the
        obvious repair for a red gate is to edit the gate — and the recorded
        retirement that used to be the honest answer no longer exists.
        """
        self.two_ports(
            {CONTEXT: triple(), TIGON: triple()},
            {CONTEXT: triple(differential="failed"), TIGON: triple()},
        )

        text = render(self.compare(version="441"))

        self.assertIn("*** 1 HOOK(S) DROPPED ***", text)
        self.assertIn(f"✗ {CONTEXT}", text)
        self.assertNotIn(f"✗ {TIGON}", text)
        self.assertIn("ONLY through a recorded retirement", text)
        self.assertNotIn("Expectation met", text)

    def test_a_dropped_hook_survives_the_json_round_trip_with_its_reasons(self):
        """`to_dict` is what a release script reads, and it must carry the finding.

        A JSON view that reported `met: false` with no hook and no reason would
        be a count again, one serialisation later.
        """
        self.two_ports({CONTEXT: triple()}, {CONTEXT: triple(differential="failed")})

        payload = json.loads(json.dumps(self.compare(version="441").to_dict()))

        self.assertFalse(payload["met"])
        self.assertEqual(payload["dropped"], [CONTEXT])
        self.assertEqual(payload["expected"], [CONTEXT])
        self.assertEqual(payload["actual"], [])
        self.assertTrue(payload["verdicts"][0]["reasons"])
        self.assertFalse(payload["verdicts"][0]["vanished"])


# ================================================================ the vanished


class VanishedTests(ExpectationTestCase):
    """A hook in the expectation with no claim at all on N. The loudest case.

    `build_report` never sees such a hook, so it has no reasons to give, and an
    empty reason list is what a *quiet* failure looks like everywhere else in
    this codebase. Here it is the finding: from this module's position, a hook
    with no evidence is indistinguishable from a hook someone deleted from the
    manifest. Every test below is paired against an ordinary regression in the
    same corpus, because a module that treated both identically would satisfy
    half of each of these on its own.
    """

    def test_a_hook_with_no_claim_at_all_is_dropped_with_no_reasons(self):
        """The two halves of `Verdict.vanished`, and the discriminator beside it.

        `set_app_context` regressed and has reasons; `tigon_url_block` is simply
        absent from 441's corpus and has none. Both are drops.
        """
        self.two_ports(
            {CONTEXT: triple(), TIGON: triple()},
            {CONTEXT: triple(differential="failed")},
        )

        comparison = self.compare(version="441")
        gone = self.verdict_for(comparison, TIGON)
        regressed = self.verdict_for(comparison, CONTEXT)

        self.assertEqual(sorted(comparison.dropped), sorted([CONTEXT, TIGON]))
        self.assertTrue(gone.vanished)
        self.assertEqual(gone.reasons, ())
        self.assertFalse(regressed.vanished)
        self.assertTrue(regressed.reasons)

    def test_the_vanished_hook_gets_the_longest_sentence_in_the_rendering(self):
        """It must not read as a quieter failure than a regression.

        The rendered block for a vanished hook says there is no evidence about it
        whatsoever and names both explanations — removed from the manifest, or
        never published. The regressed hook in the same output does not get that
        sentence, which is the assertion that stops it being printed for
        everything.
        """
        self.two_ports(
            {CONTEXT: triple(), TIGON: triple()},
            {CONTEXT: triple(differential="failed")},
        )

        text = render(self.compare(version="441"))

        self.assertIn("NO CLAIM AT ALL on 441", text)
        self.assertIn("it was removed from the manifest", text)
        self.assertIn("*** 2 HOOK(S) DROPPED ***", text)
        self.assertEqual(text.count("NO CLAIM AT ALL"), 1)

    def test_a_vanished_hook_fails_the_port_and_not_only_the_prose(self):
        """A message nobody gates on is the state this module was written to leave.

        Exit 3, `met` false, and the hook named — the same three facts a
        regression produces, because a hook that disappeared is not a lesser
        failure.
        """
        self.two_ports({CONTEXT: triple(), TIGON: triple()}, {CONTEXT: triple()})

        comparison = self.compare(version="441")
        code, stdout, _ = self.run_main("--version", "441")

        self.assertFalse(comparison.met)
        self.assertEqual(comparison.dropped, (TIGON,))
        self.assertEqual(code, EXIT_DROPPED)
        self.assertIn("NO CLAIM AT ALL", stdout)

    def test_a_whole_port_that_published_nothing_about_a_hook_set_vanishes_all_of_it(self):
        """The manifest-shrank case, which is what this is really watching for.

        440 carried seven hook ids and 439 carried ten. A port that drops three
        hooks from the manifest and publishes evidence for the rest produces
        exactly this shape, and every one of them must be named rather than the
        count quietly falling.
        """
        self.two_ports(
            {CONTEXT: triple(), TIGON: triple(), SETTINGS: triple()},
            {CONTEXT: triple()},
        )

        comparison = self.compare(version="441")

        self.assertEqual(sorted(comparison.dropped), sorted([SETTINGS, TIGON]))
        self.assertTrue(all(self.verdict_for(comparison, h).vanished
                            for h in (SETTINGS, TIGON)))
        self.assertEqual(comparison.held, (CONTEXT,))

    def test_vanished_is_false_for_every_state_that_is_not_a_drop(self):
        """The flag is a property of the state and the reasons together.

        A held or gained hook also has no reasons, and a `vanished` computed from
        emptiness alone would light up on both — which would make the loudest
        message in the module fire on the good news.
        """
        self.two_ports({CONTEXT: triple()}, {CONTEXT: triple(), TIGON: triple()})

        comparison = self.compare(version="441")

        self.assertEqual(self.states(comparison), {CONTEXT: "held", TIGON: "gained"})
        for verdict in comparison.verdicts:
            self.assertEqual(verdict.reasons, ())
            self.assertFalse(verdict.vanished, verdict.hook_id)


# ============================================================= held and gained


class HeldAndGainedTests(ExpectationTestCase):
    """The two non-failing outcomes, and why one of them is still not good news.

    A hook that starts working cannot become release-ready in the port that fixes
    it — `differential` needs a passing baseline to regress from — so the first
    version a gain appears in is the version where it is least verified. The
    module reports it as UNCONFIRMED and says what would confirm it, and the test
    that matters is that the gain really does become the next port's expectation.
    """

    def test_a_hook_ready_on_both_ports_is_held_and_the_port_passes(self):
        self.two_ports({CONTEXT: triple(), TIGON: triple()},
                       {CONTEXT: triple(), TIGON: triple()})

        comparison = self.compare(version="441")

        self.assertEqual(sorted(comparison.held), sorted([CONTEXT, TIGON]))
        self.assertEqual(comparison.dropped, ())
        self.assertTrue(comparison.met)

    def test_the_met_rendering_names_the_hooks_rather_than_only_the_count(self):
        """Symmetry with the failure path: the good news is a set too.

        A reader who sees only "expectation met" learns nothing about which
        hooks it was met for, and cannot notice that the expectation was two when
        they thought it was four.
        """
        self.two_ports({CONTEXT: triple(), TIGON: triple()},
                       {CONTEXT: triple(), TIGON: triple()})

        text = render(self.compare(version="441"))

        self.assertIn("Expectation met — all 2 hook(s)", text)
        self.assertIn(f"✓ {CONTEXT}", text)
        self.assertIn(f"✓ {TIGON}", text)
        self.assertNotIn("DROPPED", text)

    def test_a_newly_ready_hook_is_gained_and_owed_nothing_by_this_port(self):
        self.two_ports({CONTEXT: triple()}, {CONTEXT: triple(), SETTINGS: triple()})

        comparison = self.compare(version="441")

        self.assertEqual(comparison.gained, (SETTINGS,))
        self.assertNotIn(SETTINGS, comparison.expected)
        self.assertIn(SETTINGS, comparison.actual)
        self.assertTrue(comparison.met)

    def test_the_gain_is_rendered_as_unconfirmed_with_what_would_confirm_it(self):
        """Stated every time, not only when it looks surprising.

        The sentence explains *why* a gain is unconfirmed — the fixing port reads
        `inconclusive/baseline_not_a_pass` — so a reader can tell an unconfirmed
        gain from a hedge.
        """
        self.two_ports({CONTEXT: triple()}, {CONTEXT: triple(), SETTINGS: triple()})

        text = render(self.compare(version="441"))

        self.assertIn("Newly release-ready (1) — UNCONFIRMED:", text)
        self.assertIn(f"+ {SETTINGS}", text)
        self.assertIn("A hook cannot become release-ready in the port that fixes it", text)
        self.assertIn("the port after 441", text)

    def test_a_gain_becomes_the_next_ports_expectation(self):
        """The confirmation the previous test only promises, one version later.

        Four versions deep, because this is the whole shape of the claim: 441
        gains a hook and is not credited for it, 442 is *held to* it. A module
        that reported gains and never carried them forward would print the same
        UNCONFIRMED block forever and never fail anything.
        """
        self.baseline_port()
        self.port("440", {CONTEXT: triple()}, previous="439")
        self.port("441", {CONTEXT: triple(), SETTINGS: triple()}, previous="440")
        self.port("442", {CONTEXT: triple(), SETTINGS: triple(differential="failed")},
                  previous="441")

        gained = self.compare(version="441")
        owed = self.compare(version="442")

        self.assertEqual(gained.gained, (SETTINGS,))
        self.assertTrue(gained.met)
        self.assertIn(SETTINGS, owed.expected)
        self.assertEqual(owed.dropped, (SETTINGS,))
        self.assertFalse(owed.met)


class PredecessorTests(ExpectationTestCase):
    """The version immediately before N *in the series*, never the newest on disk.

    Skipping 439 to compare 441 against 440 is right; skipping 440 to compare 441
    against 439 would silently forgive whatever 440 lost. The chain matters twice
    over — the previous port's own report needs *its* predecessor, because a
    differential file is named for a pair — so the discriminating fixture is four
    versions deep and a module that reached for the wrong end of the list
    produces an empty expectation, which passes.
    """

    def test_the_predecessor_is_the_version_immediately_before_not_the_first(self):
        self.two_ports({CONTEXT: triple()}, {CONTEXT: triple()})

        self.assertEqual(self.compare(version="441").previous, "440")

    def test_a_gap_in_the_series_is_closed_over_rather_than_reached_across(self):
        """440 was never ported; 441's predecessor is 439, the one before it *here*.

        "Immediately before in the series" is not "N-1": the series is what has
        evidence, and a version nobody ported is not a hole in it.
        """
        self.baseline_port()
        self.port("441", {CONTEXT: triple()}, previous="439")

        self.assertEqual(self.compare(version="441").previous, "439")

    def test_the_previous_ports_own_report_is_read_against_its_own_predecessor(self):
        """Four versions, and the drop is only visible if 441 was read correctly.

        441's readiness depends on `differentials/440-441.jsonl`, which is found
        only by knowing 440 came before it. A predecessor rule that took the first
        version of the series would look for `439-441.jsonl`, find nothing, read
        441 as zero release-ready, and hand 442 an empty expectation — so a real
        regression would print as a clean port.
        """
        self.baseline_port()
        self.port("440", {CONTEXT: triple()}, previous="439")
        self.port("441", {CONTEXT: triple(), SETTINGS: triple()}, previous="440")
        self.port("442", {CONTEXT: triple(), SETTINGS: triple(runtime_probe="failed")},
                  previous="441")

        comparison = self.compare(version="442")

        self.assertEqual(comparison.previous, "441")
        self.assertEqual(sorted(comparison.expected), sorted([CONTEXT, SETTINGS]))
        self.assertEqual(comparison.dropped, (SETTINGS,))
        self.assertFalse(comparison.met)

    def test_an_explicit_previous_overrides_the_series(self):
        """The flag exists; it must actually do something, and it is rarely right."""
        self.baseline_port()
        self.port("440", {CONTEXT: triple()}, previous="439")
        self.port("441", {CONTEXT: triple()}, previous="440")

        self.assertEqual(self.compare(version="441", previous="439").previous, "439")

    def test_an_explicit_previous_changes_the_bar_and_not_the_evidence(self):
        """`--previous` chooses whose bar to meet, never what this port measured.

        It used to choose both. `evidence_files` names the differential file for
        the pair it is given, so `--version 442 --previous 440` looked for
        `differentials/440-442.jsonl`, found nothing, and reported 442 as having
        lost every hook 440 had — with the reason `differential: no claim
        recorded`, which is true of a file that was never supposed to exist and
        tells the reader nothing about 442. It failed in the safe direction and
        for a fabricated cause, which is its own kind of wrong.

        Now 442 is always assembled from its own predecessor, so the flag means
        the one thing its name suggests. It is still rarely right: measuring
        against 440 forgives whatever 441 lost, which is why the help says so.
        """
        self.baseline_port()
        self.port("440", {CONTEXT: triple()}, previous="439")
        self.port("441", {CONTEXT: triple()}, previous="440")
        self.port("442", {CONTEXT: triple()}, previous="441")

        honest = self.compare(version="442")
        overridden = self.compare(version="442", previous="440")

        self.assertTrue(honest.met)
        self.assertTrue(overridden.met)
        # The evidence read is identical; only the expectation's source moved.
        self.assertEqual(honest.actual, overridden.actual)
        self.assertEqual("441", honest.previous)
        self.assertEqual("440", overridden.previous)


# ===================================================== versions with evidence


class VersionsWithEvidenceTests(ExpectationTestCase):
    """The series the predecessor rule walks. Getting it wrong mis-pairs everything.

    The union of the static and runtime directories rather than either alone: a
    version part-way through a port has one and not the other, and it must still
    appear in the series so that the version after it is compared against the
    right predecessor.
    """

    def test_a_version_in_either_directory_alone_is_in_the_series(self):
        """The union, with a positive control on each side.

        A version with only static evidence is mid-port — the driver publishes it
        at build time and the device session lands hours later. Reading only one
        directory would drop it from the series and hand its successor the wrong
        predecessor, which is a wrong *comparison* rather than a missing one.
        """
        self.write(self.manifest / "static_evidence" / "440.jsonl", [])
        self.write(self.manifest / "runtime_evidence" / "441.jsonl", [])

        self.assertEqual(versions_with_evidence(self.tmp), ["440", "441"])

    def test_a_non_numeric_filename_is_not_a_version(self):
        """`int()` is called on every member, so a stray file is a crash otherwise.

        The directories also hold a `README.md` each, which the `*.jsonl` glob
        already excludes — this is about the ones that would get through it.
        """
        for name in ("441.jsonl", "draft.jsonl", "1.4.1.jsonl", "440-441.jsonl"):
            self.write(self.manifest / "static_evidence" / name, [])

        self.assertEqual(versions_with_evidence(self.tmp), ["441"])

    def test_the_series_is_sorted_by_number_and_not_as_text(self):
        """`"1000" < "439"` as strings, so a text sort puts 1000 first.

        Every point after it would then be compared against the wrong
        predecessor — the same trap `history.series` documents, and the reason
        both say `key=int`. Today's three-digit arc is ordered correctly by luck.
        """
        for version in ("1000", "441", "439", "1001"):
            self.write(self.manifest / "runtime_evidence" / f"{version}.jsonl", [])

        self.assertEqual(
            versions_with_evidence(self.tmp), ["439", "441", "1000", "1001"]
        )

    def test_versions_below_the_baseline_are_excluded_in_both_directions(self):
        """340 and 430 are a different architecture; the floor is not a convenience.

        Moved backward as well as forward, so the exclusion is the baseline's
        doing rather than some other property of the old versions' files.
        """
        for version in ("340", "430", "439", "440"):
            self.write(self.manifest / "static_evidence" / f"{version}.jsonl", [])

        self.assertEqual(versions_with_evidence(self.tmp), ["439", "440"])
        self.assertEqual(
            versions_with_evidence(self.tmp, baseline="340"),
            ["340", "430", "439", "440"],
        )
        self.assertEqual(versions_with_evidence(self.tmp, baseline="440"), ["440"])

    def test_the_default_baseline_is_the_shared_constant(self):
        """One floor for the whole pipeline, imported rather than re-typed."""
        for version in ("430", "439"):
            self.write(self.manifest / "static_evidence" / f"{version}.jsonl", [])

        self.assertEqual(BASELINE_VERSION, "439")
        self.assertEqual(
            versions_with_evidence(self.tmp),
            versions_with_evidence(self.tmp, baseline=BASELINE_VERSION),
        )

    def test_a_tree_with_no_evidence_directories_is_empty_rather_than_an_error(self):
        """A fresh checkout, or a mistyped root. The refusal belongs further up."""
        self.assertEqual(versions_with_evidence(self.tmp / "nowhere"), [])

    def test_a_baseline_that_is_not_a_version_number_is_refused(self):
        with self.assertRaises(ExpectationError) as caught:
            versions_with_evidence(self.tmp, baseline="nope")

        self.assertIn("nope", str(caught.exception))


class EvidenceFilesTests(ExpectationTestCase):
    """The conventional three files, by convention and not by argument.

    A caller that omitted `runtime_evidence` would get a report in which no hook
    is release-ready, and comparing that against a full one manufactures a drop in
    every hook at once. So the list is fixed here and the test says so.
    """

    def test_both_durable_files_are_always_named_even_when_absent(self):
        """Named, so `read_claims` refuses by path rather than contributing nothing.

        A missing file must reach the reader as "no evidence at
        manifest/runtime_evidence/441.jsonl", not as an empty list that reads as
        "this hook has no runtime evidence".
        """
        files = evidence_files(self.tmp, "441", None)

        self.assertEqual(
            [path.relative_to(self.tmp).as_posix() for path in files],
            ["manifest/static_evidence/441.jsonl", "manifest/runtime_evidence/441.jsonl"],
        )

    def test_the_differential_is_added_only_when_the_pair_file_exists(self):
        """It is the one file that legitimately does not exist, at the first version.

        439 has no `438-439.jsonl` and never will, so demanding it would make the
        baseline unreadable rather than unready.
        """
        self.write(self.manifest / "differentials" / "440-441.jsonl", [])

        self.assertEqual(len(evidence_files(self.tmp, "441", "440")), 3)
        self.assertEqual(len(evidence_files(self.tmp, "441", "439")), 2)
        self.assertEqual(len(evidence_files(self.tmp, "440", None)), 2)


# ======================================================== refusing to compare


class CompareRefusalTests(ExpectationTestCase):
    """What `compare` will not do, and says so rather than answering anyway."""

    def test_the_first_version_of_a_series_cannot_be_measured_against_one(self):
        """It establishes the bar. An empty expectation would be a vacuous pass.

        This is not hypothetical — it is 439's state in the real corpus, and the
        refusal is what stops `--version 439` reporting a clean port about a
        version nothing was compared to.
        """
        self.baseline_port()

        with self.assertRaises(ExpectationError) as caught:
            self.compare(version="439")

        self.assertIn("no predecessor", str(caught.exception))
        self.assertIn("439", str(caught.exception))

    def test_a_predecessor_that_does_not_precede_is_refused(self):
        """Comparing a port against a later one inverts the check.

        Every hook the later port fixed would read as a drop, which is a red gate
        pointing at the wrong version. Equality is refused too: a version is not
        its own predecessor, and the comparison would be empty rather than wrong,
        which is worse.
        """
        self.two_ports({CONTEXT: triple()}, {CONTEXT: triple()})

        for previous in ("441", "442"):
            with self.subTest(previous=previous):
                with self.assertRaises(ExpectationError) as caught:
                    self.compare(version="441", previous=previous)

                self.assertIn("does not precede", str(caught.exception))

    def test_a_version_that_is_not_a_number_is_refused_before_anything_is_read(self):
        self.two_ports({CONTEXT: triple()}, {CONTEXT: triple()})

        for version in ("441-rc1", "", "nope"):
            with self.subTest(version=version):
                with self.assertRaises(ExpectationError):
                    self.compare(version=version)

    def test_a_previous_that_is_not_a_number_is_refused(self):
        self.two_ports({CONTEXT: triple()}, {CONTEXT: triple()})

        with self.assertRaises(ExpectationError):
            self.compare(version="441", previous="four-forty")

    def test_a_version_with_no_evidence_is_refused_and_names_the_missing_file(self):
        """Not reported as a port with nothing release-ready.

        An empty report compared against a full one is a drop in every hook; a
        report that cannot be built at all is a refusal. The two must not be
        confusable, because the first is a red gate and the second is a typo.
        """
        self.baseline_port()
        self.port("440", {CONTEXT: triple()}, previous="439")

        with self.assertRaises(ExpectationError) as caught:
            self.compare(version="441", previous="440")

        self.assertIn("441", str(caught.exception))
        self.assertIn("no evidence at", str(caught.exception))

    def test_a_half_ported_version_is_refused_rather_than_read_as_a_regression(self):
        """Static evidence published, device session not yet run. The mid-port state.

        Reading it would report every hook of the previous port as dropped, which
        would train a reader to ignore the loudest message the module has.
        """
        self.baseline_port()
        self.port("440", {CONTEXT: triple()}, previous="439")
        self.port("441", {CONTEXT: triple()}, previous="440", runtime_file=False)

        with self.assertRaises(ExpectationError) as caught:
            self.compare(version="441")

        self.assertIn("runtime_evidence", str(caught.exception))


# ======================================================================= sweep


class SweepTests(ExpectationTestCase):
    """Both halves of the return value are the result.

    A sweep that returned only what it managed to check would pass an empty
    corpus, and a check that cannot fail is the shape this project has shipped
    more than once. So every test here asserts what was compared *and* what was
    not, and never one without the other.
    """

    def test_every_consecutive_pair_is_compared_and_nothing_is_skipped(self):
        self.two_ports({CONTEXT: triple()}, {CONTEXT: triple()})

        comparisons, skipped = self.sweep()

        self.assertEqual([(c.previous, c.version) for c in comparisons],
                         [("439", "440"), ("440", "441")])
        self.assertEqual(skipped, [])

    def test_a_drop_anywhere_in_the_series_is_carried_out_of_the_sweep(self):
        self.two_ports({CONTEXT: triple()}, {CONTEXT: triple(differential="failed")})

        comparisons, skipped = self.sweep()

        self.assertEqual([c.met for c in comparisons], [True, False])
        self.assertEqual(comparisons[-1].dropped, (CONTEXT,))
        self.assertEqual(skipped, [])

    def test_an_uncomparable_pair_is_named_with_its_reason(self):
        """Skipping is right; skipping silently is not.

        The reason is the refusal's own text, so the pair that was not checked
        carries the same sentence a human would get from asking about it
        directly.
        """
        self.baseline_port()
        self.port("440", {CONTEXT: triple()}, previous="439")
        self.port("441", {CONTEXT: triple()}, previous="440", runtime_file=False)

        comparisons, skipped = self.sweep()

        self.assertEqual([(c.previous, c.version) for c in comparisons],
                         [("439", "440")])
        self.assertEqual([pair for pair, _ in skipped], ["440 -> 441"])
        self.assertIn("runtime_evidence", skipped[0][1])

    def test_a_half_ported_tail_does_not_stop_the_pairs_before_it_being_checked(self):
        """The mid-port state is the ordinary one, and it must not turn the gate off.

        442 is part-way through, so 441 -> 442 cannot be checked — but 440 -> 441
        can, and the drop in it is found. A sweep that abandoned the run at the
        first skip would report a clean series.
        """
        self.baseline_port()
        self.port("440", {CONTEXT: triple(), TIGON: triple()}, previous="439")
        self.port("441", {CONTEXT: triple(), TIGON: triple(differential="failed")},
                  previous="440")
        self.port("442", {CONTEXT: triple()}, previous="441", runtime_file=False)

        comparisons, skipped = self.sweep()

        self.assertEqual([c.version for c in comparisons], ["440", "441"])
        self.assertEqual(comparisons[-1].dropped, (TIGON,))
        self.assertEqual([pair for pair, _ in skipped], ["441 -> 442"])

    def test_a_series_of_one_compares_nothing_and_skips_nothing(self):
        """The empty result that must not be mistaken for a pass.

        Both lists empty is the honest answer — there was no pair — and it is the
        caller's job to notice, which is why `main` turns it into exit 2 and
        `render_sweep` says so in words.
        """
        self.baseline_port()

        self.assertEqual(self.sweep(), ([], []))

    def test_the_baseline_moves_which_pairs_the_sweep_makes(self):
        """A floor that only ever excluded the same versions could be a coincidence."""
        self.two_ports({CONTEXT: triple()}, {CONTEXT: triple()})

        comparisons, _ = self.sweep(baseline="440")

        self.assertEqual([(c.previous, c.version) for c in comparisons],
                         [("440", "441")])


# ======================================================================= the CLI


class CliTests(ExpectationTestCase):
    """The process boundary: three exit codes, stderr, and JSON a script can read.

    `1` is deliberately not among them. `final_report` already exits 1 for
    "incomplete", and incomplete is this project's normal state — three hooks
    have never passed a runtime probe on any version — so a drop sharing that
    code would be invisible inside the condition that is true on every successful
    port.
    """

    def test_a_met_expectation_prints_the_report_and_exits_zero(self):
        self.two_ports({CONTEXT: triple()}, {CONTEXT: triple()})

        code, stdout, stderr = self.run_main()

        self.assertEqual(code, EXIT_MET)
        self.assertEqual(stderr, "")
        self.assertIn("EXPECTATION  440 → 441", stdout)
        self.assertIn("Expectation met", stdout)

    def test_a_drop_exits_three_and_not_the_code_final_report_uses(self):
        """The gate that matters must not hide inside the one that always fires."""
        self.two_ports({CONTEXT: triple()}, {CONTEXT: triple(differential="failed")})

        code, stdout, _ = self.run_main("--version", "441")

        self.assertEqual(code, 3)
        self.assertEqual(code, EXIT_DROPPED)
        self.assertNotIn(EXIT_DROPPED, (EXIT_MET, EXIT_REFUSED, 1))
        self.assertIn("HOOK(S) DROPPED", stdout)

    def test_one_dropped_pair_in_a_sweep_fails_the_whole_sweep(self):
        """A sweep is an assertion about the series, not a list of independent runs."""
        self.baseline_port()
        self.port("440", {CONTEXT: triple()}, previous="439")
        self.port("441", {CONTEXT: triple()}, previous="440")
        self.port("442", {CONTEXT: triple(runtime_probe="failed")}, previous="441")

        code, stdout, _ = self.run_main()

        self.assertEqual(code, EXIT_DROPPED)
        self.assertIn("EXPECTATION  441 → 442", stdout)

    def test_a_sweep_that_could_compare_nothing_exits_two_rather_than_zero(self):
        """The absence of a check is not a pass. Said in words and in the exit code.

        An empty corpus, a mistyped root and a series of one all land here, and
        all three would otherwise be the most reassuring output the tool can
        produce.
        """
        self.baseline_port()

        code, stdout, stderr = self.run_main()

        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("No pair could be compared. That is not a pass", stdout)
        self.assertIn("refused: no pair could be compared", stderr)

    def test_a_pair_the_sweep_could_not_check_is_named_in_the_output(self):
        """The NOT CHECKED block, which is the whole reason `sweep` returns two lists.

        A pair nobody checked must never be mistaken for a pair that passed, and
        the rendered form is where a human would notice. The comparison that
        *was* made is asserted in the same output, so this is not satisfied by a
        tool that printed the block unconditionally.
        """
        self.baseline_port()
        self.port("440", {CONTEXT: triple()}, previous="439")
        self.port("441", {CONTEXT: triple()}, previous="440")
        self.port("442", {CONTEXT: triple()}, previous="441", runtime_file=False)

        code, stdout, _ = self.run_main()

        self.assertEqual(code, EXIT_MET)
        self.assertIn("NOT CHECKED", stdout)
        self.assertIn("441 -> 442", stdout)
        self.assertIn("mid-port state", stdout)
        self.assertIn("EXPECTATION  440 → 441", stdout)

    def test_an_empty_root_exits_two_as_well(self):
        code, stdout, stderr = self.run_main()

        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("nothing was checked", stderr)
        self.assertIn("absence of a check", stdout)

    def test_previous_without_version_is_refused_because_a_sweep_has_no_single_one(self):
        """A flag that silently did nothing would be worse than one that refuses.

        `--previous` overrides the predecessor of one comparison. A sweep makes
        several, so honouring it would mean applying one override to every pair,
        and ignoring it would mean the run a human asked for is not the run they
        got.
        """
        self.two_ports({CONTEXT: triple()}, {CONTEXT: triple()})

        code, stdout, stderr = self.run_main("--previous", "440")

        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("refused: --previous needs --version", stderr)
        self.assertEqual(stdout, "")

    def test_previous_with_version_is_accepted_which_is_the_control(self):
        """Otherwise the refusal above is also satisfied by a flag that never works."""
        self.baseline_port()
        self.port("440", {CONTEXT: triple()}, previous="439")
        self.port("441", {CONTEXT: triple()}, previous="440")

        code, stdout, _ = self.run_main("--version", "441", "--previous", "440")

        self.assertEqual(code, EXIT_MET)
        self.assertIn("EXPECTATION  440 → 441", stdout)

    def test_a_bad_version_is_refused_on_stderr_rather_than_left_as_a_traceback(self):
        self.two_ports({CONTEXT: triple()}, {CONTEXT: triple()})

        code, stdout, stderr = self.run_main("--version", "441-rc1")

        self.assertEqual(code, EXIT_REFUSED)
        self.assertTrue(stderr.startswith("refused: "), stderr)
        self.assertEqual(stdout, "")

    def test_a_non_numeric_baseline_is_refused_and_not_a_traceback(self):
        """A typo on a flag is the most ordinary way this tool is used wrongly."""
        self.two_ports({CONTEXT: triple()}, {CONTEXT: triple()})

        code, _, stderr = self.run_main("--baseline", "nope")

        self.assertEqual(code, EXIT_REFUSED)
        self.assertTrue(stderr.startswith("refused: "), stderr)

    def test_a_version_too_long_for_int_is_refused_through_the_same_channel(self):
        """The positive control for the `ValueError` arm of `main`'s except clause.

        The comment beside that clause says `--baseline nope` is what reaches
        `int()`, and it is not: `versions_with_evidence` guards the baseline and
        raises `ExpectationError` first, so that arm looks like dead code. It is
        not dead — `\\d+` matches a number of any length and CPython refuses to
        parse an integer literal past 4300 digits — and this is the input that
        proves the refusal channel really is closed rather than closed-looking.
        """
        self.two_ports({CONTEXT: triple()}, {CONTEXT: triple()})

        code, stdout, stderr = self.run_main("--version", "9" * 4301)

        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("refused: ", stderr)
        self.assertIn("4300 digits", stderr)
        self.assertEqual(stdout, "")

    def test_a_mistyped_root_is_refused_rather_than_reported_as_a_clean_series(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = expectation.main(["--root", str(self.tmp / "typo")])

        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("refused: ", err.getvalue())

    def test_the_json_form_carries_the_verdicts_and_the_same_exit_code(self):
        """The machine-readable view is what a release script gates on.

        Asserted against the rendered form in the same run, because a JSON view
        that drifted from the table would be worse than no JSON view: the table
        is reviewed by a human and the JSON is not.
        """
        self.two_ports(
            {CONTEXT: triple(), TIGON: triple()},
            {CONTEXT: triple(), TIGON: triple(differential="failed")},
        )

        text_code, text, _ = self.run_main()
        json_code, raw, _ = self.run_main("--json")
        payload = json.loads(raw)
        # An object with `comparisons` and `not_checked`, not the bare list this
        # printed until the skipped pairs were found missing from it. Asserted
        # here as well as in `FixedDefectTests` because this is the test a reader
        # looks at to learn the shape.
        comparisons = payload["comparisons"]

        self.assertEqual(json_code, text_code)
        self.assertEqual(json_code, EXIT_DROPPED)
        self.assertEqual([item["version"] for item in comparisons], ["440", "441"])
        self.assertEqual(payload["not_checked"], [])
        self.assertEqual(comparisons[-1]["dropped"], [TIGON])
        self.assertEqual(comparisons[-1]["held"], [CONTEXT])
        self.assertFalse(comparisons[-1]["met"])
        self.assertIn(f"✗ {TIGON}", text)

    def test_the_json_names_a_vanished_hook_as_vanished(self):
        """A consumer must be able to tell the two failures apart without the prose."""
        self.two_ports({CONTEXT: triple(), TIGON: triple()}, {CONTEXT: triple()})

        _, raw, _ = self.run_main("--version", "441", "--json")
        verdicts = {
            v["hook_id"]: v
            for v in json.loads(raw)["comparisons"][0]["verdicts"]
        }

        self.assertTrue(verdicts[TIGON]["vanished"])
        self.assertEqual(verdicts[TIGON]["reasons"], [])
        self.assertFalse(verdicts[CONTEXT]["vanished"])

    def test_a_single_version_run_reports_one_comparison_and_no_sweep_noise(self):
        self.two_ports({CONTEXT: triple()}, {CONTEXT: triple()})

        _, stdout, _ = self.run_main("--version", "441")

        self.assertIn("EXPECTATION  440 → 441", stdout)
        self.assertNotIn("439 → 440", stdout)

    def test_the_json_form_reports_the_pairs_it_could_not_check(self):
        """`--json` prints the comparisons and silently drops `skipped`.

        `sweep` returns both halves precisely so that "a pair nobody checked is
        never mistaken for a pair that passed", and `render_sweep` prints a NOT
        CHECKED block for the same reason. The machine-readable form a release
        script would actually consume had neither: the pair was absent from the
        output and the exit code was 0. Fixed by making `--json` emit an object
        with `comparisons` and `not_checked` rather than a bare list -- the human
        form being the honest one and the automatable form the silent one is the
        wrong way round.
        """
        self.baseline_port()
        self.port("440", {CONTEXT: triple()}, previous="439")
        self.port("441", {CONTEXT: triple()}, previous="440", runtime_file=False)

        code, raw, _ = self.run_main("--json")
        text_code, text, _ = self.run_main()

        self.assertIn("440 -> 441", text)
        self.assertEqual(text_code, code)
        self.assertIn("440 -> 441", raw)


class StandingTests(ExpectationTestCase):
    """One hook's release-readiness across the series, and the version that was
    never readable at all.

    Moved here from `tests/test_retirement.py` when `retirement` was deleted and
    `standings` came to this module; `roster` is the consumer. The distinction
    this class exists for is the one `Standing` documents and `dropped_at` cannot
    express on its own: a hook that never passed and a hook that is still passing
    both have no drop version, and they are opposite situations. Every test here
    asserts `never_release_ready` alongside `dropped_at()` for that reason.
    """

    def baseline_port(self) -> None:
        """439 as it really is: runtime evidence, no static file, no readiness.

        Overrides `ExpectationTestCase.baseline_port`, which writes a static file
        and so makes 439 *assessed*. That is right for `compare`, which only
        needs 439 in the series, and wrong here: the first test below turns on
        439 being in the series and in neither of a standing's tuples, because
        `static_verified` had no producer until 440.
        """

        self.port("439", {CONTEXT: {"runtime_probe": "passed"}}, static_file=False)

    def ordinary_corpus(self) -> None:
        """Two hooks holding, one that has never passed. 441's real shape, smaller."""

        self.two_ports(
            {
                CONTEXT: triple(),
                TIGON: triple(),
                DISCOVER: triple(runtime_probe="inconclusive"),
            },
            {
                CONTEXT: triple(),
                TIGON: triple(),
                DISCOVER: triple(runtime_probe="inconclusive"),
            },
        )

    def standings(self, **kwargs: Any) -> dict[str, Standing]:
        return standings(self.tmp, **kwargs)

    def test_a_version_whose_evidence_cannot_be_read_is_absent_and_not_zero(self):
        """439 is in the series and in neither tuple. The failure is silent.

        `static_verified` had no producer until 440, so 439's readiness is
        unknowable rather than nil. Recorded as "assessed, nothing passed", every
        hook in the manifest acquires a version on which it failed, the oldest
        one in the series — which makes a hook that has worked since 440 read as
        having been broken from the start, so every hook in the manifest looks
        like something that stopped working.
        """
        self.ordinary_corpus()

        found = self.standings()

        self.assertIn("439", versions_with_evidence(self.tmp))
        self.assertEqual(found[CONTEXT].assessed_on, ("440", "441"))
        self.assertEqual(found[CONTEXT].release_ready_on, ("440", "441"))
        self.assertFalse(found[CONTEXT].never_release_ready)
        self.assertIsNone(found[CONTEXT].dropped_at())

    def test_a_hook_that_has_never_passed_is_marked_so_and_has_no_drop_version(self):
        """A hook that has never worked, and the reason `dropped_at` is not enough.

        Three of the seven real hooks are in this state. `dropped_at()` is None
        here for the opposite reason it is None for a hook that still works, so
        both are asserted in the same corpus.
        """
        self.ordinary_corpus()

        dormant = self.standings()[DISCOVER]
        working = self.standings()[CONTEXT]

        self.assertEqual(dormant.release_ready_on, ())
        self.assertEqual(dormant.assessed_on, ("440", "441"))
        self.assertTrue(dormant.never_release_ready)
        self.assertIsNone(dormant.dropped_at())
        self.assertIsNone(dormant.last_release_ready)
        self.assertIsNone(working.dropped_at())
        self.assertFalse(working.never_release_ready)

    def test_dropped_at_is_the_first_assessed_version_after_the_last_good_one(self):
        """Not the last one. A regression is dated where it started.

        The hook fails on 441 and on 442, and the answer is 441: a reader
        deciding whether this is a regression or a dormancy needs the version to
        go and look at, and the newest failing version is the one they are
        already looking at.
        """
        self.baseline_port()
        self.port("440", {CONTEXT: triple(), TIGON: triple()}, previous="439")
        self.port(
            "441", {CONTEXT: triple(), TIGON: triple(differential="failed")},
            previous="440",
        )
        self.port(
            "442", {CONTEXT: triple(), TIGON: triple(runtime_probe="failed")},
            previous="441",
        )

        standing = self.standings()[TIGON]

        self.assertEqual(standing.release_ready_on, ("440",))
        self.assertEqual(standing.assessed_on, ("440", "441", "442"))
        self.assertEqual(standing.last_release_ready, "440")
        self.assertEqual(standing.dropped_at(), "441")
        self.assertFalse(standing.never_release_ready)

    def test_the_version_after_the_last_good_one_is_chosen_by_number(self):
        """`"1000" > "441"` is false as text. Today's three-digit arc hides it.

        Constructed directly rather than from a corpus, because the discriminating
        input is a four-digit version and a fixture that reached one would be
        asserting the series sort at the same time.
        """
        standing = Standing(TIGON, release_ready_on=("441",), assessed_on=("441", "1000"))

        self.assertEqual(standing.dropped_at(), "1000")

    def test_a_hook_absent_from_a_versions_evidence_is_absent_from_the_standing(self):
        """Assessed means "this version had something to say about this hook".

        A hook that vanished from 441's corpus is not a hook 441 found wanting,
        and the standing must not claim it was looked at.
        """
        self.two_ports({CONTEXT: triple(), TIGON: triple()}, {CONTEXT: triple()})

        found = self.standings()

        self.assertEqual(found[TIGON].assessed_on, ("440",))
        self.assertEqual(found[TIGON].release_ready_on, ("440",))
        self.assertIsNone(found[TIGON].dropped_at())
        self.assertEqual(found[CONTEXT].assessed_on, ("440", "441"))

    def test_assessed_is_not_the_same_as_release_ready(self):
        """The whole file rests on this: a red hook is measured, not missing."""
        self.two_ports(
            {CONTEXT: triple(), TIGON: triple()},
            {CONTEXT: triple(), TIGON: triple(static_verified="failed")},
        )

        standing = self.standings()[TIGON]

        self.assertIn("441", standing.assessed_on)
        self.assertNotIn("441", standing.release_ready_on)

    def test_a_tree_where_no_version_can_be_read_has_no_standings_at_all(self):
        """Empty, and not "every hook has never been release-ready".

        The difference decides whether a fresh checkout with one half-published
        port reports every hook in the manifest as broken.
        """
        self.baseline_port()

        self.assertEqual(self.standings(), {})

    def test_the_standing_serialises_its_derived_answers(self):
        """`to_dict` must carry the reading, not just the two raw tuples.

        The derived fields used to be signed as bytes inside a retirement case,
        which is why they are serialised at all. That consumer is gone; what
        remains is that a machine-readable standing must not be quieter than the
        object — the recurring defect in this repository.
        """
        self.ordinary_corpus()

        payload = self.standings()[DISCOVER].to_dict()

        self.assertEqual(payload["hook_id"], DISCOVER)
        self.assertEqual(payload["release_ready_on"], [])
        self.assertEqual(payload["assessed_on"], ["440", "441"])
        self.assertTrue(payload["never_release_ready"])
        self.assertIsNone(payload["last_release_ready"])
        self.assertIsNone(payload["dropped_at"])

    def test_the_baseline_moves_the_window_a_standing_is_computed_over(self):
        """A floor that only ever excluded the same version could be a coincidence.

        With 441 as the floor the series is one version long, so 441 has no
        differential pair to be read against and nothing is release-ready — the
        same reason 439 establishes the bar rather than meeting it. The point
        here is that the window moved, and both halves of the standing move with
        it.
        """
        self.ordinary_corpus()

        default = self.standings()[CONTEXT]
        raised = self.standings(baseline="441")[CONTEXT]

        self.assertEqual(default.assessed_on, ("440", "441"))
        self.assertEqual(default.release_ready_on, ("440", "441"))
        self.assertEqual(raised.assessed_on, ("441",))
        self.assertEqual(raised.release_ready_on, ())

    def test_readiness_comes_from_the_same_report_the_release_gate_reads(self):
        """Not a second derivation, which would agree until one of them was edited.

        Asserted by removing a required kind rather than by inspecting the call:
        readiness is exactly "all three post-build kinds passed", so a hook whose
        differential was never recorded is not release-ready however green the
        other two are.
        """
        self.two_ports({CONTEXT: triple()}, {CONTEXT: triple(differential=None)})

        standing = self.standings()[CONTEXT]

        self.assertEqual(standing.release_ready_on, ("440",))
        self.assertEqual(standing.assessed_on, ("440", "441"))


if __name__ == "__main__":
    unittest.main()

"""`retirement.py` is the only thing that may lower the release-ready bar.

`expectation` asserts that every hook release-ready on N-1 is release-ready on N
and refuses to let that fall without a recorded retirement. It has read
`manifest/retirements.jsonl` since the day it was written and nothing had ever
produced one. This module is the producer, so every test here is a test of the
one escape hatch in the pipeline — and an escape hatch is not tested by asking
whether it opens. It is tested by asking what could open it that should not.

The properties pinned here are the ones where being wrong is invisible:

**`effective_from` is derived, never chosen.** :class:`EffectiveFromTests`. A
retirement that could name its own effective version could be backdated onto the
port that exposed the drop, which is "approve your way out of a red build"
wearing a date. Three paths are attacked separately — the CLI has no flag,
`build_case` computes `version + 1` arithmetically, and `RetirementCase.from_dict`
re-derives it rather than trusting the file that travelled between the machine
that built the case and the human who signed it. The fourth path, direct dataclass
construction, is :class:`KnownDefectTests` 1.

**An agent may not rule, and a recommendation is not a verdict.**
:class:`RulingTests`. `ruled_by: agent` is refused in every casing by the one
function both `rule()` and `publish()` call, and the discriminating test is the
one where the two vocabularies disagree: an investigation recommending `retire`
and a human ruling `keep` produces a `keep` and writes nothing. A module that
read the recommendation anywhere would pass every other test in this file.

**A ruling answers exact bytes.** :class:`BindingTests`. Every field of a case is
edited one at a time after the ruling is made, and every one of them makes
`publish` refuse. The realistic version of the same test is the last one in the
class: evidence arrives between the case being raised and the case being
answered, and the stale answer is refused rather than applied to a picture nobody
saw.

**A `keep` is an answer and not a row.** :class:`PublishTests`. Writing a row
that says "still expected" would put a hook in a file whose only meaning is "no
longer expected", and a second row for a hook that already has one cannot change
anything, because the reader takes the earliest `effective_from`.

**The two ends are connected.** :class:`RoundTripTests`. This project has
repeatedly shipped gates that were complete and disconnected at one end — a gate
with no producer, a producer whose rulings had no consumer, a required evidence
kind nothing emitted. So the important test in this file is not that `publish`
writes a well-formed row; it is that `expectation` reads that row, does *not*
excuse the port the case was built from, and does excuse the one after it. Both
directions are asserted in the same corpus against a before-and-after control,
because a retirement that excused everything and a retirement that excused
nothing look identical on a corpus with one version in it.

===============================================================================
  FIXTURE PROVENANCE
===============================================================================

Every corpus is a temp tree in the real layout — `manifest/hooks.json`,
`manifest/static_evidence/<v>.jsonl`, `manifest/runtime_evidence/<v>.jsonl`,
`manifest/differentials/<prev>-<v>.jsonl` — with the rows in the flat on-disk
shape copied from the committed files, so the JSON the module parses is the JSON
under test. The helpers are deliberately the same ones
`tests/test_expectation.py` uses, because both modules read the same corpus and a
fixture that diverged would let the two agree about a tree that cannot exist.

Nothing here writes to the repository's own `manifest/`. `publish` takes `root=`
and `path=` for exactly that reason and every call passes one: a durable store
whose writer has no seam is one every test writes to, and this project shipped 36
rows of fixture data into the committed evidence corpus that way.

439 is present in every corpus with runtime evidence and no static file, which is
its real state — `static_verified` had no producer until 440. It is therefore in
the series and has no computable readiness, and that combination is the whole
point of :class:`StandingTests`: a version that could not be read must be absent
from a standing rather than recorded as a version where nothing passed. Recorded
as zero, every hook in the manifest becomes a retirement candidate at once.

===============================================================================
  MUTATION RESULTS
===============================================================================

Sixty-six mutations were applied one at a time to an out-of-tree copy of the
repository, each to a file restored from a pristine copy with every
`__pycache__` cleared before the run — a mutate-run-restore cycle inside one
second that does not change the file's size is invisible to the bytecode cache,
and the interpreter then measures the code that is not on disk. The unmutated
copy passed first as the control and again at the end. The bracketed number is
how many distinct tests in this file failed.

One survived the first pass: the case hardcoding `status` as `active`, which
every fixture's manifest happened to agree with.
:meth:`BuildCaseTests.test_the_case_quotes_the_manifest_rather_than_assuming_its_defaults`
was written for it, and it and its two neighbours are caught now.

Lowering the bar:

* `RetirementCase.from_dict` trusts the file's `effective_from` [2] and drops
  the re-derivation refusal [2]; `build_case` derives `version + 0` [24] →
  :class:`EffectiveFromTests`
* the published row carries the case's own version rather than the derived one
  [11] → :class:`EffectiveFromTests`, :class:`RoundTripTests`
* `validate_ruling` compares `ruled_by` without `.lower()` [1] and drops the
  agent check entirely [2] → :class:`RulingTests`
* `rule()` takes its verdict from `investigation.recommendation` [7] →
  :class:`RulingTests`, :class:`PublishTests`, :class:`RoundTripTests`
* `validate_ruling` skips the `case_sha256` comparison [6], `rule()` does not
  validate what it mints [5], and the ruling is minted against a subject that is
  not the case's [36] → :class:`BindingTests` and everything that publishes
* `publish` writes for every verdict [4], skips the already-retired check [2],
  truncates rather than appends [2], and drops the round trip through
  `Retirement.from_dict` [1] → :class:`PublishTests`

Not refusing what should be refused:

* `Investigation.__post_init__` drops the blank-investigator [1], blank-summary
  [2] and recommendation [2] checks; `from_dict` accepts unknown keys [3] and a
  non-object [1] → :class:`InvestigationTests`
* `rule()` stops requiring a timestamp [1]; `validate_ruling` stops requiring a
  rationale [1], a name [1] and a decision id [1], and drops the verdict
  vocabulary [1] and hook id [1] checks → :class:`RulingTests`
* `build_case` drops the unknown-hook [2], already-retired [1], no-evidence [1]
  and not-assessed-here [2] checks → :class:`BuildCaseTests`
* `RetirementCase.from_dict` drops the schema [1] and standing-hook [1] checks;
  `candidates` drops the version guard [2] → :class:`SubjectTests`,
  :class:`CandidateTests`
* `decision_id` is a constant [2] → :class:`RulingTests`, :class:`CliTests`

Reading the evidence wrongly:

* `standings` records an uncomputable version as assessed-but-not-ready [15],
  lets the refusal propagate [82], reads every version against the first of the
  series [11], and keeps only hooks that were release-ready somewhere [61] →
  :class:`StandingTests` and everything downstream of a standing
* `Standing.dropped_at` returns the last assessed version [1] or compares
  versions as strings [1]; `never_release_ready` is inverted [8];
  `last_release_ready` returns the first good version [2] →
  :class:`StandingTests`
* `candidates` includes already-retired hooks [2], hooks not assessed at the
  version [2], and hooks that are release-ready [7] → :class:`CandidateTests`
* `case_sha256` leaves the investigation [2] or the standing [3] out of the
  subject; `Investigation.to_dict` drops the findings [5]; `Ruling.to_dict`
  drops the subject it answers [6] → :class:`SubjectTests`,
  :class:`BindingTests`
* the case hardcodes `status` [1] or `tier` [1], reads the wrong manifest key
  for the status [1], or records no intent [2] → :class:`BuildCaseTests`

The interface:

* `main` exits 0 on a refusal [10], stops catching the module's own error [9],
  stops catching `OSError` and `ValueError` [1] → :class:`CliTests`
* the `case` [10] and `rule` [6] subcommands do not write `--out`; `publish`
  ignores `--retirements` [1], ignores `path=` [4], checks the wrong file for an
  existing retirement [1], and does not create the directory it writes into [2]
  → :class:`CliTests`, :class:`PublishTests`
* `render_candidates` stops labelling a regression [1] or a dormancy [1];
  `render_case` drops the advisory note on the recommendation [1]; `publish`
  stops telling the reader to commit the row [1] → :class:`CandidateTests`,
  :class:`BuildCaseTests`, :class:`CliTests`

===============================================================================
  KNOWN DEFECTS
===============================================================================

:class:`KnownDefectTests` are `expectedFailure` on purpose — the convention
`tests/test_expectation.py`, `tests/test_history.py` and `tests/test_reaper.py`
each used for a defect their own tests found. Each asserts what this module's
docstrings say must happen and what the code does not do, so the suite stays
green today and reports an *unexpected success* the moment one is closed.

1. `RetirementCase` has no `__post_init__`, so the re-derivation of
   `effective_from` lives only in `from_dict`. A case built by calling the
   dataclass directly may say anything, and `rule()` and `publish()` both accept
   it: `RetirementCase(..., version="441", effective_from="441", ...)` publishes a
   row that stops expecting the hook on the very port that exposed the drop. The
   CLI cannot reach it — both `rule` and `publish` load through `from_dict` — but
   `publish` is the only writer of the file and is the authority, and the
   authority does not check.
2. `validate_ruling` does not require `ruled_at`. `rule()` does, and its
   docstring gives the reason ("a record stamped by whoever happened to run it is
   one no reader can order") — but `validate_ruling`'s own docstring says it is
   the authority and that it "runs even when the ruling arrives as a file from
   somewhere else", which is the case where the check is missing. A hand-written
   `ruling.json` with `"ruled_at": ""` publishes a retirement whose `recorded_at`
   is empty, through the ordinary `publish` subcommand.
3. The same gap for `decision_id`. `rule()` derives it from the answer so that
   "two identical answers deduplicate and two different ones cannot collide", and
   `validate_ruling` only asks that it be non-blank — so the property holds of
   rulings this module mints and of no other, and two different answers carried
   in hand-written files can share an id.
4. A non-iterable `findings` is a `TypeError` rather than a refusal.
   `{"findings": null}` — which is what a drafting tool that has found nothing
   would write — reaches `tuple(str(item) for item in ...)` and leaves the `case`
   subcommand as a traceback with exit 1, not `refused:` with exit 2. `main`
   catches `RetirementError`, `ExpectationError`, `ValueError` and `OSError`, and
   `TypeError` is none of them. The same shape sits in `RetirementCase.from_dict`
   for `release_ready_on` and `assessed_on`.
"""

from __future__ import annotations

import dataclasses
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Iterable, Mapping

from dfinsta_pipeline import retirement
from dfinsta_pipeline.contracts import canonical_sha256
from dfinsta_pipeline.expectation import (
    RETIREMENTS,
    ExpectationError,
    Retirement,
    compare,
    read_retirements,
    render,
    retired_by,
    versions_with_evidence,
)
from dfinsta_pipeline.history import BASELINE_VERSION
from dfinsta_pipeline.retirement import (
    RECOMMENDATIONS,
    VERDICTS,
    Investigation,
    RetirementCase,
    RetirementError,
    Ruling,
    Standing,
    build_case,
    candidates,
    case_sha256,
    publish,
    render_candidates,
    render_case,
    rule,
    standings,
    validate_ruling,
)

# ------------------------------------------------------------------- fixtures

#: Real hook ids and their real manifest text. A case prints the intent so a
#: human can see what is being lost, so the fixture carries the real sentence:
#: `hook_a` with `intent: "does a thing"` would let this file agree with a
#: manifest that could not exist.
MANIFEST_HOOKS: dict[str, tuple[str, str]] = {
    "set_app_context": (
        "Capture the Application context at process start so custom code can "
        "read SharedPreferences",
        "robust",
    ),
    "tigon_url_block": (
        "Fail outgoing API requests whose URI path is blocked, as a normal "
        "request failure",
        "robust",
    ),
    "replace_reels_discover_endpoint": (
        "Blank the Reels discover request path when Reels are disabled",
        "robust",
    ),
    "install_settings_long_click": (
        "Open the DFInsta settings dialog by long-pressing Options on the "
        "user's own profile (ProfileActionBar variant)",
        "ui",
    ),
    "install_settings_long_click_actionbar": (
        "Open the DFInsta settings dialog by long-pressing Options on the "
        "user's own profile (legacy IgActionBar variant)",
        "ui",
    ),
}

CONTEXT = "set_app_context"
TIGON = "tigon_url_block"
SETTINGS = "install_settings_long_click"
ACTIONBAR = "install_settings_long_click_actionbar"
DISCOVER = "replace_reels_discover_endpoint"

DEVICE = "device:P3227J000775"
VERIFIER = "tools/verify/verify_build.py"
STAMP = "2026-08-07T12:05:00Z"
RULED_AT = "2026-08-08T09:30:00Z"
BUILD = "64ca7eecb4520bb0e7c3667c52be835f2454f9444b8e343fd934fe841ae539b4"

#: The three post-build kinds and the producer each one allows, from
#: `evidence.ALLOWED_PRODUCERS`. A `static_verified` row naming a device is
#: refused at parse time and the fixture would be testing the schema instead.
PRODUCERS = {
    "static_verified": ("deterministic", VERIFIER),
    "runtime_probe": ("device", DEVICE),
    "differential": ("device", DEVICE),
}


def evidence_row(hook_id: str, kind: str, verdict: str, version: str) -> dict[str, Any]:
    """One claim in the flat on-disk shape `manifest/` actually holds."""

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
    A kind set to ``None`` is dropped rather than recorded with a bad verdict:
    a failed differential is a regression this port caused and a missing one
    means nobody measured, and the module tells them apart.
    """

    kinds: dict[str, str | None] = {
        "static_verified": "passed",
        "runtime_probe": "passed",
        "differential": "passed",
    }
    kinds.update(overrides)
    return {kind: verdict for kind, verdict in kinds.items() if verdict is not None}


def investigation_row(**overrides: Any) -> dict[str, Any]:
    """A well-formed investigation, in the shape the `--investigation` file holds."""

    row: dict[str, Any] = {
        "investigated_by": "agent:claude-opus-5",
        "summary": (
            "The long-press surface this hook attaches to is gone from 441: the "
            "anchor resolves, the payload applies, and the class it patches is "
            "never instantiated."
        ),
        "findings": [
            "the host class has no remaining call site in classes6.dex",
            "the probe has never announced execution on any version",
        ],
        "recommendation": "retire",
    }
    row.update(overrides)
    return row


def investigation(**overrides: Any) -> Investigation:
    return Investigation.from_dict(investigation_row(**overrides))


class RetirementTestCase(unittest.TestCase):
    """A temp tree in the real layout, and a way to run `main` against it."""

    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        self.manifest = self.tmp / "manifest"
        for name in ("static_evidence", "runtime_evidence", "differentials"):
            (self.manifest / name).mkdir(parents=True)
        self.hooks()

    # ------------------------------------------------------------- the corpus

    def hooks(self, *hook_ids: str, **overrides: Any) -> Path:
        """`manifest/hooks.json`, carrying the three fields a case quotes.

        The real file also carries anchors, payloads and probes; a case reads
        `intent`, `tier` and `status` and nothing else, so those are the ones
        pinned here — with `strategy` alongside them so the shape is a subset of
        the real record rather than a different record.
        """

        chosen = hook_ids or tuple(MANIFEST_HOOKS)
        declared = []
        for hook_id in chosen:
            intent, tier = MANIFEST_HOOKS[hook_id]
            entry: dict[str, Any] = {
                "hook_id": hook_id,
                "intent": intent,
                "tier": tier,
                "strategy": "ui_attach",
                "status": "active",
            }
            entry.update(overrides)
            declared.append(entry)
        path = self.manifest / "hooks.json"
        path.write_text(
            json.dumps({"schema_version": 1, "hooks": declared}, indent=2),
            encoding="utf-8",
        )
        return path

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
        """One version's durable evidence, split across the three directories."""

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
        """439 as it really is: runtime evidence, no static file, no readiness.

        In the series, because 440's differential is named `439-440.jsonl` and
        the pair has to exist for 440 to be release-ready in anything — and not
        computable, because `static_verified` had no producer until 440. Every
        corpus here carries it so that "assessed" and "in the series" are
        different things in every test rather than only in the one about it.
        """

        self.port("439", {CONTEXT: {"runtime_probe": "passed"}}, static_file=False)

    def two_ports(
        self,
        on_440: Mapping[str, Mapping[str, str]],
        on_441: Mapping[str, Mapping[str, str]],
    ) -> None:
        self.baseline_port()
        self.port("440", dict(on_440), previous="439")
        self.port("441", dict(on_441), previous="440")

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

    # -------------------------------------------------------------- shortcuts

    def standings(self, **kwargs: Any) -> dict[str, Standing]:
        return standings(self.tmp, **kwargs)

    def candidates(self, **kwargs: Any) -> list[Standing]:
        return candidates(self.tmp, **kwargs)

    def build(
        self, hook_id: str = DISCOVER, version: str = "441", **overrides: Any
    ) -> RetirementCase:
        return build_case(
            self.tmp,
            hook_id=hook_id,
            version=version,
            investigation=investigation(**overrides),
        )

    def signed(
        self,
        case: RetirementCase | None = None,
        *,
        verdict: str = "retire",
        rationale: str = "The surface is gone from the app; the anchor is dead code.",
        ruled_by: str = "arnav",
        ruled_at: str = RULED_AT,
    ) -> tuple[RetirementCase, Ruling]:
        case = self.build() if case is None else case
        return case, rule(
            case,
            verdict=verdict,
            rationale=rationale,
            ruled_by=ruled_by,
            ruled_at=ruled_at,
        )

    def rows(self, path: Path | None = None) -> list[dict[str, Any]]:
        location = self.tmp / RETIREMENTS if path is None else path
        if not location.is_file():
            return []
        return [
            json.loads(line)
            for line in location.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def file_at(self, name: str, payload: Any) -> Path:
        path = self.tmp / name
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    def run_main(self, *args: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = retirement.main(["--root", str(self.tmp), *args])
        return code, stdout.getvalue(), stderr.getvalue()


# ================================================================== standings


class StandingTests(RetirementTestCase):
    """One hook's release-readiness across the series, and the version that was
    never readable at all.

    The distinction this class exists for is the one `Standing` documents and
    `dropped_at` cannot express on its own: a hook that never passed and a hook
    that is still passing both have no drop version, and they are opposite
    situations. Every test here asserts `never_release_ready` alongside
    `dropped_at()` for that reason.
    """

    def test_a_version_whose_evidence_cannot_be_read_is_absent_and_not_zero(self):
        """439 is in the series and in neither tuple. The failure is silent.

        `static_verified` had no producer until 440, so 439's readiness is
        unknowable rather than nil. Recorded as "assessed, nothing passed", every
        hook in the manifest acquires a version on which it failed, the oldest
        one in the series — which makes a hook that has worked since 440 read as
        having been broken from the start, and turns the whole manifest into
        retirement candidates.
        """
        self.ordinary_corpus()

        found = self.standings()

        self.assertIn("439", versions_with_evidence(self.tmp))
        self.assertEqual(found[CONTEXT].assessed_on, ("440", "441"))
        self.assertEqual(found[CONTEXT].release_ready_on, ("440", "441"))
        self.assertFalse(found[CONTEXT].never_release_ready)
        self.assertIsNone(found[CONTEXT].dropped_at())

    def test_a_hook_that_has_never_passed_is_marked_so_and_has_no_drop_version(self):
        """The honest retirement candidate, and the reason `dropped_at` is not enough.

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
        port offers the whole manifest for retirement.
        """
        self.baseline_port()

        self.assertEqual(self.standings(), {})

    def test_the_standing_serialises_the_derived_answers_a_case_carries(self):
        """A case is signed as bytes, so the derived fields are part of the subject."""
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


# ================================================================= candidates


class CandidateTests(RetirementTestCase):
    """The list exists so nobody has to remember which hooks are quietly failing.

    Being on it is never an argument for retiring — a hook that dropped last week
    belongs on it exactly so that somebody looks at it — so the tests are about
    what must not appear: a hook already retired, and a hook nobody assessed.
    """

    def test_a_hook_not_release_ready_at_that_version_is_a_candidate(self):
        """With the working hooks in the same corpus as the control."""
        self.ordinary_corpus()

        found = self.candidates(version="441")

        self.assertEqual([standing.hook_id for standing in found], [DISCOVER])
        self.assertNotIn(CONTEXT, [standing.hook_id for standing in found])

    def test_a_hook_already_retired_is_not_offered_for_retirement_again(self):
        """Before and after the row, in one test, so the exclusion is the row's doing."""
        self.ordinary_corpus()
        case, ruling = self.signed(self.build(DISCOVER, "441"))

        before = [standing.hook_id for standing in self.candidates(version="441")]
        publish(case, ruling, root=self.tmp)
        after = [standing.hook_id for standing in self.candidates(version="441")]

        self.assertEqual(before, [DISCOVER])
        self.assertEqual(after, [])

    def test_a_hook_not_assessed_at_that_version_is_not_a_candidate(self):
        """It was not measured and found wanting; it was not measured.

        Offering it would invite a retirement argued from an absence of evidence,
        which is the one argument this module exists to make expensive.
        """
        self.two_ports({CONTEXT: triple(), TIGON: triple()}, {CONTEXT: triple()})

        found = [standing.hook_id for standing in self.candidates(version="441")]

        self.assertEqual(found, [])
        self.assertEqual(
            [standing.hook_id for standing in self.candidates(version="440")], []
        )

    def test_no_hook_is_a_candidate_at_a_version_that_could_not_be_read(self):
        """439 again, from the other end. The loudest way this could go wrong.

        Every hook in the manifest offered for retirement at once, because one
        version of the series has half a corpus, is exactly the output that would
        get somebody to approve a retirement they should not.
        """
        self.ordinary_corpus()

        self.assertEqual(self.candidates(version="439"), [])

    def test_a_version_that_is_not_a_number_is_refused_before_anything_is_read(self):
        for version in ("441-rc1", "", "nope", "44.1"):
            with self.subTest(version=version):
                with self.assertRaises(RetirementError):
                    self.candidates(version=version)

    def test_a_regression_and_a_dormancy_are_labelled_differently(self):
        """"Not release-ready" flattens two situations with opposite answers.

        The regression gets "fix these, do not retire them" and the version it
        was last good on; the dormancy gets "the honest candidates". A rendering
        that printed one list would be true and would lose the distinction the
        whole module is built around.
        """
        self.two_ports(
            {CONTEXT: triple(), TIGON: triple(), DISCOVER: triple(runtime_probe="inconclusive")},
            {
                CONTEXT: triple(),
                TIGON: triple(differential="failed"),
                DISCOVER: triple(runtime_probe="inconclusive"),
            },
        )

        text = render_candidates(self.candidates(version="441"), "441")

        self.assertIn("REGRESSIONS (1) — fix these, do not retire them:", text)
        self.assertIn(f"! {TIGON}", text)
        self.assertIn("last release-ready on 440, dropped at 441", text)
        self.assertIn("NEVER RELEASE-READY (1) — the honest candidates:", text)
        self.assertIn(f"· {DISCOVER}", text)
        self.assertIn("A candidate is a hook worth looking at, never an argument", text)

    def test_an_empty_list_says_so_rather_than_printing_an_empty_heading(self):
        """A heading with nothing under it reads as a rendering bug, not as news."""
        self.two_ports({CONTEXT: triple()}, {CONTEXT: triple()})

        text = render_candidates(self.candidates(version="441"), "441")

        self.assertIn("None. Every assessed hook is release-ready", text)
        self.assertNotIn("REGRESSIONS", text)
        self.assertNotIn("NEVER RELEASE-READY", text)


# ============================================================== investigations


class InvestigationTests(unittest.TestCase):
    """What an agent found. Evidence for a human, and never a verdict.

    Each refusal is exercised on its own rather than as "a bad investigation is
    refused", because the failure they prevent is not a malformed file: it is a
    case that reads as though somebody looked into something when nobody did.
    An investigation with a blank summary still renders, still hashes, and still
    gets signed.
    """

    def test_a_complete_investigation_is_accepted(self):
        """The positive control. Every refusal below is satisfied by refusing all."""
        found = investigation()

        self.assertEqual(found.investigated_by, "agent:claude-opus-5")
        self.assertIn("long-press surface", found.summary)
        self.assertEqual(len(found.findings), 2)
        self.assertEqual(found.recommendation, "retire")

    def test_an_investigation_that_does_not_say_who_ran_it_is_refused(self):
        for value in ("", "   ", "\n"):
            with self.subTest(investigated_by=value):
                with self.assertRaises(RetirementError) as caught:
                    investigation(investigated_by=value)

                self.assertIn("who ran it", str(caught.exception))

    def test_an_investigation_with_no_summary_is_not_evidence(self):
        """A case is read by a human. An empty summary is a red number with a name on it."""
        for value in ("", "   ", "\t\n"):
            with self.subTest(summary=value):
                with self.assertRaises(RetirementError) as caught:
                    investigation(summary=value)

                self.assertIn("no summary is not evidence", str(caught.exception))

    def test_an_unknown_recommendation_is_refused_and_the_message_lists_them(self):
        for value in ("maybe", "RETIRE", "", "yes"):
            with self.subTest(recommendation=value):
                with self.assertRaises(RetirementError) as caught:
                    investigation(recommendation=value)

                self.assertIn("unknown recommendation", str(caught.exception))
                self.assertIn("retire, keep, unclear", str(caught.exception))

    def test_the_recommendation_vocabulary_is_not_the_verdict_vocabulary(self):
        """Deliberately different words, so the two cannot be interchanged by accident.

        An agent cannot `defer` — that is a statement about a human's attention —
        and its `unclear` has no counterpart in a ruling. If the two tuples were
        the same, code that passed a recommendation where a verdict was wanted
        would work, and this whole module's one rule would be one refactor from
        gone.
        """
        self.assertNotEqual(set(RECOMMENDATIONS), set(VERDICTS))
        self.assertNotIn("defer", RECOMMENDATIONS)
        self.assertNotIn("unclear", VERDICTS)

        with self.assertRaises(RetirementError):
            investigation(recommendation="defer")

    def test_an_unknown_key_is_refused_rather_than_dropped(self):
        """`{"verdict": "retire"}` in an investigation file must not be ignored.

        Silently dropping it is the dangerous reading: the author believes they
        answered the case, the case records no answer, and the difference is
        invisible until somebody signs it.
        """
        for key in ("verdict", "ruled_by", "effective_from", "sumary"):
            with self.subTest(key=key):
                with self.assertRaises(RetirementError) as caught:
                    investigation(**{key: "retire"})

                self.assertIn("unknown keys", str(caught.exception))
                self.assertIn(key, str(caught.exception))

    def test_two_unknown_keys_are_reported_together_and_in_order(self):
        with self.assertRaises(RetirementError) as caught:
            Investigation.from_dict(
                {"investigated_by": "x", "summary": "y", "zeta": 1, "alpha": 2}
            )

        self.assertIn("alpha, zeta", str(caught.exception))

    def test_something_that_is_not_an_object_is_refused_by_type(self):
        """A JSON file holding a list or a bare string reaches this directly."""
        for value in ([], ["a"], "text", 3, None):
            with self.subTest(value=value):
                with self.assertRaises(RetirementError) as caught:
                    Investigation.from_dict(value)

                self.assertIn("must be a JSON object", str(caught.exception))

    def test_the_recommendation_defaults_to_unclear_when_it_is_not_given(self):
        """The honest default. An omitted recommendation is not a recommendation to keep."""
        found = Investigation.from_dict({"investigated_by": "x", "summary": "y"})

        self.assertEqual(found.recommendation, "unclear")
        self.assertEqual(found.findings, ())

    def test_an_investigation_round_trips_through_to_dict(self):
        """`to_dict` is what the case carries and what a reader signs."""
        original = investigation()

        self.assertEqual(original, Investigation.from_dict(original.to_dict()))
        self.assertEqual(original.to_dict()["findings"], list(original.findings))


# ============================================================ building a case


class BuildCaseTests(RetirementTestCase):
    """It will not build a case it cannot reproduce, or one there is no case for.

    The four refusals are four different ways of being asked to argue from
    nothing: a hook that is not in the manifest, one whose case is already
    closed, one no version has ever assessed, and one this version did not
    assess.
    """

    def test_a_case_carries_what_the_hook_is_for_and_not_only_that_it_is_red(self):
        """A retirement argued purely from red numbers is argued without knowing
        what is being lost, so the manifest's own sentence travels with the case.
        """
        self.ordinary_corpus()

        case = self.build(DISCOVER, "441")

        self.assertEqual(case.hook_id, DISCOVER)
        self.assertEqual(case.version, "441")
        self.assertEqual(case.intent, MANIFEST_HOOKS[DISCOVER][0])
        self.assertEqual(case.tier, "robust")
        self.assertEqual(case.status, "active")
        self.assertEqual(case.standing.assessed_on, ("440", "441"))
        self.assertTrue(case.standing.never_release_ready)

    def test_the_same_two_arguments_give_byte_identical_bytes(self):
        """What makes signing a hash mean anything on somebody else's machine.

        The investigation is the only other input, and it is supplied identically,
        so two builds must agree exactly — not merely compare equal field by
        field, which a hash does not care about.
        """
        self.ordinary_corpus()

        first = self.build(DISCOVER, "441")
        second = self.build(DISCOVER, "441")

        self.assertEqual(case_sha256(first), case_sha256(second))
        self.assertEqual(
            canonical_sha256(first.to_dict()), canonical_sha256(second.to_dict())
        )

    def test_a_hook_that_is_not_in_the_manifest_is_refused(self):
        """Its evidence needs dealing with, not a retirement.

        A retirement says "stop expecting this hook". A hook already absent from
        the manifest is not expected by anything, so the row would be a permanent
        record of a decision about nothing — and it would hide the real problem,
        which is evidence naming a hook that does not exist.
        """
        self.ordinary_corpus()

        with self.assertRaises(RetirementError) as caught:
            self.build("install_probe_long_click", "441")

        self.assertIn("not in the hook manifest", str(caught.exception))

    def test_a_hook_that_already_has_a_retirement_cannot_have_a_second_case(self):
        """And the refusal says where the first one is, so the reader can go and read it."""
        self.ordinary_corpus()
        case, ruling = self.signed(self.build(DISCOVER, "441"))
        publish(case, ruling, root=self.tmp)

        with self.assertRaises(RetirementError) as caught:
            self.build(DISCOVER, "441")

        self.assertIn("already retired at 442", str(caught.exception))
        self.assertIn("arnav", str(caught.exception))
        self.assertIn(ruling.decision_id, str(caught.exception))

    def test_a_hook_with_no_assessable_evidence_anywhere_is_refused(self):
        """Declared in the manifest and measured by nothing. There is no case to make.

        This is a hook added to the manifest and never ported, and it is the
        shape most likely to be waved through: the candidate list will never
        offer it, so the only way to reach it is to name it by hand.
        """
        self.ordinary_corpus()

        with self.assertRaises(RetirementError) as caught:
            self.build(ACTIONBAR, "441")

        self.assertIn("no assessable evidence", str(caught.exception))
        self.assertIn(BASELINE_VERSION, str(caught.exception))

    def test_a_hook_not_assessed_on_the_requested_version_is_refused_by_name(self):
        """Assessed somewhere, not here. The message lists where it was."""
        self.two_ports({CONTEXT: triple(), TIGON: triple()}, {CONTEXT: triple()})

        with self.assertRaises(RetirementError) as caught:
            self.build(TIGON, "441")

        self.assertIn("was not assessed on 441", str(caught.exception))
        self.assertIn("assessed on 440", str(caught.exception))

    def test_a_version_that_could_not_be_read_is_not_a_version_to_build_from(self):
        """439 has runtime evidence and no readiness, so no case can be built from it."""
        self.ordinary_corpus()

        with self.assertRaises(RetirementError) as caught:
            self.build(CONTEXT, "439")

        # The message moved with the ceiling: bounded at 439 the series holds only
        # 439, which is not computable, so the hook has no standing at all rather
        # than a standing that omits 439.
        self.assertIn("no assessable evidence", str(caught.exception))

    def test_a_version_that_is_not_a_number_is_refused(self):
        self.ordinary_corpus()

        for version in ("441-rc1", "", "next"):
            with self.subTest(version=version):
                with self.assertRaises(RetirementError):
                    self.build(DISCOVER, version)

    def test_a_case_may_be_built_for_a_hook_that_is_still_passing(self):
        """Deliberate, and worth pinning so it is not "fixed" into a refusal.

        A surface Instagram removed can keep passing a static check for a version
        while the feature is already gone, and `expectation` reports a retired
        hook that still passes as STILL PASSING rather than as an error. The
        argument against retiring it belongs to the human reading the case, and
        the case says in words that this is a regression rather than a dormancy.
        """
        self.ordinary_corpus()

        case = self.build(CONTEXT, "441")

        self.assertEqual(case.standing.release_ready_on, ("440", "441"))
        self.assertIn("THIS IS A REGRESSION", render_case(case))

    def test_the_case_quotes_the_manifest_rather_than_assuming_its_defaults(self):
        """`tier` and `status` are the manifest's words and not this module's.

        A case that hardcoded `active` would tell a human the hook is live at the
        exact moment they are deciding whether it still should be, and the
        manifest is where that word comes from. The tier is asserted on a `ui`
        hook because `robust` is the common value and a constant would pass on
        it. The default is exercised too, on an entry with no `status` at all —
        that is the one case where the module is allowed to supply the word.
        """
        self.two_ports(
            {SETTINGS: triple(), CONTEXT: triple()},
            {SETTINGS: triple(runtime_probe="failed"), CONTEXT: triple()},
        )
        self.hooks(SETTINGS, CONTEXT, status="experimental")

        declared = self.build(SETTINGS, "441")

        path = self.manifest / "hooks.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        for entry in payload["hooks"]:
            del entry["status"]
        path.write_text(json.dumps(payload), encoding="utf-8")
        defaulted = self.build(SETTINGS, "441")

        self.assertEqual(declared.status, "experimental")
        self.assertEqual(declared.tier, "ui")
        self.assertEqual(declared.intent, MANIFEST_HOOKS[SETTINGS][0])
        self.assertEqual(defaulted.status, "active")

    def test_the_case_records_the_investigation_verbatim(self):
        """Including a recommendation the human is about to disagree with."""
        self.ordinary_corpus()

        case = self.build(DISCOVER, "441", recommendation="keep")

        self.assertEqual(case.investigation.recommendation, "keep")
        self.assertEqual(case.investigation.investigated_by, "agent:claude-opus-5")
        self.assertEqual(
            case.to_dict()["investigation"]["findings"],
            investigation_row()["findings"],
        )

    def test_the_rendered_case_says_the_recommendation_is_not_a_verdict(self):
        """The reader has to know that the agent's answer is not the answer."""
        self.ordinary_corpus()

        text = render_case(self.build(DISCOVER, "441"))

        self.assertIn("recommendation: retire   (advisory", text)
        self.assertIn("nothing reads this as a verdict", text)
        self.assertIn("Only a human may rule", text)
        self.assertIn(f"subject           {case_sha256(self.build(DISCOVER, '441'))}", text)


# ============================================================= effective_from


class EffectiveFromTests(RetirementTestCase):
    """Derived as `version + 1`, and there is no way to say otherwise.

    A retirement that could name its own effective version could be backdated
    onto the port that exposed the drop, which is the "approve your way out of a
    red build" failure wearing a date. The value is computed in `build_case`,
    re-derived in `RetirementCase.from_dict`, and absent from the command line —
    three places, tested separately, because a case file travels between the
    machine that built it and the human who signs it and is editable in between.
    """

    def test_a_case_takes_effect_at_the_version_after_the_one_it_was_built_from(self):
        self.ordinary_corpus()

        self.assertEqual(self.build(DISCOVER, "441").effective_from, "442")
        self.assertEqual(self.build(DISCOVER, "440").effective_from, "441")

    def test_the_derivation_is_arithmetic_and_not_string_work(self):
        """`"999" + 1` is `"1000"`, and every string-shaped guess gets it wrong.

        Concatenation would give `"9991"`, and a rule that appended a digit or
        bumped the last character would pass on every three-digit version this
        project has ever had.
        """
        self.baseline_port()
        self.port("999", {CONTEXT: triple(differential=None)})
        self.port("1000", {CONTEXT: triple()}, previous="999")

        self.assertEqual(self.build(CONTEXT, "1000").effective_from, "1001")

    def test_a_case_file_that_names_its_own_effective_version_is_refused(self):
        """Every wrong value, and the one that matters most is the backdated one.

        `effective_from == version` is the single character edit that makes a
        retirement excuse the port that exposed the drop. It is tested beside
        values that are merely wrong so that the check cannot be satisfied by
        something that only rejects nonsense.
        """
        self.ordinary_corpus()
        payload = self.build(DISCOVER, "441").to_dict()

        for value in ("441", "440", "443", "", "442-rc1", "nope"):
            with self.subTest(effective_from=value):
                with self.assertRaises(RetirementError) as caught:
                    RetirementCase.from_dict({**payload, "effective_from": value})

                self.assertIn("takes effect at 442", str(caught.exception))
                self.assertIn("derived, not chosen", str(caught.exception))
                self.assertIn("excuse the port", str(caught.exception))

    def test_a_case_that_was_not_edited_survives_the_round_trip(self):
        """The positive control, and the reason it is a round trip and not a re-read.

        `rule` and `publish` both read the case from a file, so the file's bytes
        must reconstruct a case with the same subject hash — otherwise the
        binding in the next class could never be satisfied and every ruling would
        be refused for the wrong reason.
        """
        self.ordinary_corpus()
        case = self.build(DISCOVER, "441")

        reloaded = RetirementCase.from_dict(json.loads(json.dumps(case.to_dict())))

        self.assertEqual(reloaded.effective_from, "442")
        self.assertEqual(reloaded, case)
        self.assertEqual(case_sha256(reloaded), case_sha256(case))

    def test_the_command_line_has_no_flag_for_it(self):
        """Asserted against the parser, because "there is no flag" is a claim about
        the interface rather than about a value: a flag added in good faith by
        somebody who found the derivation inconvenient is exactly how this rule
        would be lost, and it would look like a usability fix.
        """
        self.ordinary_corpus()
        path = self.file_at("inv.json", investigation_row())

        with self.assertRaises(SystemExit) as caught:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                retirement.main([
                    "--root", str(self.tmp), "case", "--version", "441",
                    "--hook", DISCOVER, "--investigation", str(path),
                    "--effective-from", "441",
                ])

        self.assertNotEqual(caught.exception.code, 0)

    def test_a_retirement_row_carries_the_derived_version_and_not_the_cases(self):
        """The last link. The file `expectation` reads must hold 442, not 441."""
        self.ordinary_corpus()
        case, ruling = self.signed(self.build(DISCOVER, "441"))

        publish(case, ruling, root=self.tmp)

        self.assertEqual(self.rows()[0]["effective_from"], "442")
        self.assertEqual(read_retirements(self.tmp)[DISCOVER].effective_from, "442")


# ================================================================== the subject


class SubjectTests(RetirementTestCase):
    """The hash a human signs, and what a case file may not be.

    `case_sha256` is pure and covers the whole record, so the tests are about
    coverage rather than about the digest: a field the subject did not include
    would be a field an editor could change after the ruling was made.
    """

    def test_every_field_of_a_case_changes_its_subject_hash(self):
        """A field outside the subject is a field that can be edited after signing."""
        self.ordinary_corpus()
        case = self.build(DISCOVER, "441")
        original = case_sha256(case)
        seen = {original}

        for label, edited in self.variants(case).items():
            with self.subTest(field=label):
                digest = case_sha256(edited)

                self.assertNotEqual(digest, original, label)
                self.assertNotIn(digest, seen - {digest}, label)
                seen.add(digest)

    def variants(self, case: RetirementCase) -> dict[str, RetirementCase]:
        """Every field of a case, edited one at a time.

        `standing` and `investigation` are expanded field by field rather than
        replaced wholesale: a subject that hashed a standing by its hook id alone
        would pass a test that only swapped the whole object.
        """

        standing, found = case.standing, case.investigation
        return {
            "schema_version": dataclasses.replace(case, schema_version=2),
            "hook_id": dataclasses.replace(
                case,
                hook_id=SETTINGS,
                standing=dataclasses.replace(standing, hook_id=SETTINGS),
            ),
            # Moved together, because they no longer can move apart:
            # `__post_init__` derives `effective_from` from `version`, so there is
            # no valid case in which one differs from the other's successor. That
            # also retires the standalone `effective_from` variant this dict used
            # to carry — varying `version` is what covers both fields now.
            "version": dataclasses.replace(case, version="440", effective_from="441"),
            "standing.release_ready_on": dataclasses.replace(
                case, standing=dataclasses.replace(standing, release_ready_on=("440",))
            ),
            "standing.assessed_on": dataclasses.replace(
                case, standing=dataclasses.replace(standing, assessed_on=("441",))
            ),
            "investigation.investigated_by": dataclasses.replace(
                case, investigation=dataclasses.replace(found, investigated_by="someone")
            ),
            "investigation.summary": dataclasses.replace(
                case, investigation=dataclasses.replace(found, summary="something else")
            ),
            "investigation.findings": dataclasses.replace(
                case, investigation=dataclasses.replace(found, findings=("one",))
            ),
            "investigation.recommendation": dataclasses.replace(
                case, investigation=dataclasses.replace(found, recommendation="keep")
            ),
            "intent": dataclasses.replace(case, intent="something else"),
            "tier": dataclasses.replace(case, tier="ui"),
            "status": dataclasses.replace(case, status="retired"),
        }

    def test_an_unsupported_case_schema_is_refused_rather_than_guessed_at(self):
        """A case written by a newer producer must not be read with today's meanings."""
        self.ordinary_corpus()
        payload = self.build(DISCOVER, "441").to_dict()

        for value in (None, 0, 2, "1"):
            with self.subTest(schema_version=value):
                with self.assertRaises(RetirementError) as caught:
                    RetirementCase.from_dict({**payload, "schema_version": value})

                self.assertIn("unsupported case schema", str(caught.exception))

    def test_a_case_that_is_not_an_object_is_refused_by_type(self):
        for value in ([], "text", 3, None):
            with self.subTest(value=value):
                with self.assertRaises(RetirementError) as caught:
                    RetirementCase.from_dict(value)

                self.assertIn("must be a JSON object", str(caught.exception))

    def test_a_case_with_no_standing_is_refused(self):
        """The standing is the argument. A case without one is a request with a name on it."""
        self.ordinary_corpus()
        payload = self.build(DISCOVER, "441").to_dict()

        for value in (None, "none", [], 3):
            with self.subTest(standing=value):
                with self.assertRaises(RetirementError) as caught:
                    RetirementCase.from_dict({**payload, "standing": value})

                self.assertIn("no standing", str(caught.exception))

    def test_a_case_whose_standing_names_a_different_hook_is_refused(self):
        """The pasted-standing case: real evidence, attached to the wrong hook.

        Everything a reader would check by eye still looks right — the standing
        is genuine, the versions are real — and the case argues for retiring
        something the numbers are not about.
        """
        self.ordinary_corpus()
        payload = self.build(DISCOVER, "441").to_dict()
        payload["standing"] = {**payload["standing"], "hook_id": CONTEXT}

        with self.assertRaises(RetirementError) as caught:
            RetirementCase.from_dict(payload)

        self.assertIn("different hooks", str(caught.exception))

    def test_a_case_version_that_is_not_a_number_is_refused(self):
        """`int()` is called on it to derive the effective version."""
        self.ordinary_corpus()
        payload = self.build(DISCOVER, "441").to_dict()

        for value in ("441-rc1", "", "next", "44.1"):
            with self.subTest(version=value):
                with self.assertRaises(RetirementError) as caught:
                    RetirementCase.from_dict({**payload, "version": value})

                self.assertIn("not a version number", str(caught.exception))

    def test_a_case_with_a_malformed_investigation_is_refused_at_the_case_level(self):
        """The investigation's own refusals must not be lost on the way in."""
        self.ordinary_corpus()
        payload = self.build(DISCOVER, "441").to_dict()
        payload["investigation"] = {**payload["investigation"], "summary": "  "}

        with self.assertRaises(RetirementError) as caught:
            RetirementCase.from_dict(payload)

        self.assertIn("not evidence", str(caught.exception))


# ==================================================================== ruling


class RulingTests(RetirementTestCase):
    """A human's answer, and every way something that is not one could be recorded.

    `validate_ruling` is called twice on purpose — by `rule()` where a human can
    fix the problem, and by `publish()` which is the authority and runs even when
    the ruling arrives as a file from somewhere else. Where this project split a
    check into a filter and an authority before, the authority checked *less*, so
    each rule below is asserted through `publish` as well as through `rule`.
    """

    def setUp(self) -> None:
        super().setUp()
        self.ordinary_corpus()

    def published(self, **overrides: Any) -> Ruling:
        """A ruling assembled by hand, as one arriving as a file would be."""

        case = self.build(DISCOVER, "441")
        row = {
            "schema_version": 1,
            "hook_id": case.hook_id,
            "verdict": "retire",
            "rationale": "The surface is gone.",
            "ruled_by": "arnav",
            "case_sha256": case_sha256(case),
            "decision_id": "retire-by-hand",
            "ruled_at": RULED_AT,
        }
        row.update(overrides)
        return Ruling.from_dict(row)

    def test_a_ruling_names_the_case_it_answers(self):
        case, ruling = self.signed()

        self.assertEqual(ruling.case_sha256, case_sha256(case))
        self.assertEqual(ruling.hook_id, case.hook_id)
        self.assertEqual(ruling.verdict, "retire")
        self.assertEqual(ruling.ruled_by, "arnav")
        self.assertEqual(ruling.ruled_at, RULED_AT)

    def test_an_agent_may_not_rule_in_any_casing_or_padding(self):
        """The lock the whole design rests on.

        An agent assembles every fact in the case. If it could also sign the
        answer, the cheapest route past a red build would be for the thing being
        measured to rule that the measurement no longer applies — so a comparison
        that missed `AGENT` or ` agent ` would be no lock at all. Asserted at both
        the minting and the publishing end, because the file could be written
        anywhere.
        """
        case = self.build(DISCOVER, "441")

        for spelling in ("agent", "Agent", "AGENT", "  agent  ", "aGeNt"):
            with self.subTest(ruled_by=spelling):
                with self.assertRaises(RetirementError) as minting:
                    rule(
                        case,
                        verdict="retire",
                        rationale="because",
                        ruled_by=spelling,
                        ruled_at=RULED_AT,
                    )
                with self.assertRaises(RetirementError) as publishing:
                    publish(case, self.published(ruled_by=spelling), root=self.tmp)

                self.assertIn("ruled_by is 'agent'", str(minting.exception))
                self.assertIn("a human closes it", str(publishing.exception))
                self.assertEqual(self.rows(), [])

    def test_a_human_whose_name_contains_the_word_may_still_rule(self):
        """The control, and the reason the check is equality rather than a substring.

        `human-via-agent-draft` is a person who ruled on an agent's draft, which
        is the ordinary path this module was written for. Broadening the rule
        would refuse the legitimate case and the refusal would read as a parse
        error.
        """
        case, ruling = self.signed(ruled_by="human-via-agent-draft")

        self.assertEqual(ruling.ruled_by, "human-via-agent-draft")
        self.assertIsNotNone(publish(case, ruling, root=self.tmp))

    def test_a_ruling_must_name_who_made_it(self):
        case = self.build(DISCOVER, "441")

        for value in ("", "   "):
            with self.subTest(ruled_by=value):
                with self.assertRaises(RetirementError) as caught:
                    self.signed(case, ruled_by=value)
                with self.assertRaises(RetirementError):
                    publish(case, self.published(ruled_by=value), root=self.tmp)

                self.assertIn("must name who made it", str(caught.exception))

    def test_a_ruling_needs_a_rationale_because_the_row_is_permanent(self):
        """The next reader is somebody asking why the bar moved."""
        case = self.build(DISCOVER, "441")

        for value in ("", "   ", "\n"):
            with self.subTest(rationale=value):
                with self.assertRaises(RetirementError) as caught:
                    self.signed(case, rationale=value)
                with self.assertRaises(RetirementError):
                    publish(case, self.published(rationale=value), root=self.tmp)

                self.assertIn("needs a rationale", str(caught.exception))
                self.assertEqual(self.rows(), [])

    def test_a_ruling_needs_a_timestamp_and_this_layer_does_not_read_the_clock(self):
        """A record stamped by whoever happened to run it is one no reader can order.

        Supplying it also makes a replay rewrite the line already on disk rather
        than minting a new decision id for the same answer.
        """
        case = self.build(DISCOVER, "441")

        for value in ("", "   "):
            with self.subTest(ruled_at=value):
                with self.assertRaises(RetirementError) as caught:
                    self.signed(case, ruled_at=value)

                self.assertIn("needs a timestamp", str(caught.exception))

    def test_an_unknown_verdict_is_refused_and_the_message_lists_the_three(self):
        """`unclear` among them: it is a recommendation and never an answer."""
        case = self.build(DISCOVER, "441")

        for value in ("unclear", "RETIRE", "", "yes", "retired"):
            with self.subTest(verdict=value):
                with self.assertRaises(RetirementError) as caught:
                    self.signed(case, verdict=value)
                with self.assertRaises(RetirementError):
                    publish(case, self.published(verdict=value), root=self.tmp)

                self.assertIn("unknown verdict", str(caught.exception))
                self.assertIn("retire, keep, defer", str(caught.exception))

    def test_a_ruling_must_carry_a_decision_id(self):
        case = self.build(DISCOVER, "441")

        with self.assertRaises(RetirementError) as caught:
            publish(case, self.published(decision_id="   "), root=self.tmp)

        self.assertIn("must carry a decision id", str(caught.exception))
        self.assertEqual(self.rows(), [])

    def test_a_ruling_for_another_hook_is_refused_before_the_hash_is_compared(self):
        """The message has to name both, or the reader is told two hashes differ."""
        case = self.build(DISCOVER, "441")

        with self.assertRaises(RetirementError) as caught:
            publish(case, self.published(hook_id=CONTEXT), root=self.tmp)

        self.assertIn(CONTEXT, str(caught.exception))
        self.assertIn(DISCOVER, str(caught.exception))

    def test_a_recommendation_to_retire_and_a_ruling_of_keep_produce_a_keep(self):
        """The single most important behaviour in the module.

        `Investigation.recommendation` is advisory and nothing reads it. An agent
        that recommends retiring and a human who disagrees must produce the
        human's answer, with both positions preserved on the record and nothing
        written to the file the expectation reads.
        """
        case = self.build(DISCOVER, "441", recommendation="retire")

        case, ruling = self.signed(case, verdict="keep", rationale="Not yet — the "
                                   "walkthrough never reaches this surface.")
        written = publish(case, ruling, root=self.tmp)

        self.assertEqual(case.investigation.recommendation, "retire")
        self.assertEqual(ruling.verdict, "keep")
        self.assertIsNone(written)
        self.assertEqual(self.rows(), [])
        self.assertEqual(read_retirements(self.tmp), {})

    def test_a_recommendation_to_keep_does_not_stop_a_human_retiring_it(self):
        """The other direction, so the test above is not satisfied by ignoring the verdict."""
        case = self.build(DISCOVER, "441", recommendation="keep")

        case, ruling = self.signed(case, verdict="retire")
        publish(case, ruling, root=self.tmp)

        self.assertEqual(case.investigation.recommendation, "keep")
        self.assertEqual(read_retirements(self.tmp)[DISCOVER].effective_from, "442")

    def test_the_decision_id_is_derived_from_the_answer_and_not_supplied(self):
        """Two identical answers deduplicate; two different ones cannot collide.

        Every input to the answer is varied separately, because an id derived
        from the hook and the verdict alone would deduplicate two different
        rationales into one decision.
        """
        case = self.build(DISCOVER, "441")
        base = self.signed(case)[1]
        again = self.signed(case)[1]

        self.assertEqual(base.decision_id, again.decision_id)
        self.assertTrue(base.decision_id.startswith(f"retire-{DISCOVER}-"))

        differing = {
            "verdict": self.signed(case, verdict="keep")[1],
            "rationale": self.signed(case, rationale="a different reason")[1],
            "ruled_by": self.signed(case, ruled_by="someone-else")[1],
            "ruled_at": self.signed(case, ruled_at="2026-09-09T00:00:00Z")[1],
            "case": self.signed(self.build(DISCOVER, "440"))[1],
        }
        for label, other in differing.items():
            with self.subTest(varied=label):
                self.assertNotEqual(other.decision_id, base.decision_id, label)

    def test_a_ruling_round_trips_through_to_dict(self):
        """`rule --out` writes it and `publish --ruling` reads it back."""
        _, ruling = self.signed()

        self.assertEqual(ruling, Ruling.from_dict(json.loads(json.dumps(ruling.to_dict()))))
        self.assertEqual(ruling.to_dict()["schema_version"], 1)

    def test_an_unsupported_ruling_schema_is_refused(self):
        _, ruling = self.signed()
        payload = ruling.to_dict()

        for value in (None, 0, 2, "1"):
            with self.subTest(schema_version=value):
                with self.assertRaises(RetirementError) as caught:
                    Ruling.from_dict({**payload, "schema_version": value})

                self.assertIn("unsupported ruling schema", str(caught.exception))

    def test_a_ruling_that_is_not_an_object_is_refused_by_type(self):
        for value in ([], "text", 3, None):
            with self.subTest(value=value):
                with self.assertRaises(RetirementError) as caught:
                    Ruling.from_dict(value)

                self.assertIn("must be a JSON object", str(caught.exception))


# ================================================================== the binding


class BindingTests(RetirementTestCase):
    """A ruling answers exact bytes or it answers nothing.

    The hash is re-derived from the case in hand rather than compared with a copy
    travelling beside it, so a ruling cannot be replayed onto a case that has
    since changed. Every field is tried, because "the case changed" in practice
    means one line of a JSON file was different when the human read it.
    """

    def setUp(self) -> None:
        super().setUp()
        self.ordinary_corpus()

    def test_editing_any_field_of_the_case_after_signing_makes_publish_refuse(self):
        """Thirteen fields, thirteen refusals, and nothing written for any of them."""
        case, ruling = self.signed(self.build(DISCOVER, "441"))

        for label, edited in SubjectTests.variants(self, case).items():
            with self.subTest(field=label):
                with self.assertRaises(RetirementError):
                    publish(edited, ruling, root=self.tmp)

                self.assertEqual(self.rows(), [], label)

    def test_the_refusal_names_both_answers_and_says_what_to_do(self):
        """"The hashes differ" is unactionable; "rebuild the case and rule again" is not."""
        case, ruling = self.signed(self.build(DISCOVER, "441"))
        edited = dataclasses.replace(case, intent="something else")

        with self.assertRaises(RetirementError) as caught:
            publish(edited, ruling, root=self.tmp)

        self.assertIn(ruling.case_sha256[:12], str(caught.exception))
        self.assertIn(case_sha256(edited)[:12], str(caught.exception))
        self.assertIn("rebuild the case and rule again", str(caught.exception))

    def test_evidence_arriving_after_the_case_was_read_refuses_the_stale_answer(self):
        """The realistic version, and the reason the binding exists at all.

        The case is raised on Monday against 441's standing, the human answers on
        Tuesday, and **441 is re-measured** in between. The standing in the case
        they read no longer describes the hook, so the answer is refused rather
        than applied to a picture nobody saw.

        Re-measuring 441 and not porting 442, which is what this test used to do:
        a `Standing` is now bounded at its own case's version, so a later port
        deliberately does *not* move an earlier case. That property has its own
        test; this one is about evidence changing underneath a reader.
        """
        raised = self.build(DISCOVER, "441")
        _, ruling = self.signed(raised)

        self.port("441", {CONTEXT: triple(), DISCOVER: triple()}, previous="440")
        rebuilt = self.build(DISCOVER, "441")

        self.assertNotEqual(case_sha256(rebuilt), case_sha256(raised))
        with self.assertRaises(RetirementError):
            publish(rebuilt, ruling, root=self.tmp)
        self.assertEqual(self.rows(), [])

    def test_a_later_port_does_not_move_an_earlier_case(self):
        """A docket raised before the next port must survive it.

        `Standing` used to describe the whole series, so porting 442 changed what
        a 441 case said and a docket recorded on Monday could not be re-derived on
        Thursday. It failed closed rather than admitting anything wrong, and a
        gate answerable only until the next port is still a broken gate.
        """
        before = self.build(DISCOVER, "441")
        self.port("442", {CONTEXT: triple(), DISCOVER: triple(runtime_probe="failed")},
                  previous="441")

        after = self.build(DISCOVER, "441")
        self.assertEqual(case_sha256(before), case_sha256(after))
        self.assertEqual(("440", "441"), after.standing.assessed_on)

    def test_a_ruling_for_the_same_hook_at_another_version_is_not_interchangeable(self):
        """Two honest cases, one honest ruling, and it belongs to only one of them."""
        at_440 = self.build(DISCOVER, "440")
        at_441 = self.build(DISCOVER, "441")
        _, ruling = self.signed(at_440)

        with self.assertRaises(RetirementError):
            publish(at_441, ruling, root=self.tmp)

        self.assertIsNotNone(publish(at_440, ruling, root=self.tmp))
        self.assertEqual(self.rows()[0]["effective_from"], "441")

    def test_the_same_check_runs_at_both_ends(self):
        """`rule()` refuses it too, so a human is told at the point they can fix it."""
        case = self.build(DISCOVER, "441")
        other = dataclasses.replace(case, tier="ui")
        _, ruling = self.signed(case)

        with self.assertRaises(RetirementError):
            validate_ruling(other, ruling)
        validate_ruling(case, ruling)


# ================================================================== publishing


class PublishTests(RetirementTestCase):
    """The append, and the four things it must not do.

    It must not write for an answer that is not "retire"; it must not write a
    second row for a hook that already has one; it must not write anywhere the
    reader is not looking; and it must not write anything at all when the ruling
    is refused. The last is asserted in every refusal test in this file rather
    than once here — a check that refuses after writing is a check that did not
    refuse.
    """

    def setUp(self) -> None:
        super().setUp()
        self.ordinary_corpus()

    def test_a_retirement_is_appended_where_the_expectation_will_look_for_it(self):
        case, ruling = self.signed(self.build(DISCOVER, "441"))

        written = publish(case, ruling, root=self.tmp)

        self.assertEqual(written, self.tmp / RETIREMENTS)
        self.assertEqual(
            self.rows()[0],
            {
                "schema_version": 1,
                "hook_id": DISCOVER,
                "effective_from": "442",
                "decision_id": ruling.decision_id,
                "ruled_by": "arnav",
                "rationale": ruling.rationale,
                "recorded_at": RULED_AT,
            },
        )

    def test_a_keep_and_a_defer_write_nothing_and_say_so_by_returning_none(self):
        """They are answers, not non-events — and the file's only meaning is
        "no longer expected", so a row saying "still expected" does not belong in it.
        """
        for verdict in ("keep", "defer"):
            with self.subTest(verdict=verdict):
                case, ruling = self.signed(self.build(CONTEXT, "441"), verdict=verdict)

                self.assertIsNone(publish(case, ruling, root=self.tmp))
                self.assertFalse((self.tmp / RETIREMENTS).exists())
                self.assertEqual(read_retirements(self.tmp), {})

    def test_a_second_retirement_for_the_same_hook_is_refused(self):
        """Appending it could not change anything, because the earliest wins.

        A second row is therefore either a no-op that looks like a decision, or —
        if it names an earlier version — a way to reach further back than the
        first ruling allowed. Both are worse than a refusal.
        """
        first, first_ruling = self.signed(self.build(DISCOVER, "441"))
        publish(first, first_ruling, root=self.tmp)
        second = dataclasses.replace(first, version="440", effective_from="441")
        _, second_ruling = self.signed(second, rationale="trying again, earlier")

        with self.assertRaises(RetirementError) as caught:
            publish(second, second_ruling, root=self.tmp)

        self.assertIn("already has a retirement at 442", str(caught.exception))
        self.assertIn("earliest effective_from wins", str(caught.exception))
        self.assertEqual(len(self.rows()), 1)

    def test_a_retirement_for_a_different_hook_is_appended_beside_it(self):
        """The control for the refusal above: the file is append-only, not write-once."""
        first, first_ruling = self.signed(self.build(DISCOVER, "441"))
        second, second_ruling = self.signed(self.build(CONTEXT, "441"))

        publish(first, first_ruling, root=self.tmp)
        publish(second, second_ruling, root=self.tmp)

        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(sorted(read_retirements(self.tmp)), sorted([CONTEXT, DISCOVER]))

    def test_the_row_is_read_back_by_the_module_that_will_consume_it(self):
        """Written by one module and parsed by another, so the two are checked together."""
        case, ruling = self.signed(self.build(DISCOVER, "441"))

        publish(case, ruling, root=self.tmp)
        recorded = read_retirements(self.tmp)[DISCOVER]

        self.assertEqual(recorded, Retirement.from_dict(self.rows()[0]))
        self.assertEqual(recorded.ruled_by, "arnav")
        self.assertEqual(recorded.rationale, ruling.rationale)
        self.assertEqual(recorded.recorded_at, RULED_AT)

    def test_a_row_the_reader_would_refuse_is_refused_before_it_is_written(self):
        """`publish` round-trips the row through `Retirement.from_dict` on purpose.

        A field renamed on either side would otherwise produce a file that
        publishes cleanly and is refused on read — the both-ends disconnection
        this project keeps shipping. Reached here with an `effective_from` the
        reader will not accept, which `publish` itself has no opinion about.
        """
        # Reached through an empty `hook_id`, which `RetirementCase` permits (it
        # only requires the case and its standing to agree) and the reader
        # refuses. The route this test used to take — a non-numeric `version` —
        # is now closed by `__post_init__`, which is the better place for it; the
        # round trip still has to bite for the fields nothing upstream judges.
        case = dataclasses.replace(
            self.build(DISCOVER, "441"),
            hook_id="",
            standing=dataclasses.replace(self.build(DISCOVER, "441").standing, hook_id=""),
        )
        _, ruling = self.signed(case)

        with self.assertRaises(ExpectationError) as caught:
            publish(case, ruling, root=self.tmp)

        self.assertIn("hook_id", str(caught.exception))
        self.assertEqual(self.rows(), [])

    def test_publish_writes_where_it_is_told_and_nowhere_else(self):
        """The seam. A durable store whose writer has no seam is one every test
        writes to, and this project shipped 36 rows of fixture data into the
        committed evidence corpus that way.
        """
        elsewhere = self.tmp / "proposed" / "retirements.jsonl"
        case, ruling = self.signed(self.build(DISCOVER, "441"))

        written = publish(case, ruling, root=self.tmp, path=elsewhere)

        self.assertEqual(written, elsewhere)
        self.assertTrue(elsewhere.is_file())
        self.assertFalse((self.tmp / RETIREMENTS).exists())
        self.assertEqual(read_retirements(self.tmp), {})
        self.assertIn(DISCOVER, read_retirements(self.tmp, path=elsewhere))

    def test_the_already_retired_check_reads_the_file_being_written_to(self):
        """`path=` wins over `root=` in the reader, so the check must use it.

        Checking the conventional location while appending somewhere else would
        let a proposed file accumulate two rows for one hook, and the second one
        is unreachable by the reader for ever.
        """
        elsewhere = self.tmp / "proposed.jsonl"
        case, ruling = self.signed(self.build(DISCOVER, "441"))
        publish(case, ruling, root=self.tmp, path=elsewhere)

        with self.assertRaises(RetirementError):
            publish(case, ruling, root=self.tmp, path=elsewhere)

        self.assertEqual(len(self.rows(elsewhere)), 1)

    def test_the_directory_is_created_when_the_file_has_never_existed(self):
        """Today's state is that the file does not exist. The first publish makes it."""
        target = self.tmp / "fresh" / "manifest" / "retirements.jsonl"
        case, ruling = self.signed(self.build(DISCOVER, "441"))

        publish(case, ruling, root=self.tmp, path=target)

        self.assertTrue(target.is_file())

    def test_an_existing_file_is_appended_to_and_not_replaced(self):
        """Append-only is the property; a rewrite would lose every earlier decision."""
        path = self.tmp / RETIREMENTS
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "hook_id": ACTIONBAR,
                    "effective_from": "440",
                    "decision_id": "retire-earlier",
                    "ruled_by": "arnav",
                    "rationale": "recorded before this one",
                    "recorded_at": "2026-08-01T00:00:00Z",
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        case, ruling = self.signed(self.build(DISCOVER, "441"))

        publish(case, ruling, root=self.tmp)

        self.assertEqual([row["hook_id"] for row in self.rows()], [ACTIONBAR, DISCOVER])


# ================================================================== round trip


class RoundTripTests(RetirementTestCase):
    """The producer and the consumer, in one corpus.

    This project has repeatedly shipped gates that were complete and disconnected
    at one end. So these tests do not stop at a well-formed row: they publish
    one, then ask `expectation` — the module that fails the port — what it makes
    of it, on the version the case was built from and on the version after.

    Both directions are asserted against a before-and-after control in the same
    corpus, because a retirement that excused everything and one that excused
    nothing are indistinguishable on a corpus with a single version in it.
    """

    def four_ports(self) -> None:
        """440 and 441 hold two hooks; 442 has lost one of them entirely.

        `tigon_url_block` is release-ready on 440 and 441 and has no claim at all
        on 442, which is the case a retirement is written for: the hook was
        removed from the manifest and its evidence with it.
        """

        self.baseline_port()
        self.port("440", {CONTEXT: triple(), TIGON: triple()}, previous="439")
        self.port("441", {CONTEXT: triple(), TIGON: triple()}, previous="440")
        self.port("442", {CONTEXT: triple()}, previous="441")

    def test_a_published_retirement_is_not_in_force_at_the_version_it_came_from(self):
        """441's failure stays on the record. This is the whole reason for the delay.

        If a red build could be turned green by approving a retirement, approving
        one is the cheapest thing a tired person can do at the end of a long port,
        and the gate would reliably be answered "yes" exactly when the evidence
        for "yes" is weakest.
        """
        self.four_ports()
        case, ruling = self.signed(self.build(TIGON, "441"))

        publish(case, ruling, root=self.tmp)
        recorded = read_retirements(self.tmp)

        self.assertEqual(retired_by("441", recorded), {})
        self.assertEqual(sorted(retired_by("442", recorded)), [TIGON])
        self.assertEqual(recorded[TIGON].effective_from, "442")

    def test_the_retirement_excuses_the_next_port_and_the_control_proves_it_was_needed(self):
        """The connection, in the module that can fail a port.

        Before the row, 442 has dropped a hook that was release-ready on 441 and
        the comparison fails. After it, the same corpus passes and the hook is
        reported as retired with the human's name and reason attached. Nothing in
        the evidence changed between the two halves.
        """
        self.four_ports()
        before = compare(self.tmp, version="442")

        case, ruling = self.signed(self.build(TIGON, "441"))
        publish(case, ruling, root=self.tmp)
        after = compare(self.tmp, version="442")
        text = render(after)

        self.assertEqual(before.dropped, (TIGON,))
        self.assertFalse(before.met)
        self.assertEqual(after.dropped, ())
        self.assertTrue(after.met)
        self.assertEqual(
            {verdict.hook_id: verdict.state for verdict in after.verdicts},
            {CONTEXT: "held", TIGON: "retired"},
        )
        self.assertIn("Retired, so not expected (1):", text)
        self.assertIn(f"arnav ruled at 442 ({ruling.decision_id})", text)
        self.assertIn(ruling.rationale, text)

    def test_the_retirement_does_not_repair_the_port_that_exposed_the_drop(self):
        """The backdating attempt, run end to end and failing to land.

        The hook drops on 441, a case is built from 441's evidence and answered
        `retire`, and 441 still fails afterwards — because the row takes effect
        at 442. This is `effective_from` doing the only job it has, measured by
        the module that reads it rather than by the module that wrote it.
        """
        self.two_ports(
            {CONTEXT: triple(), SETTINGS: triple()},
            {CONTEXT: triple(), SETTINGS: triple(differential="failed")},
        )
        case, ruling = self.signed(self.build(SETTINGS, "441"))

        publish(case, ruling, root=self.tmp)
        after = compare(self.tmp, version="441")

        self.assertEqual(after.dropped, (SETTINGS,))
        self.assertFalse(after.met)
        self.assertEqual(read_retirements(self.tmp)[SETTINGS].effective_from, "442")

    def test_a_hook_retired_while_still_passing_is_reported_rather_than_hidden(self):
        """The case for retiring was probably made when it was not passing.

        Nothing here removes a hook from the manifest or stops its probe
        reporting, so a retired hook that works again must reach a human — and it
        does, in the port report, as STILL PASSING.
        """
        self.two_ports({CONTEXT: triple(), TIGON: triple()},
                       {CONTEXT: triple(), TIGON: triple()})
        self.port("442", {CONTEXT: triple(), TIGON: triple()}, previous="441")
        case, ruling = self.signed(self.build(CONTEXT, "441"))

        publish(case, ruling, root=self.tmp)
        after = compare(self.tmp, version="442")

        self.assertTrue(after.met)
        self.assertEqual(
            {verdict.hook_id: verdict.state for verdict in after.verdicts}[CONTEXT],
            "retired_still_passing",
        )
        self.assertIn("STILL PASSING", render(after))

    def test_a_kept_hook_is_still_owed_by_the_next_port(self):
        """The negative control for the whole class.

        The same corpus and the same case, answered `keep`: 442 still fails,
        because nothing was written. Without this, every test above is satisfied
        by an `expectation` that had stopped expecting anything.
        """
        self.four_ports()
        case, ruling = self.signed(self.build(TIGON, "441"), verdict="keep")

        publish(case, ruling, root=self.tmp)
        after = compare(self.tmp, version="442")

        self.assertEqual(after.dropped, (TIGON,))
        self.assertFalse(after.met)


# ========================================================================= CLI


class CliTests(RetirementTestCase):
    """The process boundary: 0 for an answer, 2 for a refusal, and JSON.

    Every refusal in this module is reachable from the command line, and a
    refusal that leaves as a traceback is one a script reads as a crash rather
    than as an answer — so each subcommand is asserted for its code, its stream
    and the absence of a written row.
    """

    def setUp(self) -> None:
        super().setUp()
        self.ordinary_corpus()
        self.investigation_path = self.file_at("inv.json", investigation_row())
        self.case_path = self.tmp / "case.json"
        self.ruling_path = self.tmp / "ruling.json"

    def make_case(self, hook: str = DISCOVER, version: str = "441") -> tuple[int, str]:
        code, stdout, _ = self.run_main(
            "case", "--version", version, "--hook", hook,
            "--investigation", str(self.investigation_path), "--out", str(self.case_path),
        )
        return code, stdout

    def make_ruling(self, *args: str) -> tuple[int, str, str]:
        return self.run_main(
            "rule", "--case", str(self.case_path), "--verdict", "retire",
            "--rationale", "The surface is gone from the app.",
            "--ruled-by", "arnav", "--ruled-at", RULED_AT,
            "--out", str(self.ruling_path), *args,
        )

    def test_candidates_prints_the_list_and_exits_zero(self):
        code, stdout, stderr = self.run_main("candidates", "--version", "441")

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("RETIREMENT CANDIDATES at 441", stdout)
        self.assertIn(DISCOVER, stdout)

    def test_candidates_json_carries_the_standing_a_script_would_read(self):
        """The machine-readable view has to say which situation each hook is in.

        The human form separates a regression from a dormancy in words; a JSON
        view that printed only hook ids would drop the distinction that decides
        whether retiring is sensible.
        """
        code, stdout, _ = self.run_main("candidates", "--version", "441", "--json")
        payload = json.loads(stdout)

        self.assertEqual(code, 0)
        self.assertEqual([item["hook_id"] for item in payload], [DISCOVER])
        self.assertTrue(payload[0]["never_release_ready"])
        self.assertEqual(payload[0]["assessed_on"], ["440", "441"])
        self.assertIsNone(payload[0]["dropped_at"])

    def test_a_bad_version_is_refused_on_stderr_rather_than_left_as_a_traceback(self):
        code, stdout, stderr = self.run_main("candidates", "--version", "441-rc1")

        self.assertEqual(code, 2)
        self.assertTrue(stderr.startswith("refused: "), stderr)
        self.assertEqual(stdout, "")

    def test_the_case_subcommand_prints_the_subject_and_writes_a_loadable_file(self):
        code, stdout = self.make_case()
        written = RetirementCase.from_dict(json.loads(self.case_path.read_text()))

        self.assertEqual(code, 0)
        self.assertIn(f"RETIREMENT CASE  {DISCOVER}", stdout)
        self.assertIn("would take effect 442", stdout)
        self.assertIn(case_sha256(written), stdout)
        self.assertEqual(written.effective_from, "442")

    def test_an_unknown_hook_is_refused_with_a_code_a_script_can_read(self):
        code, _, stderr = self.run_main(
            "case", "--version", "441", "--hook", "no_such_hook",
            "--investigation", str(self.investigation_path),
        )

        self.assertEqual(code, 2)
        self.assertIn("refused: ", stderr)
        self.assertIn("not in the hook manifest", stderr)

    def test_a_missing_investigation_file_is_refused_and_names_the_path(self):
        """The investigation is required: a case with none is a red number and a
        request to act on it.
        """
        code, _, stderr = self.run_main(
            "case", "--version", "441", "--hook", DISCOVER,
            "--investigation", str(self.tmp / "nope.json"),
        )

        self.assertEqual(code, 2)
        self.assertIn("no investigation at", stderr)
        self.assertIn("nope.json", stderr)

    def test_an_unparseable_investigation_is_refused_at_its_own_path(self):
        path = self.tmp / "broken.json"
        path.write_text("{not json", encoding="utf-8")

        code, _, stderr = self.run_main(
            "case", "--version", "441", "--hook", DISCOVER, "--investigation", str(path),
        )

        self.assertEqual(code, 2)
        self.assertIn(str(path), stderr)

    def test_an_investigation_naming_an_unknown_key_is_refused(self):
        """A drafting tool that wrote `verdict` into the file must not be ignored."""
        path = self.file_at("extra.json", investigation_row(verdict="retire"))

        code, _, stderr = self.run_main(
            "case", "--version", "441", "--hook", DISCOVER, "--investigation", str(path),
        )

        self.assertEqual(code, 2)
        self.assertIn("unknown keys: verdict", stderr)

    def test_the_rule_subcommand_prints_the_ruling_and_writes_it(self):
        self.make_case()

        code, stdout, stderr = self.make_ruling()
        payload = json.loads(self.ruling_path.read_text())

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout), payload)
        self.assertEqual(payload["verdict"], "retire")
        self.assertEqual(payload["ruled_by"], "arnav")
        self.assertTrue(payload["decision_id"].startswith(f"retire-{DISCOVER}-"))

    def test_an_agent_cannot_rule_from_the_command_line_either(self):
        self.make_case()

        code, stdout, stderr = self.run_main(
            "rule", "--case", str(self.case_path), "--verdict", "retire",
            "--rationale", "because", "--ruled-by", "agent", "--ruled-at", RULED_AT,
        )

        self.assertEqual(code, 2)
        self.assertIn("ruled_by is 'agent'", stderr)
        self.assertEqual(stdout, "")

    def test_a_verdict_outside_the_vocabulary_is_rejected_by_the_parser(self):
        """`--verdict unclear` never reaches the module: `choices` refuses it first."""
        self.make_case()

        with self.assertRaises(SystemExit) as caught:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                retirement.main([
                    "--root", str(self.tmp), "rule", "--case", str(self.case_path),
                    "--verdict", "unclear", "--rationale", "x", "--ruled-by", "arnav",
                    "--ruled-at", RULED_AT,
                ])

        self.assertNotEqual(caught.exception.code, 0)

    def test_a_case_file_edited_to_backdate_itself_is_refused_by_the_rule_command(self):
        """The attack this whole module is shaped around, at the point it would happen.

        The case is built honestly, written to a file, and the one field that
        would excuse the failing port is edited before it is signed.
        """
        self.make_case()
        payload = json.loads(self.case_path.read_text())
        payload["effective_from"] = "441"
        self.case_path.write_text(json.dumps(payload), encoding="utf-8")

        code, stdout, stderr = self.make_ruling()

        self.assertEqual(code, 2)
        self.assertIn("derived, not chosen", stderr)
        self.assertEqual(stdout, "")

    def test_the_publish_subcommand_appends_the_row_and_says_to_commit_it(self):
        """An uncommitted row works here and vanishes on clone, so the tool says so."""
        self.make_case()
        self.make_ruling()

        code, stdout, stderr = self.run_main(
            "publish", "--case", str(self.case_path), "--ruling", str(self.ruling_path),
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn(f"{DISCOVER}: retired from 442", stdout)
        self.assertIn("Commit it", stdout)
        self.assertEqual(read_retirements(self.tmp)[DISCOVER].effective_from, "442")

    def test_publishing_a_keep_writes_nothing_and_still_exits_zero(self):
        """A refusal and an answer of "no" must not share an exit code."""
        self.make_case()
        self.run_main(
            "rule", "--case", str(self.case_path), "--verdict", "keep",
            "--rationale", "not yet", "--ruled-by", "arnav", "--ruled-at", RULED_AT,
            "--out", str(self.ruling_path),
        )

        code, stdout, _ = self.run_main(
            "publish", "--case", str(self.case_path), "--ruling", str(self.ruling_path),
        )

        self.assertEqual(code, 0)
        self.assertIn("Nothing written — the hook stays expected", stdout)
        self.assertFalse((self.tmp / RETIREMENTS).exists())

    def test_publishing_a_stale_ruling_is_refused_and_writes_nothing(self):
        """The case is edited after the ruling was written, which is the file's whole risk."""
        self.make_case()
        self.make_ruling()
        payload = json.loads(self.case_path.read_text())
        payload["intent"] = "something the human never read"
        self.case_path.write_text(json.dumps(payload), encoding="utf-8")

        code, stdout, stderr = self.run_main(
            "publish", "--case", str(self.case_path), "--ruling", str(self.ruling_path),
        )

        self.assertEqual(code, 2)
        self.assertIn("rebuild the case and rule again", stderr)
        self.assertEqual(stdout, "")
        self.assertFalse((self.tmp / RETIREMENTS).exists())

    def test_publish_honours_the_destination_it_is_given(self):
        elsewhere = self.tmp / "proposed.jsonl"
        self.make_case()
        self.make_ruling()

        code, _, _ = self.run_main(
            "publish", "--case", str(self.case_path), "--ruling", str(self.ruling_path),
            "--retirements", str(elsewhere),
        )

        self.assertEqual(code, 0)
        self.assertTrue(elsewhere.is_file())
        self.assertFalse((self.tmp / RETIREMENTS).exists())

    def test_a_missing_ruling_file_is_refused_rather_than_treated_as_no_answer(self):
        self.make_case()

        code, _, stderr = self.run_main(
            "publish", "--case", str(self.case_path),
            "--ruling", str(self.tmp / "absent.json"),
        )

        self.assertEqual(code, 2)
        self.assertIn("no ruling at", stderr)

    def test_the_whole_journey_ends_in_a_port_the_expectation_passes(self):
        """Four commands, one corpus, and the answer read back by the consumer.

        Each subcommand is tested on its own above; this is the one that would
        fail if they were each correct and did not compose — the shape this
        project has shipped more than once.
        """
        self.port("442", {CONTEXT: triple(), TIGON: triple()}, previous="441")
        before = compare(self.tmp, version="442")

        listing, listed, _ = self.run_main("candidates", "--version", "441")
        built, _ = self.make_case(DISCOVER, "441")
        ruled, _, _ = self.make_ruling()
        published, _, _ = self.run_main(
            "publish", "--case", str(self.case_path), "--ruling", str(self.ruling_path),
        )
        after = compare(self.tmp, version="442")

        self.assertEqual([listing, built, ruled, published], [0, 0, 0, 0])
        self.assertIn(DISCOVER, listed)
        # DISCOVER was never release-ready, so neither port owed it: the visible
        # change is in the record, and the port that was already clean stays clean.
        self.assertTrue(before.met)
        self.assertTrue(after.met)
        self.assertEqual(read_retirements(self.tmp)[DISCOVER].effective_from, "442")
        self.assertEqual(
            [standing.hook_id for standing in candidates(self.tmp, version="441")], []
        )

    def test_a_subcommand_is_required(self):
        """`python -m dfinsta_pipeline.retirement` with no verb must not do anything."""
        with self.assertRaises(SystemExit) as caught:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                retirement.main(["--root", str(self.tmp)])

        self.assertNotEqual(caught.exception.code, 0)

    def test_a_mistyped_root_is_refused_rather_than_reported_as_no_candidates(self):
        """An empty answer from the wrong directory is the most reassuring wrong answer."""
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = retirement.main(
                ["--root", str(self.tmp / "typo"), "case", "--version", "441",
                 "--hook", DISCOVER, "--investigation", str(self.investigation_path)]
            )

        self.assertEqual(code, 2)
        self.assertIn("refused: ", stderr.getvalue())

    def test_a_non_numeric_baseline_is_refused_through_the_same_channel(self):
        code, _, stderr = self.run_main(
            "--baseline", "nope", "candidates", "--version", "441"
        )

        self.assertEqual(code, 2)
        self.assertIn("refused: ", stderr)


# ============================================================== known defects


class AuthorityCompletenessTests(RetirementTestCase):
    """Four defects found by writing this file, all closed the same day.

    They were pinned as `expectedFailure` first — the convention this suite uses
    for a defect its own tests found — which is why each reads as an assertion
    about what the module's docstrings *promise* rather than about what its code
    does. Kept as ordinary tests now: a fix without a test is a fix until the
    next refactor.

    Three of the four were one shape seen three times. `rule()` mints a ruling
    and `validate_ruling` is the authority that admits one, and the authority
    checked **less** than the minter: it required neither a timestamp nor a
    decision id it could re-derive, so a hand-written `ruling.json` carrying the
    printed subject hash published a row stamped with nothing. That is exactly
    `the-authority-checked-less-than-the-filter`, in a module whose own docstring
    cites it. Ask, of every clause: if the other layer were bypassed entirely,
    what still holds?

    The fourth was worse and is the reason `__post_init__` now exists on
    `RetirementCase`. `effective_from` is the invariant the module is built
    around, and it was re-derived only in `from_dict` — so the CLI was safe and
    every in-process caller could construct a backdated case in one line and hand
    it to the only function that writes the file. An invariant enforced by the
    last function in a chain holds only while that function stays last.
    """

    def setUp(self) -> None:
        super().setUp()
        self.ordinary_corpus()

    def test_a_case_built_by_hand_cannot_backdate_its_own_effective_version(self):
        """A backdated case cannot be constructed at all, let alone published.

        The re-derivation used to live only in `from_dict`, so the CLI was safe
        and the library was not: `RetirementCase(version="441",
        effective_from="441")` was signed by `rule()` without complaint and
        written by `publish()` — the only writer of the file — producing a row
        that stopped expecting the hook on the very port that exposed the drop.

        Fixed a layer lower than this test originally demanded. `__post_init__`
        re-derives, so there is no ordering of calls that reaches `publish` with a
        backdated case, and `dataclasses.replace` cannot make one either. That
        matters more than where the refusal happens: an invariant enforced by the
        last function in a chain holds only while that function stays last.
        """
        good = self.build(DISCOVER, "441")
        self.assertEqual("442", good.effective_from)

        for label, kwargs in {
            "backdated onto its own version": {"effective_from": "441"},
            "pushed forward past it": {"effective_from": "443"},
            "version moved, date left behind": {"version": "440"},
            "not a version at all": {"version": "v441"},
        }.items():
            with self.subTest(case=label):
                with self.assertRaises(RetirementError):
                    dataclasses.replace(good, **kwargs)

    def test_a_ruling_with_no_timestamp_is_refused_by_the_authority_too(self):
        """`rule()` requires `ruled_at`; `validate_ruling` does not.

        `rule()`'s refusal says a record stamped by whoever happened to run it is
        one no reader can order. `validate_ruling` is documented as the authority
        that "runs even when the ruling arrives as a file from somewhere else" —
        which is precisely the path with no check. A hand-written `ruling.json`
        carrying the subject hash `render_case` prints, with `"ruled_at": ""`,
        publishes a retirement whose `recorded_at` is empty.
        """
        case = self.build(DISCOVER, "441")
        ruling = Ruling.from_dict({
            "schema_version": 1,
            "hook_id": DISCOVER,
            "verdict": "retire",
            "rationale": "The surface is gone.",
            "ruled_by": "arnav",
            "case_sha256": case_sha256(case),
            "decision_id": "retire-by-hand",
            "ruled_at": "",
        })

        with self.assertRaises(RetirementError):
            publish(case, ruling, root=self.tmp)

    def test_a_decision_id_that_is_not_derived_from_the_answer_is_refused(self):
        """`rule()` derives it so "two different ones cannot collide".

        `validate_ruling` only asks that it be non-blank, so the property holds
        of rulings this module mints and of nothing else. Two different answers
        arriving as files can share an id, and `manifest/decisions.jsonl` and the
        retirement row would then disagree about which decision a hook was
        retired under.
        """
        case = self.build(DISCOVER, "441")
        honest = self.signed(case)[1]
        forged = Ruling.from_dict({**honest.to_dict(), "decision_id": "retire-anything"})

        with self.assertRaises(RetirementError):
            publish(case, forged, root=self.tmp)

    def test_a_findings_field_that_is_not_a_list_is_refused_and_not_a_traceback(self):
        """`{"findings": null}` is a `TypeError` out of a generator expression.

        It is what a drafting tool that found nothing would write, and
        `tuple(str(item) for item in data.get("findings", ()))` raises
        `TypeError`, which `main` does not catch — the `case` subcommand exits 1
        with a traceback rather than 2 with `refused:`. `RetirementCase.from_dict`
        has the same shape for `release_ready_on` and `assessed_on`, reachable
        from `rule` and `publish` with a hand-edited case file.

        A refusal channel is only a channel if everything uses it.
        """
        for value in (None, 3, True):
            with self.subTest(findings=value):
                with self.assertRaises(RetirementError):
                    Investigation.from_dict(
                        {"investigated_by": "x", "summary": "y", "findings": value}
                    )


if __name__ == "__main__":
    unittest.main()

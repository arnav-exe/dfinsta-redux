"""3C: the pipeline says where and when. The human says whether.

This is a test file for a decision *not* to build something, which needs
justifying, because a test that guards an absence is usually a test that cannot
fail. The three here can: each has a positive control that plants what it is
looking for and requires the scan to find it.

**The decision.** Nothing computes a consent answer, an addictiveness score or a
ranking. `block` and `offer_toggle` now require a human to say which side of the
consent test a candidate falls on (`test_consent_test.py`), and the launch-window
count is put in front of them (`test_startup_signal.py`) — but the answer itself
is typed by a person and copied, never derived.

**Why not, given the pipeline plainly has the data.** Because there is no method,
and this project has already paid once for inventing one: six of seven a-priori
addictiveness signals were noise and a composite put a 40-literal random control
*between* the labelled positives and negatives. That is now confirmed from
outside. A 2026 CHI systematic review of dark-pattern experiments covers 148
experimental units across 27 papers: "Attention Capture" appears three times in
the whole corpus, and time-on-platform was measured in 3 of 86 outcome units from
1 of 20 papers. The one properly preregistered test of a mechanism everybody
believes in — notification badges, Dekker 2024, n=205 — found checking p=.421,
screen time p=.094, and FoMO *increased*. Meta's own Project Daisy hid like
counts from 12% of Instagram MAU for six weeks and moved time spent by
−0.29% ± 0.235.

**And the measurement here is one-directional**, which is the specific reason an
inference would be wrong rather than merely unsupported. A launch-window request
is unsolicited by measurement. A request after a tap is *not* solicited by
measurement — generic recommendations and search sit at 100% and 33% in Lukoff's
bands with the same tap in front of both. Any function mapping the counts to an
answer would have to invent the half the walk cannot see.

What the pipeline may do, and does: count, attribute, and say which of those it
could not do.
"""

import ast
import inspect
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from dfinsta_pipeline import device_evidence
from dfinsta_pipeline.feature_gate import (
    CONSENT_ANSWERS,
    FeatureDispositionsV1,
    FeatureDispositionV1,
)
from dfinsta_pipeline.rulings import plan

REPOSITORY = Path(__file__).resolve().parents[1]
PACKAGE = REPOSITORY / "src" / "dfinsta_pipeline"

#: The module that *declares* the vocabulary, and the only one allowed to hold a
#: consent answer as a bare literal — the tuple itself and Lukoff's band table.
DECLARING_MODULE = "feature_gate.py"


def returns_a_consent_answer(source: str) -> list[str]:
    """Every function in `source` that returns a consent answer as a literal.

    Value position only. `device_evidence` carries dict *keys* named
    `"unsolicited"` and `"solicited"` whose values are counts, and a scan that
    could not tell a key from a value would flag them — then be relaxed until it
    flagged nothing, which is how a check becomes decorative.
    """

    tree = ast.parse(source)
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Return) or inner.value is None:
                continue
            for value in ast.walk(inner.value):
                # A dict's keys are reached by `ast.walk` too, so they are
                # excluded explicitly rather than by hoping none appear.
                if isinstance(value, ast.Dict):
                    continue
                if isinstance(value, ast.Constant) and value.value in CONSENT_ANSWERS:
                    parents = [
                        parent for parent in ast.walk(inner.value)
                        if isinstance(parent, ast.Dict) and value in parent.keys
                    ]
                    if not parents:
                        found.append(node.name)
    return sorted(set(found))


#: Modules that must be scanned by name. A glob that silently matches nothing —
#: or a skip condition widened until it skips everything — leaves an absence
#: assertion that cannot fail, which is worse than no assertion because it reads
#: as protection. These are the ones that hold the counts, the corpus, the
#: rulings and the client, so an inferrer would land in one of them.
MUST_BE_SCANNED = frozenset({
    "assessment.py",
    "device_evidence.py",
    "grouping.py",
    "observation.py",
    "rulings.py",
    "submission.py",
})


def scan_package() -> tuple[frozenset[str], dict[str, list[str]]]:
    """`(what was scanned, what was found)`. Both halves are asserted."""

    scanned: set[str] = set()
    offenders: dict[str, list[str]] = {}
    for path in sorted(PACKAGE.glob("*.py")):
        if path.name == DECLARING_MODULE:
            continue
        scanned.add(path.name)
        functions = returns_a_consent_answer(path.read_text(encoding="utf-8"))
        if functions:
            offenders[path.name] = functions
    return frozenset(scanned), offenders


class NothingDerivesAConsentAnswerTests(unittest.TestCase):
    def test_no_module_in_the_pipeline_returns_one(self) -> None:
        _, offenders = scan_package()
        self.assertEqual(
            {}, offenders,
            "a function returning a consent answer is an inference of the judgement "
            "this gate exists to ask a human for",
        )

    def test_the_scan_covered_the_modules_that_could_hold_one(self) -> None:
        """The other half of the assertion above, and the one that survived a
        mutation without it: widening the skip to `path.name != "nothing.py"`
        empties the loop, finds no offenders, and passes.
        """
        scanned, _ = scan_package()
        self.assertEqual(frozenset(), MUST_BE_SCANNED - scanned)
        self.assertGreater(
            len(scanned), 40,
            "the package has dozens of modules; a scan covering a handful of them "
            "is a glob that stopped matching",
        )
        self.assertNotIn(DECLARING_MODULE, scanned)

    def test_the_scan_finds_one_when_there_is_one(self) -> None:
        """The positive control. A search that cannot succeed always passes.

        Two shapes, because the plausible inferrer is a threshold and the
        plausible refactor is a lookup table returned wholesale.
        """
        threshold = (
            "def infer(unsolicited, solicited):\n"
            "    if unsolicited > solicited:\n"
            "        return 'unsolicited'\n"
            "    return 'solicited'\n"
        )
        self.assertEqual(["infer"], returns_a_consent_answer(threshold))

        nested = (
            "def classify(reading):\n"
            "    return [('x', 'mixed')]\n"
        )
        self.assertEqual(["classify"], returns_a_consent_answer(nested))

    def test_the_scan_does_not_flag_a_count_keyed_by_that_name(self) -> None:
        """The control in the other direction, which is the one that would have
        forced the check to be watered down. `device_evidence` really does return
        these keys, and must keep being allowed to."""
        keyed = (
            "def as_dict(self):\n"
            "    return {'unsolicited': self.unsolicited, 'solicited': self.solicited}\n"
        )
        self.assertEqual([], returns_a_consent_answer(keyed))

    def test_device_evidence_is_actually_covered_by_that_scan(self) -> None:
        """The module most likely to grow an inferrer is the one holding the
        counts, so it is named rather than left to a glob that might miss it."""
        self.assertIn(
            Path(inspect.getsourcefile(device_evidence)).name,
            {path.name for path in PACKAGE.glob("*.py")},
        )
        self.assertEqual(
            [], returns_a_consent_answer(Path(inspect.getsourcefile(device_evidence)).read_text())
        )


class TheAnswerIsCopiedNotDerivedTests(unittest.TestCase):
    """Whatever a human typed is what lands, including a combination no rule
    would produce."""

    def _planned(self, answers: tuple[str, ...]) -> dict[str, str | None]:
        document = FeatureDispositionsV1(
            1, "b" * 64, "2026-08-01",
            tuple(
                FeatureDispositionV1(1, f"gap:feed/x{index}/", "block", "because", answer)
                for index, answer in enumerate(answers)
            ),
        )
        with TemporaryDirectory() as directory:
            manifest = Path(directory) / "hooks.json"
            manifest.write_text(
                (REPOSITORY / "manifest" / "hooks.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            result = plan(
                document, run_id="feat-443", decision_id="d1",
                recorded_at="2026-08-18T00:00:00Z", manifest_path=manifest,
                source_path=REPOSITORY / "dfinsta_source_1.3",
            )
        return {item.candidate_id: item.consent for item in result.rulings}

    def test_every_permutation_survives_unchanged(self) -> None:
        for answers in (
            ("solicited", "unsolicited", "mixed"),
            ("mixed", "solicited", "unsolicited"),
            ("unsolicited", "unsolicited", "unsolicited"),
        ):
            with self.subTest(answers=answers):
                planned = self._planned(answers)
                self.assertEqual(
                    list(answers),
                    [planned[f"gap:feed/x{index}/"] for index in range(len(answers))],
                )


class EvidenceCarriesCountsAndNotAnswersTests(unittest.TestCase):
    """The gate is shown numbers and a caveat, never a proposed answer."""

    def test_no_evidence_detail_value_is_a_consent_answer(self) -> None:
        from dfinsta_pipeline.device_evidence import DeviceReading, _evidence

        readings = (
            DeviceReading("x", corpora=(("443", "one-pass-v1"),)),
            DeviceReading("x", watched_in=(("443", "one-pass-v1"),), sessions=4),
            DeviceReading(
                "x", watched_in=(("443", "one-pass-v1"),), sessions=4, seen=9,
                attributed_sessions=4, unsolicited=9, solicited=0,
            ),
            DeviceReading(
                "x", watched_in=(("443", "one-pass-v1"),), sessions=4, seen=9,
                attributed_sessions=4, unsolicited=0, solicited=9,
            ),
        )
        for reading in readings:
            with self.subTest(kind=reading.kind):
                evidence = _evidence(reading)
                for key, value in evidence.detail.items():
                    self.assertNotIn(
                        value, CONSENT_ANSWERS,
                        f"detail[{key!r}] carries a consent answer, which is the "
                        "human's to give",
                    )

    def test_an_all_unsolicited_reading_still_proposes_nothing(self) -> None:
        """The sharp case: 9 of 9 in the launch window is as strong as this
        measurement gets, and it still may not answer for anyone."""
        from dfinsta_pipeline.device_evidence import DeviceReading, _evidence

        evidence = _evidence(DeviceReading(
            "x", watched_in=(("443", "one-pass-v1"),), sessions=4, seen=9,
            attributed_sessions=4, unsolicited=9, solicited=0,
        ))
        self.assertNotIn("consent", evidence.detail)
        self.assertNotIn("verdict", evidence.detail)
        self.assertNotIn("recommend", evidence.summary.lower())
        self.assertNotIn("should be blocked", evidence.summary.lower())


if __name__ == "__main__":
    unittest.main()

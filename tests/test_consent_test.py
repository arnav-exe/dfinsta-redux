"""The consent test: the one criterion this project adopts, and where it binds.

A ruling used to carry free-text `rationale` and nothing else, so nothing could
check that the criterion had even been considered. The six rulings recorded on
2026-08-08 are all prose, and re-reading them cannot recover which side of the
test each was on — "show me everything ruled unsolicited" had no answer.

**The criterion.** *Did a user action cause this content to appear?* Adopted
because it is the only one with measured support: Lukoff et al., CHI 2021, 120
YouTube users, 467 coded responses, and the share reporting "less in control"
runs 100% for generic recommendations down to 0% for playlists and
subscriptions. It is deliberately not a score — six of seven a-priori
addictiveness signals this project tried were noise, and a composite put the
random control *between* the labelled groups.

**Where it binds.** `block` and `offer_toggle` are the two verdicts that change
the shipped app, and only they require an answer. `ignore` and `defer` change
nothing, so charging a judgement for the no-op would make the gate harder to
answer without making the app safer — the same scoping, and the same reason, as
the device-measurement clause it sits beside.

**What it deliberately does not do.** It cannot tell whether the answer is
*right*. `unsolicited` on a candidate the phone only ever requested after a tap
is a wrong answer nothing here can see. What it makes impossible is ruling
without answering; what it makes possible is auditing the answers afterwards
against the corpus.

`OrderingTests` is the one that would be easy to get wrong twice. Measurement is
checked before consent on purpose, and the client was changed to stop checking
consent at all so that the order lives in one place.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from dfinsta_pipeline import rulings as rulings_module
from dfinsta_pipeline.contracts import canonical_json
from dfinsta_pipeline.feature_gate import (
    ACTING_VERDICTS,
    CONSENT_ANSWERS,
    CONSENT_BANDS,
    DEVICE_UNWATCHED,
    SILENT_VERDICT,
    UNANSWERED_CONSENT,
    VERDICTS,
    FeatureDispositionsV1,
    FeatureDispositionV1,
    validate_submission,
)
from dfinsta_pipeline.rulings import Ruling, RulingError, read_store
from dfinsta_pipeline.submission import SubmissionRefused, _feature_rulings, consent_test

from tests.test_feature_gate import (
    CANDIDATES,
    GateTestCase,
    assessment_ref,
    dispositions_document,
    make_assessment,
    make_request,
    ruling,
)

REPOSITORY = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------- the vocabulary


class ConsentVocabularyTests(unittest.TestCase):
    def test_the_three_answers_are_the_ones_the_criterion_names(self) -> None:
        """Pinned, because the gate's refusal message enumerates them.

        A fourth answer is a change to what a human is being asked, not a
        widening of an enum, and it should not arrive as a passing diff.
        """
        self.assertEqual(CONSENT_ANSWERS, ("solicited", "unsolicited", "mixed"))

    def test_unanswered_is_distinct_from_every_answer(self) -> None:
        """"Not asked" must never be readable as an answer.

        The same absence rule the device evidence beside this uses, where a
        candidate nobody watched is a different fact from one watched and never
        seen. If `UNANSWERED_CONSENT` ever became a member of `CONSENT_ANSWERS`,
        every acting verdict would satisfy clause 11 while answering nothing.
        """
        self.assertNotIn(UNANSWERED_CONSENT, CONSENT_ANSWERS)

    def test_an_invented_answer_is_refused_by_the_contract(self) -> None:
        for bad in ("unsolicted", "SOLICITED", "unknown", "yes", " "):
            with self.subTest(consent=bad), self.assertRaises(ValueError):
                FeatureDispositionV1(1, CANDIDATES[0], "block", "why", bad)

    def test_a_non_string_answer_is_a_type_error(self) -> None:
        for bad in (None, 1, True, ["solicited"]):
            with self.subTest(consent=bad), self.assertRaises(TypeError):
                FeatureDispositionV1(1, CANDIDATES[0], "block", "why", bad)  # type: ignore[arg-type]

    def test_every_answer_is_accepted_on_every_verdict(self) -> None:
        """Answering is required of the acting verdicts and permitted of all.

        A human who works out that a deferred candidate is `unsolicited` should
        be able to record it; the next port then inherits the finding rather
        than the deliberation.
        """
        for verdict in VERDICTS:
            for answer in CONSENT_ANSWERS:
                with self.subTest(verdict=verdict, consent=answer):
                    item = FeatureDispositionV1(1, CANDIDATES[0], verdict, "why", answer)
                    self.assertEqual(item.consent, answer)


class ConsentBandsTests(unittest.TestCase):
    """The measured data behind the question, pinned to the paper."""

    def test_the_bands_are_lukoffs_published_figures(self) -> None:
        self.assertEqual(
            {feature: share for feature, share, _ in CONSENT_BANDS},
            {
                "generic recommendations": 100,
                "advertisements": 98,
                "autoplay": 87,
                "notifications": 53,
                "search results": 33,
                "playlists": 0,
                "subscriptions": 0,
            },
        )

    def test_the_zero_rows_are_solicited_and_are_why_the_criterion_is_consent(self) -> None:
        """Playlists and subscriptions are recommender surfaces at 0%.

        They are the rows that distinguish *consent* from *personalisation*. If
        the criterion were "is it algorithmic", both would score high; nobody
        reported losing control to either, because the user built them. Dropping
        these two rows from what a human is shown would leave a table that reads
        as "recommendation is bad", which is not what the data says.
        """
        by_feature = {feature: side for feature, _, side in CONSENT_BANDS}
        self.assertEqual(by_feature["playlists"], "solicited")
        self.assertEqual(by_feature["subscriptions"], "solicited")

    def test_every_band_is_labelled_with_a_real_answer(self) -> None:
        for feature, share, side in CONSENT_BANDS:
            with self.subTest(feature=feature):
                self.assertIn(side, CONSENT_ANSWERS)
                self.assertTrue(0 <= share <= 100)

    def test_the_printed_question_carries_the_question_and_every_band(self) -> None:
        """A human answering the gate calibrates against data or against a hunch.

        The text is the whole point of printing rather than summarising, so this
        asserts the sentence and all seven rows reach the page.
        """
        page = consent_test()
        self.assertIn("Did a user action cause this content to appear?", page)
        for feature, share, _ in CONSENT_BANDS:
            with self.subTest(feature=feature):
                self.assertIn(feature, page)
                self.assertIn(f"{share}%", page)
        for answer in CONSENT_ANSWERS:
            self.assertIn(answer, page)


# ------------------------------------------------------------- the authority


class ConsentBeforeActingTests(GateTestCase):
    """Clause 11: an acting verdict owes an answer."""

    def _document(self, verdict: str, consent: str) -> FeatureDispositionsV1:
        return dispositions_document(
            self.request,
            tuple(
                ruling(name, verdict, "because", consent) if name == CANDIDATES[0]
                else ruling(name, "ignore", "", UNANSWERED_CONSENT)
                for name in self.request.candidate_ids
            ),
        )

    def test_an_acting_verdict_without_an_answer_is_refused_by_name(self) -> None:
        for verdict in ACTING_VERDICTS:
            with self.subTest(verdict=verdict):
                document = self._document(verdict, UNANSWERED_CONSENT)
                with self.assertRaises(ValueError) as raised:
                    self.admit(document)
                self.assertIn(CANDIDATES[0], str(raised.exception))
                self.assertIn("consent test", str(raised.exception))

    def test_an_acting_verdict_with_an_answer_is_admitted(self) -> None:
        """The control. Without it the clause could be refusing for any reason."""
        for verdict in ACTING_VERDICTS:
            for answer in CONSENT_ANSWERS:
                with self.subTest(verdict=verdict, consent=answer):
                    self.admit(self._document(verdict, answer))

    def test_the_no_op_verdicts_owe_nothing(self) -> None:
        """`ignore` and `defer` change nothing in the app, so they are free.

        This is the clause's scope, and it is the reason the gate stays
        answerable: a human may dispose of every candidate they do not want to
        act on without first settling a judgement about each one.
        """
        for verdict in (SILENT_VERDICT, "defer"):
            with self.subTest(verdict=verdict):
                self.admit(self._document(verdict, UNANSWERED_CONSENT))

    def test_the_clause_is_scoped_to_exactly_the_acting_verdicts(self) -> None:
        """Derived from `ACTING_VERDICTS`, never re-listed.

        A verdict added to that tuple must inherit the requirement. This walks
        every verdict there is and asserts the requirement tracks membership,
        so widening the tuple cannot silently leave a new acting verdict
        answerable with no criterion.
        """
        for verdict in VERDICTS:
            with self.subTest(verdict=verdict):
                document = self._document(verdict, UNANSWERED_CONSENT)
                if verdict in ACTING_VERDICTS:
                    with self.assertRaises(ValueError):
                        self.admit(document)
                else:
                    self.admit(document)


class OrderingTests(GateTestCase):
    """Measurement is checked before consent, and in exactly one place."""

    def test_an_unmeasured_candidate_is_refused_for_measurement_not_consent(self) -> None:
        """Both clauses would fire; the human must be told the fatal one first.

        A candidate no device has looked for may not be acted on *at all*, so
        sending someone away to think about the consent test — and then refusing
        them anyway — wastes the deliberation the gate exists to buy.
        """
        # The request pins its assessment by hash, so all three have to be built
        # together — a fixture that changes one in isolation is refused by the
        # binding clause long before reaching either clause this is about.
        assessment = make_assessment(CANDIDATES, kind=DEVICE_UNWATCHED)
        request = make_request(
            assessment=assessment_ref(canonical_json(assessment).encode("utf-8"))
        )
        document = dispositions_document(
            request,
            tuple(
                ruling(name, "block", "because", UNANSWERED_CONSENT)
                for name in request.candidate_ids
            ),
        )
        with self.assertRaises(ValueError) as raised:
            self.admit(document, request=request, assessment=assessment)
        message = str(raised.exception)
        self.assertIn("no device", message)
        self.assertNotIn("consent test", message)

    def test_the_client_does_not_also_require_an_answer(self) -> None:
        """Two places deciding when a ruling is answerable would be one too many.

        `_feature_rulings` runs before the client's own `validate_submission`
        call, so a requirement there would win every time and the ordering above
        would be unreachable. It reads a missing answer through untouched.
        """
        detail = {
            name: {"verdict": "block", "rationale": "because"}
            for name in CANDIDATES
        }
        out = _feature_rulings(detail, CANDIDATES)
        self.assertEqual(out[CANDIDATES[0]], ("block", "because", UNANSWERED_CONSENT))

    def test_the_client_still_refuses_a_misspelt_answer_by_name(self) -> None:
        """The one check the client keeps: a typo, named, before anything hashes.

        The contract would refuse it too, through an exception the CLI turns
        into a traceback and with a message naming no candidate.
        """
        detail = {
            name: {"verdict": "block", "rationale": "because", "consent": "unsolicted"}
            for name in CANDIDATES
        }
        with self.assertRaises(SubmissionRefused) as raised:
            _feature_rulings(detail, CANDIDATES)
        self.assertIn(CANDIDATES[0], str(raised.exception))
        self.assertIn("unsolicted", str(raised.exception))


class DocumentRoundTripTests(unittest.TestCase):
    def test_the_answer_survives_serialisation(self) -> None:
        for answer in (*CONSENT_ANSWERS, UNANSWERED_CONSENT):
            with self.subTest(consent=answer):
                item = FeatureDispositionV1(1, CANDIDATES[0], "defer", "later", answer)
                self.assertEqual(FeatureDispositionV1.from_dict(item.to_dict()), item)

    def test_a_document_missing_the_key_is_refused(self) -> None:
        """Strict both ways, like every other field on this contract.

        A dispositions document is fetched from CAS and decoded before anything
        checks it, so a body that simply omits the answer must not decode into
        one that silently answers nothing.
        """
        data = FeatureDispositionV1(1, CANDIDATES[0], "block", "why", "unsolicited").to_dict()
        del data["consent"]
        with self.assertRaises(ValueError):
            FeatureDispositionV1.from_dict(data)

    def test_the_answer_changes_the_digest(self) -> None:
        """Otherwise the human's signature would not cover it.

        The gate binds a decision to a document hash; a field outside that hash
        is a field a human can be shown one value of and have another applied.
        """
        one = FeatureDispositionV1(1, CANDIDATES[0], "block", "why", "solicited")
        other = FeatureDispositionV1(1, CANDIDATES[0], "block", "why", "unsolicited")
        self.assertNotEqual(one.sha256, other.sha256)


# ------------------------------------------------------- the committed store


class RulingStoreTests(unittest.TestCase):
    """The store must be able to say "nobody was asked", because that is true."""

    def test_the_six_committed_rulings_read_back_as_unanswered(self) -> None:
        """They were recorded on 2026-08-08, before the question existed.

        Backfilling them would mean this code answering, into a committed store,
        a question a human was never asked — the precise failure of the 36
        fabricated rows this project has already shipped once. `None` is the
        honest value and the only one available.
        """
        rows = read_store(REPOSITORY / "manifest" / "rulings.jsonl")
        self.assertEqual(len(rows), 6)
        for row in rows:
            with self.subTest(candidate=row.candidate_id):
                self.assertIsNone(row.consent)

    def test_a_record_without_the_key_round_trips_to_the_committed_bytes(self) -> None:
        """`to_dict` omits it rather than writing null.

        A stored `"consent": null` would be a new fact about six old rulings, and
        would rewrite bytes that are already committed and already hashed into
        the record of what a human decided.
        """
        path = REPOSITORY / "manifest" / "rulings.jsonl"
        raw = [json.loads(line)["record"] for line in path.read_text().splitlines() if line.strip()]
        for row, original in zip(read_store(path), raw, strict=True):
            with self.subTest(candidate=row.candidate_id):
                self.assertEqual(row.to_dict(), original)
                self.assertNotIn("consent", row.to_dict())

    def test_an_answered_record_round_trips(self) -> None:
        for answer in CONSENT_ANSWERS:
            with self.subTest(consent=answer):
                row = self._ruling(consent=answer)
                self.assertIn("consent", row.to_dict())
                self.assertEqual(Ruling.from_dict(row.to_dict()), row)

    def test_an_invented_answer_is_refused_by_the_store(self) -> None:
        for bad in ("unsolicted", "", "SOLICITED"):
            with self.subTest(consent=bad), self.assertRaises(RulingError):
                self._ruling(consent=bad)

    def test_an_unknown_field_is_still_refused(self) -> None:
        """Widening by one name, not by "anything new is fine"."""
        data = self._ruling(consent="mixed").to_dict()
        data["note"] = "hand-added"
        with self.assertRaises(RulingError):
            Ruling.from_dict(data)

    def _ruling(self, *, consent: str | None) -> Ruling:
        return Ruling(
            candidate_id=CANDIDATES[0],
            verdict="block",
            rationale="because",
            run_id="feat-443",
            decision_id="decision-1",
            assessment_sha256="a" * 64,
            policy_revision="2026-08-01",
            recorded_at="2026-08-18T00:00:00Z",
            consent=consent,
        )


class PlanCarriesTheAnswerTests(unittest.TestCase):
    """The answer has to reach the store, or the gate recorded it into nothing."""

    def _plan(self, consent: str):
        document = FeatureDispositionsV1(
            1,
            "b" * 64,
            "2026-08-01",
            (
                FeatureDispositionV1(1, CANDIDATES[0], "block", "because", consent),
                FeatureDispositionV1(1, CANDIDATES[1], "ignore", "", UNANSWERED_CONSENT),
            ),
        )
        with TemporaryDirectory() as directory:
            manifest = Path(directory) / "hooks.json"
            manifest.write_text(
                (REPOSITORY / "manifest" / "hooks.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            return rulings_module.plan(
                document,
                run_id="feat-443",
                decision_id="decision-1",
                recorded_at="2026-08-18T00:00:00Z",
                manifest_path=manifest,
                source_path=REPOSITORY / "dfinsta_source_1.3",
            )

    def test_the_disposition_answer_lands_on_the_ruling(self) -> None:
        for answer in CONSENT_ANSWERS:
            with self.subTest(consent=answer):
                planned = {item.candidate_id: item for item in self._plan(answer).rulings}
                self.assertEqual(planned[CANDIDATES[0]].consent, answer)

    def test_the_documents_blank_becomes_the_stores_none(self) -> None:
        """`""` and `None` mean the same thing and are different types on purpose.

        The document requires an answer for the acting verdicts, so it can carry
        a meaningful blank; the store must be able to say a record predates the
        question entirely. Writing `""` into the store would make an unanswered
        new ruling indistinguishable from a pre-2026-08-18 one under a check
        that tests truthiness — and identical to neither under one that tests
        `is None`.
        """
        planned = {item.candidate_id: item for item in self._plan("mixed").rulings}
        self.assertIsNone(planned[CANDIDATES[1]].consent)
        self.assertNotIn("consent", planned[CANDIDATES[1]].to_dict())


if __name__ == "__main__":
    unittest.main()

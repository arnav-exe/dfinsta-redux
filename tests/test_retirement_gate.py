"""The hook-retirement gate's wire contracts, and the docket a run id reaches.

Two modules, one question each.

`retirement_gate.py` is pure. Its whole load-bearing function is
:func:`validate_submission`, which is **the authority**: the Workflow's update
validator runs in the sandbox with no store and no ledger, so it is a filter, and
this project has already shipped the split where the authority checked *less* and
"who may answer" came to rest entirely on the sandbox. So
:class:`ValidateSubmissionTests` breaks exactly one thing at a time, from a triple
that is otherwise genuine, and :class:`AuthorityCompletenessTests` walks the
filter's own clause list and requires the authority to refuse each one
independently — a test that fails if a clause is ever added to the sandbox alone.

`retirement_record.py` writes the run-keyed row that makes the gate answerable at
all. Its property is not "a docket exists somewhere": it is that a party who was
not there when the question was raised recovers the *same subject bytes* from a
run id and a read-only ledger. :class:`RoundTripTests` asserts that hash equality
both ways, with a positive control that the hash is capable of moving.

===============================================================================
  WHAT KEYS THE OPERATION
===============================================================================

Not the docket's digest — the inputs it derives from. :class:`OperationKeyTests`
attacks that from both sides: recording the same inputs twice is idempotent, a
changed investigations file mints a different key, and a *later* version's
evidence arriving does not move the key at all, because `_evidence_digests`
filters to `<= version`. The last one is the reason the evidence contributes as
`{path: sha256}` rather than as a directory digest, and it is asserted on the
helper directly as well as end to end.

===============================================================================
  FIXTURE PROVENANCE
===============================================================================

The evidence corpus is `tests/test_retirement.py`'s, by subclassing its
`RetirementTestCase` — the same `manifest/hooks.json`, the same flat evidence
rows, the same 439 with runtime evidence and no static file. A second corpus
builder would agree with the first until one of them was edited, and both modules
under test read that tree through `retirement.candidates`. The one addition is
`policy_revision` in the manifest, which `assessment.policy_revision` requires and
a retirement case does not.

The ledger and store are real: a SQLite `Ledger` and a `ContentStore` under a
`tempfile` state root, with a genuine begin/effect/complete cycle, following
`tests/test_assessment_record.py`. :class:`ResolveRefusalTests` plants recorded
state by hand because two of the states it needs — an authority row disagreeing
with its own operation, and non-canonical bytes in CAS — are states `record` will
not produce; each plants everything else genuinely, breaks exactly one thing, and
has a positive control planted the same way that resolves cleanly.

Nothing here writes to the repository's own `manifest/`. Every call passes
`root=` a temp tree: this project shipped 36 rows of fixture data into the
committed evidence corpus through a writer that had no seam.

===============================================================================
  KNOWN DEFECTS
===============================================================================

:class:`KnownDefectTests` are `expectedFailure`, the convention
`tests/test_retirement.py` and `tests/test_expectation.py` already use. Each
asserts what the modules' own docstrings promise and what the code does not do,
so the suite stays green today and reports an *unexpected success* the moment one
is closed.

1. `retirement_record.main`'s `show` subcommand catches `(RecordError, OSError)`
   and the ledger raises a plain `ValueError` for a run id that was never
   recorded — the single most likely thing to type wrong. It is a traceback and
   exit 1 where the contract is `refused:` and exit 2. `assessment_record.main`,
   the stated model, catches `ValueError` as well.
2. `record` promises "one error type out of this module, matching
   `assessment_record.record`" and lets `FileNotFoundError` out of both
   `manifest.read_bytes()` and `Path(investigations_path).read_bytes()`.
   `assessment_record._record` wraps exactly those reads in a `RecordError`
   naming the file, and its comment says why the *later* OSError is deliberately
   not wrapped — this module has the second half of that design and not the
   first.
3. `record` never checks that `run_id` and `allowed_actor` are identifiers, so a
   docket recorded under `"retire 441!"` is filed, resolvable, and permanently
   un-gateable: `derived_gate_id` refuses it at the far end, which is precisely
   the "answerable in a test and unanswerable in production" failure that
   function's docstring exists to prevent. `assessment_record._record` validates
   both before writing anything.
4. `_evidence_digests` filters to `<= version` so that "a docket about 441 is not
   different because 442 was ported" — but `build_docket` reaches
   `retirement.standings`, which reads the *whole* series, and a case embeds its
   hook's `Standing`. Porting 442 therefore changes a 441 docket's bytes while
   leaving its operation key alone, and re-recording refuses with "the recorded
   docket does not match the one just computed from the same inputs". It fails
   closed, so nothing is admitted wrongly; the claim in the module docstring is
   still false.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Mapping

from dfinsta_pipeline import retirement, retirement_gate, retirement_record
from dfinsta_pipeline.contracts import (
    ID_PATTERN,
    ArtifactRef,
    GateDecision,
    canonical_json,
    canonical_sha256,
)
from dfinsta_pipeline.ledger import Ledger
from dfinsta_pipeline.retirement import Investigation
from dfinsta_pipeline.retirement_gate import (
    DOCKET_ARTIFACT_KIND,
    GATE_ID_SUFFIX,
    MAX_RATIONALE,
    RULINGS_ARTIFACT_KIND,
    VERDICTS,
    HookRetirementGateV1,
    RetirementGateError,
    RetirementGateRequestV1,
    RetirementGateSubmissionV1,
    RetirementRulingsAdmissionV1,
    RetirementRulingsV1,
    RetirementRulingV1,
    RetirementRunRequestV1,
    RetirementRunResultV1,
    derive_retirement_gate,
    derive_retirement_gate_request,
    derived_gate_id,
    validate_submission,
)
from dfinsta_pipeline.retirement_record import (
    DOCKET_OPERATION_KIND,
    RecordError,
    build_docket,
    operation_input,
    read_investigations,
    record,
    resolve,
    resolve_with,
)
from dfinsta_pipeline.store import ContentStore

from tests.test_retirement import (
    CONTEXT,
    DISCOVER,
    SETTINGS,
    TIGON,
    RetirementTestCase,
    investigation_row,
    triple,
)


# ------------------------------------------------------------------ constants

RUN_ID = "retire-441"
OTHER_RUN_ID = "retire-441-second"
ACTOR = "arnav"
OTHER_ACTOR = "mallory"
OWNER_TOKEN = "retirement-owner-1"
POLICY_REVISION = "2026-08-01"
OTHER_POLICY_REVISION = "2026-09-01"
VERSION = "441"
GATE_ID = f"{RUN_ID}{GATE_ID_SUFFIX}"
ISSUED_AT = "2026-08-08T09:30:00+00:00"

#: A rationale long enough to be a sentence a human would write. Blank-ness, not
#: length, is what `validate_submission` refuses, so the fixture is never blank
#: and every test that wants blank says so.
RATIONALE = "The long-press surface is gone from the app; the anchor is dead code."

#: Two hooks, because a docket with one cannot distinguish "every hook was ruled
#: on" from "a hook was ruled on".
HOOK_A = SETTINGS
HOOK_B = DISCOVER

CASE_A = "a1" * 32
CASE_B = "b2" * 32
OTHER_DIGEST = "cd" * 32


def artifact(kind: str, body: bytes, *, sha256: str | None = None, size: int | None = None,
             producer: str = "retirement-op-1", input_hashes: tuple[str, ...] = ()) -> ArtifactRef:
    """A reference to `body`, with the digest and size overridable one at a time.

    `uri` follows whichever digest is in force, because `ArtifactRef` refuses a
    uri that does not — so a test that breaks the digest is breaking the binding
    between the reference and the document, which is what it means to, and not
    the reference's own internal consistency.
    """

    digest = hashlib.sha256(body).hexdigest() if sha256 is None else sha256
    return ArtifactRef(
        schema_version=1,
        kind=kind,
        sha256=digest,
        size=len(body) if size is None else size,
        uri=f"cas://sha256/{digest}",
        producer_operation_id=producer,
        input_hashes=input_hashes,
    )


def docket_document(version: str = VERSION, *, hooks: tuple[str, ...] = (HOOK_A, HOOK_B),
                    policy_revision: str = POLICY_REVISION) -> dict[str, Any]:
    """The shape `resolve_with` reads out of CAS: a version and a list of cases.

    Small on purpose. Only :class:`ResolveRefusalTests` uses it, and only its
    `version` and its cases' `hook_id`s are read; a full `build_docket` document
    there would be testing `build_docket` again through a longer route.
    """

    return {
        "schema_version": 1,
        "version": version,
        "policy_revision": policy_revision,
        "cases": [{"hook_id": hook, "version": version} for hook in hooks],
    }


# ============================================================ pure: identifiers


class DerivedGateIdTests(unittest.TestCase):
    """`<run_id>-hook-retirement-gate`, or a refusal. Never a shortening.

    A truncated gate id still looks plausible and stops matching the client's
    `matches` predicate, so the gate is answerable in a test and unanswerable in
    production. The length arithmetic is asserted against `ID_PATTERN` rather
    than against the number 128, so the test follows the pattern if it moves.
    """

    def test_the_gate_id_is_the_run_id_and_the_suffix(self) -> None:
        self.assertEqual(derived_gate_id(RUN_ID), f"{RUN_ID}{GATE_ID_SUFFIX}")
        self.assertEqual(GATE_ID_SUFFIX, "-hook-retirement-gate")

    def test_a_run_id_that_is_not_an_identifier_is_refused(self) -> None:
        for value in ("", "retire 441", "-retire-441", "retire/441", "retire\n441", 441, None):
            with self.subTest(run_id=value):
                with self.assertRaises(RetirementGateError) as caught:
                    derived_gate_id(value)  # type: ignore[arg-type]
                self.assertIn("run id", str(caught.exception))

    def test_a_run_id_whose_gate_id_would_overflow_is_refused_not_shortened(self) -> None:
        longest = len(ID_PATTERN.pattern and "A") + 127  # the pattern's own maximum
        run_id = "r" * (longest - len(GATE_ID_SUFFIX) + 1)
        self.assertIsNotNone(ID_PATTERN.fullmatch(run_id), "the run id itself must be valid")

        with self.assertRaises(RetirementGateError) as caught:
            derived_gate_id(run_id)

        message = str(caught.exception)
        self.assertIn("not a valid identifier", message)
        self.assertIn(run_id, message)

    def test_the_longest_run_id_that_still_fits_is_accepted(self) -> None:
        """Positive control: the refusal above is about overflow, not about length.

        Without this, a `derived_gate_id` that refused every long run id would
        pass the test above and quietly make a whole class of run ids ungateable.
        """
        run_id = "r" * (128 - len(GATE_ID_SUFFIX))
        gate_id = derived_gate_id(run_id)
        self.assertEqual(len(gate_id), 128)
        self.assertIsNotNone(ID_PATTERN.fullmatch(gate_id))
        self.assertTrue(gate_id.startswith(run_id))

    def test_nothing_it_returns_is_ever_a_prefix_of_the_run_id(self) -> None:
        for length in (1, 2, 50, 106, 107):
            run_id = "r" * length
            with self.subTest(length=length):
                self.assertEqual(derived_gate_id(run_id), run_id + GATE_ID_SUFFIX)


# ================================================== pure: the history envelope


def gate(**overrides: Any) -> HookRetirementGateV1:
    fields: dict[str, Any] = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "gate_id": GATE_ID,
        "request_sha256": "e3" * 32,
        "allowed_actor": ACTOR,
        "policy_revision": POLICY_REVISION,
    }
    fields.update(overrides)
    return HookRetirementGateV1(**fields)


class HookRetirementGateV1Tests(unittest.TestCase):
    """Six scalars and nothing a human wrote."""

    def test_it_carries_six_fields_and_no_prose(self) -> None:
        names = [field.name for field in dataclasses.fields(HookRetirementGateV1)]
        self.assertEqual(
            names,
            [
                "schema_version",
                "run_id",
                "gate_id",
                "request_sha256",
                "allowed_actor",
                "policy_revision",
            ],
        )
        self.assertEqual(set(gate().to_dict()), set(names))

    def test_it_round_trips_and_hashes_stably(self) -> None:
        original = gate()
        self.assertEqual(HookRetirementGateV1.from_dict(original.to_dict()), original)
        self.assertEqual(original.sha256, gate().sha256)
        self.assertEqual(original.sha256, canonical_sha256(original.to_dict()))

    def test_a_gate_id_that_does_not_derive_from_the_run_id_is_refused(self) -> None:
        for gate_id in (
            f"{OTHER_RUN_ID}{GATE_ID_SUFFIX}",
            RUN_ID,
            f"{RUN_ID}-hook-retirement-gate-2",
            f"{RUN_ID}{GATE_ID_SUFFIX}x",
        ):
            with self.subTest(gate_id=gate_id):
                with self.assertRaises(RetirementGateError) as caught:
                    gate(gate_id=gate_id)
                self.assertIn("derive", str(caught.exception))

    def test_each_scalar_is_validated(self) -> None:
        for field, value, fragment in (
            ("schema_version", 2, "schema"),
            ("run_id", "retire 441", "run id"),
            ("request_sha256", "not-a-digest", "request digest"),
            ("request_sha256", "AB" * 32, "request digest"),
            ("allowed_actor", "", "allowed actor"),
            ("policy_revision", "2026 08 01", "policy revision"),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(RetirementGateError) as caught:
                    gate(**{field: value})
                self.assertIn(fragment, str(caught.exception).lower())

    def test_changing_any_scalar_changes_the_hash(self) -> None:
        """Positive control for every equality assertion made on this hash."""
        base = gate().sha256
        for field, value in (
            ("run_id", "retire-442"),
            ("request_sha256", "f0" * 32),
            ("allowed_actor", OTHER_ACTOR),
            ("policy_revision", OTHER_POLICY_REVISION),
        ):
            with self.subTest(field=field):
                changed = {field: value}
                if field == "run_id":
                    changed["gate_id"] = f"{value}{GATE_ID_SUFFIX}"
                self.assertNotEqual(gate(**changed).sha256, base)


# ============================================================= pure: the subject


def docket_ref(body: bytes = b"docket-bytes", **overrides: Any) -> ArtifactRef:
    return artifact(DOCKET_ARTIFACT_KIND, body, **overrides)


def request(**overrides: Any) -> RetirementGateRequestV1:
    fields: dict[str, Any] = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "gate_id": GATE_ID,
        "docket": docket_ref(),
        "version": VERSION,
        "policy_revision": POLICY_REVISION,
        "allowed_actor": ACTOR,
        "hook_ids": (HOOK_A, HOOK_B),
    }
    fields.update(overrides)
    return RetirementGateRequestV1(**fields)


class RetirementGateRequestV1Tests(unittest.TestCase):
    """The derived subject: the bytes a human's decision is bound to."""

    def test_it_round_trips_and_hashes_stably(self) -> None:
        original = request()
        self.assertEqual(RetirementGateRequestV1.from_dict(original.to_dict()), original)
        self.assertEqual(original.sha256, request().sha256)

    def test_a_docket_with_no_hooks_has_nothing_to_ask(self) -> None:
        with self.assertRaises(RetirementGateError) as caught:
            request(hook_ids=())
        self.assertIn("nothing to ask", str(caught.exception))

    def test_a_hook_ruled_on_twice_in_the_subject_is_refused(self) -> None:
        with self.assertRaises(RetirementGateError) as caught:
            request(hook_ids=(HOOK_A, HOOK_B, HOOK_A))
        self.assertIn("Duplicate hook id", str(caught.exception))

    def test_hook_ids_must_be_a_tuple_of_identifiers(self) -> None:
        for value, fragment in (
            ([HOOK_A], "hook"),
            ((HOOK_A, ""), "hook id"),
            ((HOOK_A, "not a hook"), "hook id"),
            ((HOOK_A, 7), "hook id"),
        ):
            with self.subTest(hook_ids=value):
                with self.assertRaises(RetirementGateError) as caught:
                    request(hook_ids=value)
                self.assertIn(fragment, str(caught.exception).lower())

    def test_the_docket_must_be_an_artifact_ref_of_the_docket_kind(self) -> None:
        with self.assertRaises(RetirementGateError) as caught:
            request(docket=artifact(RULINGS_ARTIFACT_KIND, b"rulings"))
        self.assertIn(DOCKET_ARTIFACT_KIND, str(caught.exception))

        with self.assertRaises(RetirementGateError) as caught:
            request(docket={"kind": DOCKET_ARTIFACT_KIND})
        self.assertIn("ArtifactRef", str(caught.exception))

    def test_the_gate_id_must_derive_from_the_run_id(self) -> None:
        with self.assertRaises(RetirementGateError) as caught:
            request(gate_id=f"{OTHER_RUN_ID}{GATE_ID_SUFFIX}")
        self.assertIn("derive", str(caught.exception))

    def test_the_version_and_the_actor_are_inside_the_hashed_bytes(self) -> None:
        """Both are load-bearing at the far end and neither may be a free scalar.

        `allowed_actor` is hashed so the admitting Activity can check who
        answered without trusting History; `version` is hashed so a docket cannot
        be answered as though it were another port's.
        """
        base = request().sha256
        self.assertNotEqual(request(allowed_actor=OTHER_ACTOR).sha256, base)
        self.assertNotEqual(request(version="442").sha256, base)
        self.assertIn("allowed_actor", request().to_dict())
        self.assertIn("version", request().to_dict())

    def test_the_hash_covers_every_field_of_the_docket_reference(self) -> None:
        """Not the digest alone. `producer_operation_id` and `input_hashes` are in
        the subject, so a reference rebuilt from a row rather than loaded from the
        operation derives a different question."""
        base = request().sha256
        for replacement in (
            dataclasses.replace(docket_ref(), input_hashes=("11" * 32,)),
            dataclasses.replace(docket_ref(), producer_operation_id="other-op"),
            docket_ref(b"different-docket-bytes"),
        ):
            with self.subTest(ref=replacement.producer_operation_id):
                self.assertNotEqual(request(docket=replacement).sha256, base)

    def test_hook_order_is_part_of_the_subject(self) -> None:
        """The docket's own order, carried and hashed. Two orders are two subjects,
        and silently sorting here would make the request disagree with the
        document it pins."""
        self.assertNotEqual(request(hook_ids=(HOOK_B, HOOK_A)).sha256, request().sha256)

    def test_from_dict_refuses_hook_ids_that_are_not_an_array(self) -> None:
        row = request().to_dict()
        row["hook_ids"] = HOOK_A
        with self.assertRaises(RetirementGateError) as caught:
            RetirementGateRequestV1.from_dict(row)
        self.assertIn("array", str(caught.exception))


# ============================================================== pure: a ruling


def ruling(hook_id: str = HOOK_A, **overrides: Any) -> RetirementRulingV1:
    fields: dict[str, Any] = {
        "schema_version": 1,
        "hook_id": hook_id,
        "verdict": "retire",
        "rationale": RATIONALE,
        "case_sha256": CASE_A if hook_id == HOOK_A else CASE_B,
    }
    fields.update(overrides)
    return RetirementRulingV1(**fields)


def rulings(*items: RetirementRulingV1, **overrides: Any) -> RetirementRulingsV1:
    fields: dict[str, Any] = {
        "schema_version": 1,
        "docket_sha256": docket_ref().sha256,
        "version": VERSION,
        "policy_revision": POLICY_REVISION,
        "rulings": items or (ruling(HOOK_A), ruling(HOOK_B, verdict="keep")),
    }
    fields.update(overrides)
    return RetirementRulingsV1(**fields)


class RetirementRulingV1Tests(unittest.TestCase):
    """One hook's answer, bound to the case bytes it answers."""

    def test_every_verdict_in_the_vocabulary_is_accepted(self) -> None:
        for verdict in VERDICTS:
            with self.subTest(verdict=verdict):
                self.assertEqual(ruling(verdict=verdict).verdict, verdict)

    def test_an_unknown_verdict_is_refused(self) -> None:
        for verdict in ("approve", "Retire", "ignore", "", None, "unclear"):
            with self.subTest(verdict=verdict):
                with self.assertRaises(RetirementGateError) as caught:
                    ruling(verdict=verdict)
                self.assertIn("Unknown verdict", str(caught.exception))

    def test_the_wire_vocabulary_matches_the_local_workflows(self) -> None:
        """Cross-checked rather than imported, as the module docstring says.

        These are two layers — the wire contract and the local CLI — and a change
        to either that silently changed the other is the coupling worth refusing.
        A test is the right place for the agreement to live.
        """
        self.assertEqual(VERDICTS, retirement.VERDICTS)
        self.assertNotEqual(VERDICTS, retirement.RECOMMENDATIONS)

    def test_a_rationale_at_the_limit_is_accepted_and_one_over_is_not(self) -> None:
        self.assertEqual(len(ruling(rationale="x" * MAX_RATIONALE).rationale), MAX_RATIONALE)
        with self.assertRaises(RetirementGateError) as caught:
            ruling(rationale="x" * (MAX_RATIONALE + 1))
        self.assertIn("rationale", str(caught.exception))
        with self.assertRaises(RetirementGateError):
            ruling(rationale=None)

    def test_a_blank_rationale_is_a_shape_this_type_allows(self) -> None:
        """Deliberately pinned. The blank-rationale refusal is `validate_submission`'s,
        which is why :class:`ValidateSubmissionTests` can reach it at all — if the
        dataclass refused it too, that test would be asserting the wrong layer and
        would keep passing after the authority's clause was deleted."""
        self.assertEqual(ruling(rationale="   ").rationale, "   ")

    def test_the_case_digest_is_required_and_validated(self) -> None:
        for value in ("", "not-a-digest", "AB" * 32, None):
            with self.subTest(case_sha256=value):
                with self.assertRaises(RetirementGateError) as caught:
                    ruling(case_sha256=value)
                self.assertIn("case digest", str(caught.exception))

    def test_it_round_trips(self) -> None:
        original = ruling()
        self.assertEqual(RetirementRulingV1.from_dict(original.to_dict()), original)


class RetirementRulingsV1Tests(unittest.TestCase):
    """The document a human signs."""

    def test_a_document_with_no_rulings_is_not_an_answer(self) -> None:
        with self.assertRaises(RetirementGateError) as caught:
            rulings(rulings=())
        self.assertIn("not an answer", str(caught.exception))

    def test_every_element_must_be_a_ruling(self) -> None:
        with self.assertRaises(RetirementGateError) as caught:
            rulings(rulings=(ruling(HOOK_A), ruling(HOOK_B).to_dict()))
        self.assertIn("RetirementRulingV1", str(caught.exception))

    def test_it_names_the_docket_the_version_and_the_policy(self) -> None:
        row = rulings().to_dict()
        self.assertEqual(row["docket_sha256"], docket_ref().sha256)
        self.assertEqual(row["version"], VERSION)
        self.assertEqual(row["policy_revision"], POLICY_REVISION)

    def test_its_scalars_are_validated(self) -> None:
        for field, value, fragment in (
            ("docket_sha256", "nope", "docket digest"),
            ("version", "", "version"),
            ("policy_revision", "a b", "policy revision"),
            ("schema_version", 2, "schema"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(RetirementGateError) as caught:
                    rulings(**{field: value})
                self.assertIn(fragment, str(caught.exception).lower())

    def test_it_round_trips_through_a_list_of_dicts(self) -> None:
        original = rulings()
        self.assertEqual(RetirementRulingsV1.from_dict(original.to_dict()), original)
        row = original.to_dict()
        row["rulings"] = {"hook": "no"}
        with self.assertRaises(RetirementGateError) as caught:
            RetirementRulingsV1.from_dict(row)
        self.assertIn("array", str(caught.exception))


# ================================================== pure: the remaining envelopes


def decision(**overrides: Any) -> GateDecision:
    subject = overrides.pop("subject", request().sha256)
    fields: dict[str, Any] = {
        "schema_version": 1,
        "decision_id": "retire-441-decision-1",
        "idempotency_id": "retire-441-idem-1",
        "actor": ACTOR,
        "run_id": RUN_ID,
        "gate_id": GATE_ID,
        "subject_sha256": subject,
        "admission_sha256": subject,
        "prepared_sha256": subject,
        "policy_revision": POLICY_REVISION,
        "decision": "approve",
        "rationale": "Read both cases against the manifest and the probe series.",
        "issued_at": ISSUED_AT,
    }
    fields.update(overrides)
    return GateDecision(**fields)


def rulings_ref(document: RetirementRulingsV1 | None = None, **overrides: Any) -> ArtifactRef:
    body = canonical_json((document or rulings()).to_dict()).encode("utf-8")
    return artifact(RULINGS_ARTIFACT_KIND, body, **overrides)


def submission(**overrides: Any) -> RetirementGateSubmissionV1:
    fields: dict[str, Any] = {
        "schema_version": 1,
        "decision": decision(),
        "rulings": rulings_ref(),
    }
    fields.update(overrides)
    return RetirementGateSubmissionV1(**fields)


class WireEnvelopeTests(unittest.TestCase):
    """The shapes that cross between a client, a Workflow and an Activity."""

    def test_a_submission_must_carry_a_gate_decision_and_a_rulings_reference(self) -> None:
        with self.assertRaises(RetirementGateError) as caught:
            submission(decision=decision().__dict__)
        self.assertIn("GateDecision", str(caught.exception))

        with self.assertRaises(RetirementGateError) as caught:
            submission(rulings=docket_ref())
        self.assertIn(RULINGS_ARTIFACT_KIND, str(caught.exception))

    def test_a_submission_round_trips(self) -> None:
        original = submission()
        self.assertEqual(RetirementGateSubmissionV1.from_dict(original.to_dict()), original)

    def test_a_run_request_needs_a_positive_timeout(self) -> None:
        self.assertEqual(RetirementRunRequestV1(1, RUN_ID, 60).gate_timeout_seconds, 60)
        for value in (0, -1, 1.5, "60", None):
            with self.subTest(timeout=value):
                with self.assertRaises(RetirementGateError) as caught:
                    RetirementRunRequestV1(1, RUN_ID, value)
                self.assertIn("timeout", str(caught.exception))

    def test_a_run_request_round_trips(self) -> None:
        original = RetirementRunRequestV1(1, RUN_ID, 60)
        self.assertEqual(RetirementRunRequestV1.from_dict(original.to_dict()), original)

    def test_a_run_result_may_carry_no_decision_and_no_rulings(self) -> None:
        """`blocked` is the state a gate nobody answered ends in, and it has
        neither. A result type that required them would make the timeout path
        unrepresentable and invite an implicit approval."""
        blocked = RetirementRunResultV1(1, RUN_ID, "blocked", None, None)
        self.assertEqual(RetirementRunResultV1.from_dict(blocked.to_dict()), blocked)

    def test_a_run_result_validates_what_it_does_carry(self) -> None:
        for field, value, fragment in (
            ("state", "", "state"),
            ("state", "   ", "state"),
            ("decision_id", "not an id", "decision id"),
            ("rulings", docket_ref(), RULINGS_ARTIFACT_KIND),
        ):
            with self.subTest(field=field):
                fields: dict[str, Any] = {
                    "schema_version": 1,
                    "run_id": RUN_ID,
                    "state": "completed",
                    "decision_id": "retire-441-decision-1",
                    "rulings": rulings_ref(),
                }
                fields[field] = value
                with self.assertRaises(RetirementGateError) as caught:
                    RetirementRunResultV1(**fields)
                self.assertIn(fragment, str(caught.exception))

    def test_a_completed_run_result_round_trips_with_its_rulings(self) -> None:
        original = RetirementRunResultV1(
            1, RUN_ID, "completed", "retire-441-decision-1", rulings_ref()
        )
        self.assertEqual(RetirementRunResultV1.from_dict(original.to_dict()), original)

    def test_an_admission_and_its_decision_must_name_the_same_run(self) -> None:
        with self.assertRaises(RetirementGateError) as caught:
            RetirementRulingsAdmissionV1(1, OTHER_RUN_ID, submission())
        self.assertIn("different runs", str(caught.exception))

    def test_an_admission_round_trips_and_must_carry_a_submission(self) -> None:
        original = RetirementRulingsAdmissionV1(1, RUN_ID, submission())
        self.assertEqual(RetirementRulingsAdmissionV1.from_dict(original.to_dict()), original)
        with self.assertRaises(RetirementGateError) as caught:
            RetirementRulingsAdmissionV1(1, RUN_ID, submission().to_dict())
        self.assertIn("RetirementGateSubmissionV1", str(caught.exception))


class StrictDecodingTests(unittest.TestCase):
    """`_strict` refuses unknown keys AND missing ones, for every decoder.

    Both halves, one at a time, for all eight types. A decoder that tolerates a
    missing key silently supplies a default, and the field most worth omitting
    from a retirement document is the one that binds it to a subject — so the
    missing half is asserted with the same weight as the unknown half rather than
    being taken on trust from the shared helper.
    """

    def documents(self) -> list[tuple[type, dict[str, Any]]]:
        return [
            (HookRetirementGateV1, gate().to_dict()),
            (RetirementGateRequestV1, request().to_dict()),
            (RetirementRulingV1, ruling().to_dict()),
            (RetirementRulingsV1, rulings().to_dict()),
            (RetirementGateSubmissionV1, submission().to_dict()),
            (RetirementRunRequestV1, RetirementRunRequestV1(1, RUN_ID, 60).to_dict()),
            (
                RetirementRunResultV1,
                RetirementRunResultV1(1, RUN_ID, "blocked", None, None).to_dict(),
            ),
            (
                RetirementRulingsAdmissionV1,
                RetirementRulingsAdmissionV1(1, RUN_ID, submission()).to_dict(),
            ),
        ]

    def test_every_decoder_accepts_its_own_output(self) -> None:
        """The control. Without it the two refusal tests below could both pass
        against a decoder that refuses everything."""
        for kind, row in self.documents():
            with self.subTest(kind=kind.__name__):
                self.assertIsInstance(kind.from_dict(row), kind)

    def test_every_decoder_refuses_an_unknown_key(self) -> None:
        for kind, row in self.documents():
            with self.subTest(kind=kind.__name__):
                with self.assertRaises(RetirementGateError) as caught:
                    kind.from_dict({**row, "ruled_by": ACTOR})
                self.assertIn("unknown ruled_by", str(caught.exception))

    def test_every_decoder_refuses_a_missing_key(self) -> None:
        for kind, row in self.documents():
            for name in row:
                with self.subTest(kind=kind.__name__, missing=name):
                    trimmed = {key: value for key, value in row.items() if key != name}
                    with self.assertRaises(RetirementGateError) as caught:
                        kind.from_dict(trimmed)
                    self.assertIn(f"missing {name}", str(caught.exception))

    def test_every_decoder_refuses_something_that_is_not_an_object(self) -> None:
        for kind, _ in self.documents():
            for value in ([], "gate", None, 1):
                with self.subTest(kind=kind.__name__, value=value):
                    with self.assertRaises(RetirementGateError) as caught:
                        kind.from_dict(value)
                    self.assertIn("must be an object", str(caught.exception))


# ================================================================ pure: deriving


class DeriveTests(unittest.TestCase):
    """The subject and its envelope, from recorded state alone."""

    def test_the_request_derives_its_own_gate_id(self) -> None:
        derived = derive_retirement_gate_request(
            RUN_ID, docket_ref(), VERSION, POLICY_REVISION, ACTOR, (HOOK_A, HOOK_B)
        )
        self.assertEqual(derived.gate_id, derived_gate_id(RUN_ID))
        self.assertEqual(derived, request())
        self.assertEqual(derived.sha256, request().sha256)

    def test_the_request_accepts_any_sequence_of_hooks_and_stores_a_tuple(self) -> None:
        derived = derive_retirement_gate_request(
            RUN_ID, docket_ref(), VERSION, POLICY_REVISION, ACTOR, [HOOK_A, HOOK_B]
        )
        self.assertEqual(derived.hook_ids, (HOOK_A, HOOK_B))

    def test_a_run_id_that_cannot_make_a_gate_id_is_refused_at_derivation(self) -> None:
        with self.assertRaises(RetirementGateError):
            derive_retirement_gate_request(
                "retire 441", docket_ref(), VERSION, POLICY_REVISION, ACTOR, (HOOK_A,)
            )

    def test_the_gate_takes_actor_and_policy_from_the_request_and_nowhere_else(self) -> None:
        """A second parameter is a second chance to disagree.

        The signature is asserted as well as the behaviour: a version that grew an
        `allowed_actor=` parameter would still pass a behavioural test that never
        passed one, and the one that reached History would be the one the
        validator enforced.
        """
        self.assertEqual(list(inspect.signature(derive_retirement_gate).parameters), ["request"])
        with self.assertRaises(TypeError):
            derive_retirement_gate(request(), allowed_actor=OTHER_ACTOR)  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            derive_retirement_gate(request(), OTHER_ACTOR)  # type: ignore[call-arg]

        subject = request(allowed_actor=OTHER_ACTOR)
        derived = derive_retirement_gate(subject)
        self.assertEqual(derived.allowed_actor, OTHER_ACTOR)
        self.assertEqual(derived.policy_revision, subject.policy_revision)

    def test_the_gate_carries_the_requests_own_digest(self) -> None:
        subject = request()
        derived = derive_retirement_gate(subject)
        self.assertEqual(derived.request_sha256, subject.sha256)
        self.assertEqual(derived.run_id, subject.run_id)
        self.assertEqual(derived.gate_id, subject.gate_id)
        # Positive control: the envelope tracks the subject rather than a constant.
        self.assertNotEqual(
            derive_retirement_gate(request(version="442")).request_sha256, subject.sha256
        )

    def test_deriving_from_anything_but_an_exact_request_is_refused(self) -> None:
        for value in (request().to_dict(), None, gate()):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(RetirementGateError) as caught:
                    derive_retirement_gate(value)
                self.assertIn("RetirementGateRequestV1", str(caught.exception))


# ======================================================= pure: THE AUTHORITY


class ValidateSubmissionFixture(unittest.TestCase):
    """A triple that validates, and a way to break exactly one thing in it."""

    def setUp(self) -> None:
        self.docket = docket_ref()
        self.request = request(docket=self.docket)
        self.rulings = rulings()

    # `submission` is rebuilt per call so a mutated document's reference is
    # recomputed over the mutated bytes unless the test says otherwise: a test
    # that changed a rulings field and left a stale digest would be caught by the
    # integrity clause and prove nothing about the clause it meant to attack.
    def submission_for(
        self, document: RetirementRulingsV1 | None = None, **decision_overrides: Any
    ) -> RetirementGateSubmissionV1:
        document = self.rulings if document is None else document
        return RetirementGateSubmissionV1(
            schema_version=1,
            decision=decision(subject=self.request.sha256, **decision_overrides),
            rulings=rulings_ref(document),
        )

    def refuses(self, fragment: str, *, document: RetirementRulingsV1 | None = None,
                submitted: RetirementGateSubmissionV1 | None = None,
                subject: RetirementGateRequestV1 | None = None) -> str:
        document = self.rulings if document is None else document
        submitted = self.submission_for(document) if submitted is None else submitted
        with self.assertRaises(RetirementGateError) as caught:
            validate_submission(subject or self.request, submitted, document)
        message = str(caught.exception)
        self.assertIn(fragment, message)
        return message


class ValidateSubmissionTests(ValidateSubmissionFixture):
    """The authority, one broken clause at a time.

    Every test here starts from a triple the control below admits and changes one
    thing. That is the only structure that distinguishes "this function refuses"
    from "this function refuses for the reason claimed", and the recorded failure
    this module is written against — the authority checking less than the filter
    — is invisible to any test that does not attack each clause alone.
    """

    def test_the_control_triple_is_admitted(self) -> None:
        self.assertIsNone(validate_submission(self.request, self.submission_for(), self.rulings))

    # ------------------------------------------------------ document integrity

    def test_a_rulings_reference_whose_digest_is_not_the_documents_is_refused(self) -> None:
        self.refuses(
            "does not match its reference digest",
            submitted=RetirementGateSubmissionV1(
                1, decision(subject=self.request.sha256), rulings_ref(sha256=OTHER_DIGEST)
            ),
        )

    def test_a_rulings_reference_whose_size_is_not_the_documents_is_refused(self) -> None:
        body = canonical_json(self.rulings.to_dict()).encode("utf-8")
        self.refuses(
            "does not match its reference size",
            submitted=RetirementGateSubmissionV1(
                1, decision(subject=self.request.sha256), rulings_ref(size=len(body) + 1)
            ),
        )

    def test_the_digest_is_recomputed_from_the_document_in_hand(self) -> None:
        """A reference to *some other* well-formed rulings document is refused.

        The realistic shape of the attack: both objects are internally valid and
        each hashes to itself, and only comparing them catches it.
        """
        other = rulings(ruling(HOOK_A), ruling(HOOK_B), version=VERSION)
        self.assertNotEqual(
            canonical_json(other.to_dict()), canonical_json(self.rulings.to_dict())
        )
        self.refuses(
            "does not match its reference digest",
            submitted=RetirementGateSubmissionV1(
                1, decision(subject=self.request.sha256), rulings_ref(other)
            ),
        )

    # --------------------------------------------------- binding to this gate

    def test_each_of_the_three_hash_fields_is_checked_on_its_own(self) -> None:
        """All three, not just `subject_sha256`.

        The Workflow sets all three to the request hash and its validator checks
        all three. When this project last left two of them to the filter alone, a
        decision with the right subject and a wrong admission hash was admitted —
        so each is broken here while the other two stay correct.
        """
        for field in ("subject_sha256", "admission_sha256", "prepared_sha256"):
            with self.subTest(field=field):
                self.refuses(
                    "does not bind this retirement subject",
                    submitted=self.submission_for(**{field: OTHER_DIGEST}),
                )

    def test_a_decision_naming_another_run_is_refused(self) -> None:
        self.refuses("does not bind this gate", submitted=self.submission_for(run_id=OTHER_RUN_ID))

    def test_a_decision_naming_another_gate_is_refused(self) -> None:
        self.refuses(
            "does not bind this gate",
            submitted=self.submission_for(gate_id=f"{OTHER_RUN_ID}{GATE_ID_SUFFIX}"),
        )

    def test_a_decision_made_under_another_policy_revision_is_refused(self) -> None:
        self.refuses(
            "different policy revision",
            submitted=self.submission_for(policy_revision=OTHER_POLICY_REVISION),
        )

    def test_an_actor_the_gate_did_not_authorize_is_refused_here_and_not_only_in_the_sandbox(
        self,
    ) -> None:
        """The recorded failure, asserted directly.

        `allowed_actor` is inside the derived bytes precisely so this layer can
        verify it without trusting anything carried through History. The sandbox
        validator also checks it, and that is exactly why this test exists: the
        last time this project split a check that way, the authority dropped its
        half and "who may answer" came to rest entirely on the filter.
        """
        message = self.refuses(
            "not authorized", submitted=self.submission_for(actor=OTHER_ACTOR)
        )
        self.assertIn("actor", message)

    def test_the_subject_is_recomputed_and_cannot_be_supplied(self) -> None:
        """No `request_sha256` parameter, by signature and by keyword.

        A caller that could supply the digest to compare against would be
        asserting what was approved, which is the one thing this gate exists to
        prevent. The behavioural half is below it: the same submission, checked
        against a request differing in one field, is refused — so the number
        really is derived from the request in hand.
        """
        self.assertEqual(
            list(inspect.signature(validate_submission).parameters),
            ["request", "submission", "rulings"],
        )
        with self.assertRaises(TypeError):
            validate_submission(  # type: ignore[call-arg]
                self.request,
                self.submission_for(),
                self.rulings,
                request_sha256=self.request.sha256,
            )

        moved = request(docket=self.docket, version="442")
        self.assertNotEqual(moved.sha256, self.request.sha256)
        self.refuses(
            "does not bind this retirement subject",
            submitted=self.submission_for(),
            subject=moved,
        )

    # ---------------------------------------------- binding to this docket

    def test_rulings_answering_another_docket_are_refused(self) -> None:
        self.refuses(
            "answer a different docket", document=rulings(docket_sha256=OTHER_DIGEST)
        )

    def test_rulings_naming_another_instagram_version_are_refused(self) -> None:
        self.refuses("different Instagram version", document=rulings(version="442"))

    def test_rulings_written_under_another_policy_revision_are_refused(self) -> None:
        self.refuses(
            "different policy revision",
            document=rulings(policy_revision=OTHER_POLICY_REVISION),
        )

    def test_the_docket_binding_is_to_the_reference_the_request_pins(self) -> None:
        """The clause reads the request's docket, not a constant.

        A whole second gate — its own docket, its own subject, its own correctly
        bound decision — is refused the moment it is handed *these* rulings. Every
        other clause passes, so this is the one doing the refusing, and a version
        that compared the rulings against themselves would admit it.
        """
        elsewhere = request(docket=docket_ref(b"a-different-docket"))
        self.assertNotEqual(elsewhere.docket.sha256, self.request.docket.sha256)
        bound_elsewhere = RetirementGateSubmissionV1(
            schema_version=1,
            decision=decision(subject=elsewhere.sha256),
            rulings=rulings_ref(self.rulings),
        )
        self.refuses(
            "answer a different docket", submitted=bound_elsewhere, subject=elsewhere
        )
        # Positive control: the same submission against its own docket's rulings
        # is admitted, so the refusal above is about the docket and nothing else.
        matching = rulings(docket_sha256=elsewhere.docket.sha256)
        self.assertIsNone(
            validate_submission(
                elsewhere,
                RetirementGateSubmissionV1(
                    1, decision(subject=elsewhere.sha256), rulings_ref(matching)
                ),
                matching,
            )
        )

    # ------------------------------------------------------- hook coverage

    def test_a_hook_ruled_on_twice_is_refused(self) -> None:
        self.refuses(
            "ruled on twice",
            document=rulings(ruling(HOOK_A), ruling(HOOK_A, verdict="keep"), ruling(HOOK_B)),
        )

    def test_a_missing_ruling_is_refused_and_is_not_read_as_keep(self) -> None:
        """The clause the whole one-gate-many-hooks design rests on.

        Silence that under-requires is how a bar quietly moves, and the message
        says so — asserted, because a refusal that said only "invalid" would
        invite the next reader to default it.
        """
        message = self.refuses("No ruling for", document=rulings(ruling(HOOK_A)))
        self.assertIn(HOOK_B, message)
        self.assertIn("is not a `keep`", message)

    def test_a_ruling_for_a_hook_not_in_the_docket_is_refused(self) -> None:
        message = self.refuses(
            "not in this docket",
            document=rulings(ruling(HOOK_A), ruling(HOOK_B), ruling(TIGON, verdict="keep")),
        )
        self.assertIn(TIGON, message)

    def test_a_missing_hook_and_an_unknown_hook_together_report_the_missing_one(self) -> None:
        """Order matters in the refusal: the under-answer is the dangerous half."""
        message = self.refuses(
            "No ruling for",
            document=rulings(ruling(HOOK_A), ruling(TIGON, verdict="keep")),
        )
        self.assertIn(HOOK_B, message)

    # ------------------------------------------------------------ rationale

    def test_a_blank_rationale_is_refused_for_every_verdict_including_keep(self) -> None:
        """`keep` too. Going on carrying a hook that does not work is a decision.

        There is no `SILENT_VERDICT` here, unlike the feature gate, and this is
        the test that would fail if one were added quietly.
        """
        for verdict in VERDICTS:
            for blank in ("", "   ", "\n", "\t "):
                with self.subTest(verdict=verdict, rationale=repr(blank)):
                    message = self.refuses(
                        "every verdict needs a rationale",
                        document=rulings(
                            ruling(HOOK_A, verdict=verdict, rationale=blank),
                            ruling(HOOK_B),
                        ),
                    )
                    self.assertIn(HOOK_A, message)
                    self.assertIn("keep", message)

    def test_a_blank_rationale_on_the_last_hook_is_reached(self) -> None:
        """The loop runs to the end, not just over the first entry."""
        self.refuses(
            "every verdict needs a rationale",
            document=rulings(ruling(HOOK_A), ruling(HOOK_B, rationale=" ")),
        )

    # ------------------------------------------------------------ arguments

    def test_each_argument_must_be_the_exact_type(self) -> None:
        good_submission = self.submission_for()
        for args, fragment in (
            ((self.request.to_dict(), good_submission, self.rulings), "RetirementGateRequestV1"),
            ((self.request, good_submission.to_dict(), self.rulings), "RetirementGateSubmissionV1"),
            ((self.request, good_submission, self.rulings.to_dict()), "RetirementRulingsV1"),
            ((None, good_submission, self.rulings), "RetirementGateRequestV1"),
        ):
            with self.subTest(fragment=fragment):
                with self.assertRaises(RetirementGateError) as caught:
                    validate_submission(*args)
                self.assertIn(fragment, str(caught.exception))

    def test_every_refusal_is_a_retirement_gate_error_and_a_value_error(self) -> None:
        """The admitting Activity catches `ValueError` and re-raises non-retryably.

        A refusal that were a `TypeError` would escape that handler and be
        retried by Temporal until the budget ran out, which reads as a broken
        worker rather than as a wrong answer.
        """
        self.assertTrue(issubclass(RetirementGateError, ValueError))
        with self.assertRaises(ValueError):
            validate_submission(self.request, self.submission_for(actor=OTHER_ACTOR), self.rulings)


class AuthorityCompletenessTests(ValidateSubmissionFixture):
    """Everything the sandbox filter refuses, the authority refuses too.

    `HookRetirementRunWorkflow.validate_submit_retirement_rulings` is a filter
    with no store and no ledger. Its clauses are enumerated here as data, and each
    is required to be refused by `validate_submission` on its own. The point is
    not to re-test the clauses one at a time — the class above does that — but to
    keep the two lists joined: a clause added to the sandbox alone leaves this
    test asserting a refusal that no longer happens.
    """

    #: (what the filter checks, the field that breaks it, a bad value).
    FILTER_CLAUSES = (
        ("actor", "actor", OTHER_ACTOR),
        ("run", "run_id", OTHER_RUN_ID),
        ("gate", "gate_id", f"{OTHER_RUN_ID}{GATE_ID_SUFFIX}"),
        ("subject hash", "subject_sha256", OTHER_DIGEST),
        ("admission hash", "admission_sha256", OTHER_DIGEST),
        ("prepared hash", "prepared_sha256", OTHER_DIGEST),
        ("policy revision", "policy_revision", OTHER_POLICY_REVISION),
    )

    def test_the_authority_refuses_every_clause_the_filter_refuses(self) -> None:
        for label, field, value in self.FILTER_CLAUSES:
            with self.subTest(clause=label):
                with self.assertRaises(RetirementGateError):
                    validate_submission(
                        self.request, self.submission_for(**{field: value}), self.rulings
                    )

    def test_the_authority_also_refuses_what_the_filter_structurally_cannot(self) -> None:
        """The four clauses that need the document, which the sandbox cannot read.

        This is the "and more" half of the split. Without it, an implementation
        that simply re-ran the filter's list would pass the test above.
        """
        for label, document in (
            ("wrong docket", rulings(docket_sha256=OTHER_DIGEST)),
            ("wrong version", rulings(version="442")),
            ("missing hook", rulings(ruling(HOOK_A))),
            ("blank rationale", rulings(ruling(HOOK_A, rationale=""), ruling(HOOK_B))),
        ):
            with self.subTest(clause=label):
                with self.assertRaises(RetirementGateError):
                    validate_submission(
                        self.request, self.submission_for(document), document
                    )

    def test_the_rulings_kind_the_filter_names_is_the_one_the_contract_requires(self) -> None:
        """The filter checks `submission.rulings.kind` by hand; the dataclass
        already refuses anything else. Pinned together so a rename breaks here
        rather than at a gate a human is waiting on."""
        self.assertEqual(RULINGS_ARTIFACT_KIND, "hook-retirement-rulings-v1")
        self.assertEqual(DOCKET_ARTIFACT_KIND, retirement_record.DOCKET_OPERATION_KIND)
        with self.assertRaises(RetirementGateError):
            RetirementGateSubmissionV1(1, decision(), docket_ref())


# ======================================================== recording: the corpus


class DocketCorpus(RetirementTestCase):
    """`tests/test_retirement.py`'s evidence tree, plus a state root.

    The manifest gains a `policy_revision`, which `assessment.policy_revision`
    requires of the file and a retirement case does not read. Everything else is
    the shared fixture, so both modules under test see the tree the other tests
    already agree about.
    """

    def setUp(self) -> None:
        super().setUp()
        # `.resolve()` because `/tmp` is a symlink on some systems and the store
        # pins an absolute root; an unresolved copy compares unequal.
        self.tmp = self.tmp.resolve()
        self.manifest = self.tmp / "manifest"
        self.state = self.tmp / "state"
        self.ledger_path = self.state / "ledger.sqlite3"
        self.cas = self.state / "cas"

    def hooks(self, *hook_ids: str, **overrides: Any) -> Path:
        path = super().hooks(*hook_ids, **overrides)
        row = json.loads(path.read_text(encoding="utf-8"))
        row["policy_revision"] = POLICY_REVISION
        path.write_text(json.dumps(row, indent=2), encoding="utf-8")
        return path

    # ------------------------------------------------------------- the corpus

    def two_candidates(self) -> None:
        """441 with two hooks open: one that has never passed and one that fails
        static verification. Two, because a docket with one cannot distinguish
        "every hook was ruled on" from "a hook was ruled on"."""
        shape = {
            CONTEXT: triple(),
            TIGON: triple(),
            DISCOVER: triple(runtime_probe="inconclusive"),
            SETTINGS: triple(static_verified="failed"),
        }
        self.two_ports(dict(shape), dict(shape))

    def all_ready(self) -> None:
        shape = {CONTEXT: triple(), TIGON: triple()}
        self.two_ports(dict(shape), dict(shape))

    def investigations_file(
        self, *hook_ids: str, name: str = "investigations.json", **overrides: Any
    ) -> Path:
        chosen = hook_ids or (HOOK_A, HOOK_B)
        return self.file_at(name, {hook: investigation_row(**overrides) for hook in chosen})

    def investigations(self, *hook_ids: str, **overrides: Any) -> dict[str, Investigation]:
        chosen = hook_ids or (HOOK_A, HOOK_B)
        return {
            hook: Investigation.from_dict(investigation_row(**overrides)) for hook in chosen
        }

    # ----------------------------------------------------------- the recording

    def record_docket(self, *, state: Path | None = None, **overrides: Any):
        arguments: dict[str, Any] = {
            "run_id": RUN_ID,
            "version": VERSION,
            "investigations_path": getattr(self, "_investigations", None)
            or self.investigations_file(),
            "allowed_actor": ACTOR,
            "owner_token": OWNER_TOKEN,
            "root": self.tmp,
        }
        arguments.update(overrides)
        return record(self.state if state is None else state, **arguments)

    def subject_for(self, recorded) -> RetirementGateRequestV1:
        """The gate subject, from a `RecordedDocket` and nothing else.

        Literally the derivation `activities._retirement_request` performs, so the
        equality this fixture asserts is the one the Activity depends on.
        """
        return derive_retirement_gate_request(
            recorded.run_id,
            recorded.docket,
            recorded.version,
            recorded.policy_revision,
            recorded.allowed_actor,
            recorded.hook_ids,
        )

    def blobs(self) -> set[str]:
        if not self.cas.is_dir():
            return set()
        return {str(path.relative_to(self.cas)) for path in self.cas.rglob("*") if path.is_file()}

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = retirement_record.main(
                ["--state-root", str(self.state), "--root", str(self.tmp), *args]
            )
        return code, stdout.getvalue(), stderr.getvalue()

    # ---------------------------------------------------------- planted state

    def plant(
        self,
        run_id: str,
        body: bytes,
        *,
        version: str = VERSION,
        row_input_sha256: str | None = None,
        policy_revision: str = POLICY_REVISION,
        allowed_actor: str = ACTOR,
    ) -> ArtifactRef:
        """A genuine completed operation and a genuine authority row over `body`.

        Everything real except the one thing a test breaks. `record` will not
        produce a row that disagrees with its own operation, nor non-canonical
        bytes in CAS, and constructing either any other way would be testing the
        construction rather than `resolve_with`.
        """
        store = ContentStore(self.cas)
        ledger = Ledger(self.ledger_path)
        key = hashlib.sha256(f"{run_id}-operation".encode("utf-8")).hexdigest()
        operation_input_sha256 = hashlib.sha256(f"{run_id}-input".encode("utf-8")).hexdigest()
        reference = store.put_bytes(
            kind=DOCKET_ARTIFACT_KIND,
            data=body,
            producer_operation_id=key,
            input_hashes=(operation_input_sha256,),
        )
        ledger.begin_operation(
            key, DOCKET_OPERATION_KIND, operation_input_sha256, OWNER_TOKEN, retry_safe=True
        )
        ledger.record_effect(key, OWNER_TOKEN, reference)
        ledger.complete_operation(key, reference)
        ledger.record_retirement_docket_authority(
            {
                "run_id": run_id,
                "operation_key": key,
                "input_sha256": row_input_sha256 or operation_input_sha256,
                "docket_sha256": reference.sha256,
                "version": version,
                "policy_revision": policy_revision,
                "allowed_actor": allowed_actor,
            }
        )
        return reference


# ================================================== recording: the input files


class ReadInvestigationsTests(DocketCorpus):
    """The only part of a docket a machine did not derive."""

    def test_it_reads_an_object_keyed_by_hook(self) -> None:
        path = self.investigations_file()
        found = read_investigations(path)
        self.assertEqual(sorted(found), sorted((HOOK_A, HOOK_B)))
        self.assertIsInstance(found[HOOK_A], Investigation)
        self.assertEqual(found[HOOK_A].recommendation, "retire")

    def test_a_file_that_is_not_there_is_a_refusal_naming_it(self) -> None:
        missing = self.tmp / "nowhere.json"
        with self.assertRaises(RecordError) as caught:
            read_investigations(missing)
        self.assertIn(str(missing), str(caught.exception))

    def test_a_file_that_is_not_json_is_a_refusal(self) -> None:
        path = self.tmp / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(RecordError) as caught:
            read_investigations(path)
        self.assertIn(str(path), str(caught.exception))

    def test_a_json_document_that_is_not_an_object_is_a_refusal(self) -> None:
        for payload in ([investigation_row()], "investigation", 3, None):
            with self.subTest(payload=type(payload).__name__):
                path = self.file_at("shape.json", payload)
                with self.assertRaises(RecordError) as caught:
                    read_investigations(path)
                self.assertIn("keyed by hook", str(caught.exception))

    def test_a_malformed_investigation_is_a_refusal_naming_its_hook(self) -> None:
        for payload, fragment in (
            ({HOOK_A: investigation_row(summary="")}, "summary"),
            ({HOOK_A: investigation_row(investigated_by="  ")}, "who ran it"),
            ({HOOK_A: investigation_row(recommendation="defer")}, "recommendation"),
            ({HOOK_A: {**investigation_row(), "verdict": "retire"}}, "unknown keys"),
            ({HOOK_A: "a sentence"}, "JSON object"),
        ):
            with self.subTest(fragment=fragment):
                path = self.file_at("bad.json", payload)
                with self.assertRaises(RecordError) as caught:
                    read_investigations(path)
                message = str(caught.exception)
                self.assertIn(HOOK_A, message)
                self.assertIn(fragment, message)

    def test_an_agents_recommendation_is_carried_and_is_not_a_verdict(self) -> None:
        """`unclear` has no counterpart in `VERDICTS`, so it cannot be read as one
        by code that mistook a recommendation for an answer."""
        path = self.file_at("unclear.json", {HOOK_A: investigation_row(recommendation="unclear")})
        self.assertEqual(read_investigations(path)[HOOK_A].recommendation, "unclear")
        self.assertNotIn("unclear", VERDICTS)


# ====================================================== recording: the docket


class BuildDocketTests(DocketCorpus):
    """Every open case at a version, as one signable document."""

    def setUp(self) -> None:
        super().setUp()
        self.two_candidates()

    def build(self, **overrides: Any):
        arguments: dict[str, Any] = {
            "version": VERSION,
            "investigations": self.investigations(),
            "policy_revision": POLICY_REVISION,
        }
        arguments.update(overrides)
        return build_docket(self.tmp, **arguments)

    def test_it_holds_a_case_for_every_open_candidate(self) -> None:
        document, cases = self.build()
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["version"], VERSION)
        self.assertEqual(document["policy_revision"], POLICY_REVISION)
        self.assertEqual([case.hook_id for case in cases], [HOOK_A, HOOK_B])
        self.assertEqual([case["hook_id"] for case in document["cases"]], [HOOK_A, HOOK_B])

    def test_every_case_takes_effect_the_version_after_the_one_it_was_built_from(self) -> None:
        """Carried through into the docket, not only into a single-case file.

        The whole reason retirement lands a version late is that a red build must
        not be turnable green by approving one, and a docket is the new path to
        the same file.
        """
        _, cases = self.build()
        for case in cases:
            with self.subTest(hook=case.hook_id):
                self.assertEqual(case.effective_from, "442")

    def test_a_candidate_with_no_investigation_is_refused(self) -> None:
        with self.assertRaises(RecordError) as caught:
            self.build(investigations=self.investigations(HOOK_B))
        message = str(caught.exception)
        self.assertIn(f"no investigation for {HOOK_A}", message)
        self.assertIn("request to rule without evidence", message)

    def test_an_investigation_for_a_hook_that_is_not_a_candidate_is_refused(self) -> None:
        with self.assertRaises(RecordError) as caught:
            self.build(investigations=self.investigations(HOOK_A, HOOK_B, TIGON))
        message = str(caught.exception)
        self.assertIn(TIGON, message)
        self.assertIn("not a candidate", message)

    def test_a_version_that_is_not_a_number_is_refused(self) -> None:
        for version in ("44x", "", "v441", "441.0"):
            with self.subTest(version=version):
                with self.assertRaises(RecordError) as caught:
                    self.build(version=version)
                self.assertIn("not a version number", str(caught.exception))

    def test_the_document_is_reproducible_byte_for_byte(self) -> None:
        first, _ = self.build()
        second, _ = self.build()
        self.assertEqual(canonical_json(first), canonical_json(second))
        # Positive control: it is capable of moving.
        moved, _ = self.build(investigations=self.investigations(summary="Something else."))
        self.assertNotEqual(canonical_json(moved), canonical_json(first))

    def test_the_docket_carries_the_investigation_a_human_will_read(self) -> None:
        """A docket entry saying only that the numbers are red is a request to
        rule without evidence, so the prose has to survive into the document."""
        document, _ = self.build()
        text = canonical_json(document)
        self.assertIn("agent:claude-opus-5", text)
        self.assertIn("classes6.dex", text)


class NoQuestionTests(DocketCorpus):
    """A version where every assessed hook is release-ready."""

    def setUp(self) -> None:
        super().setUp()
        self.all_ready()

    def test_a_docket_with_no_open_case_is_refused(self) -> None:
        """A gate with no question does not need a human.

        A refusal rather than an empty docket because `RetirementGateRequestV1`
        refuses empty `hook_ids` at the far end, and a Workflow that had already
        started waiting would be waiting for an answer nobody could give.
        """
        with self.assertRaises(RecordError) as caught:
            build_docket(
                self.tmp, version=VERSION, investigations={}, policy_revision=POLICY_REVISION
            )
        message = str(caught.exception)
        self.assertIn("A gate with no", message)
        self.assertIn(VERSION, message)

    def test_recording_one_is_refused_too_and_writes_nothing(self) -> None:
        with self.assertRaises(RecordError):
            self.record_docket(investigations_path=self.investigations_file())
        self.assertEqual(self.blobs(), set())
        self.assertFalse(self.ledger_path.exists())


class EvidenceDigestTests(DocketCorpus):
    """`{relative path: sha256}`, filtered to `<= version`.

    Asserted on the helper directly because the property it exists for — a docket
    about 441 is not different because 442 was ported — is a statement about which
    files are read, and the end-to-end route can only observe it through a hash.
    """

    def setUp(self) -> None:
        super().setUp()
        self.two_candidates()

    def digests(self, version: str = VERSION) -> Mapping[str, str]:
        return retirement_record._evidence_digests(self.tmp, version, "439")

    def test_it_names_every_evidence_file_at_or_before_the_version(self) -> None:
        found = self.digests()
        self.assertEqual(
            sorted(found),
            [
                "manifest/differentials/439-440.jsonl",
                "manifest/differentials/440-441.jsonl",
                "manifest/runtime_evidence/439.jsonl",
                "manifest/runtime_evidence/440.jsonl",
                "manifest/runtime_evidence/441.jsonl",
                "manifest/static_evidence/440.jsonl",
                "manifest/static_evidence/441.jsonl",
            ],
        )
        for name, digest in found.items():
            with self.subTest(path=name):
                self.assertEqual(
                    digest, hashlib.sha256((self.tmp / name).read_bytes()).hexdigest()
                )

    def test_a_later_versions_evidence_is_not_read(self) -> None:
        """The whole reason this is per-file and not a directory digest."""
        before = dict(self.digests())
        self.port("442", {CONTEXT: triple(), DISCOVER: triple()}, previous="441")
        self.assertEqual(dict(self.digests()), before)
        # Positive control: 442's files really did arrive and really are readable.
        self.assertTrue((self.manifest / "static_evidence" / "442.jsonl").is_file())
        self.assertIn("manifest/static_evidence/442.jsonl", self.digests("442"))

    def test_changing_an_earlier_versions_evidence_does_move_it(self) -> None:
        before = dict(self.digests())
        path = self.manifest / "runtime_evidence" / "440.jsonl"
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        self.assertNotEqual(dict(self.digests()), before)

    def test_a_version_with_no_evidence_at_or_before_it_is_refused(self) -> None:
        with self.assertRaises(RecordError) as caught:
            retirement_record._evidence_digests(self.tmp, "438", "438")
        self.assertIn("nothing to build", str(caught.exception))


# ================================================ recording: what keys the work


class OperationKeyTests(DocketCorpus):
    """The operation is keyed on the inputs, never on the docket it produces."""

    def setUp(self) -> None:
        super().setUp()
        self.two_candidates()
        self._investigations = self.investigations_file()

    def test_the_input_names_the_inputs_and_nothing_derived(self) -> None:
        payload = operation_input(RUN_ID, VERSION, "aa" * 32, {"b": "1", "a": "2"}, "bb" * 32, POLICY_REVISION)
        self.assertEqual(
            sorted(payload),
            [
                "evidence",
                "investigations_sha256",
                "manifest_sha256",
                "policy_revision",
                # A recorded retirement removes a hook from `candidates`, so it
                # genuinely changes the docket and belongs in the key. Without it
                # the key would say "same question" while the answer had one case
                # fewer, and the operation would adopt a reference for a docket
                # that no longer describes the question.
                "retired",
                "run_id",
                "version",
            ],
        )
        self.assertNotIn("docket_sha256", payload)
        # Evidence is sorted, so two readings of the same tree key identically
        # whatever order the filesystem hands them back in.
        self.assertEqual(list(payload["evidence"]), ["a", "b"])

    def test_recording_the_same_inputs_twice_is_idempotent(self) -> None:
        first = self.record_docket()
        second = self.record_docket()
        self.assertEqual(second.operation_key, first.operation_key)
        self.assertEqual(second.input_sha256, first.input_sha256)
        self.assertEqual(second.docket, first.docket)
        self.assertEqual(second.document, first.document)
        # And no second blob was minted for the same bytes.
        self.assertEqual(len(self.blobs()), 1)

    def test_a_changed_investigations_file_is_a_different_question(self) -> None:
        """Two dockets built from the same evidence and different investigations
        are different questions, and a human who read one must not be recorded as
        having answered the other.

        Recorded into two state roots under the same run id, because the run id is
        itself part of the key and recording twice into one root would be refused
        by the conflict check before the keys could be compared.
        """
        first = self.record_docket(state=self.tmp / "state-a")
        changed = self.investigations_file(name="changed.json", summary="A different finding.")
        second = self.record_docket(state=self.tmp / "state-b", investigations_path=changed)

        self.assertNotEqual(second.operation_key, first.operation_key)
        self.assertNotEqual(second.input_sha256, first.input_sha256)
        self.assertNotEqual(second.docket.sha256, first.docket.sha256)

    def test_the_same_investigations_in_two_state_roots_key_identically(self) -> None:
        """Positive control for the test above: the key is a function of the
        inputs, so two independent recordings of the same inputs agree."""
        first = self.record_docket(state=self.tmp / "state-a")
        second = self.record_docket(state=self.tmp / "state-b")
        self.assertEqual(second.operation_key, first.operation_key)
        self.assertEqual(second.docket.sha256, first.docket.sha256)

    def test_a_changed_manifest_is_a_different_key(self) -> None:
        first = self.record_docket(state=self.tmp / "state-a")
        path = self.manifest / "hooks.json"
        row = json.loads(path.read_text(encoding="utf-8"))
        row["hooks"][0]["intent"] = row["hooks"][0]["intent"] + " (reworded)"
        path.write_text(json.dumps(row, indent=2), encoding="utf-8")
        second = self.record_docket(state=self.tmp / "state-b")
        self.assertNotEqual(second.operation_key, first.operation_key)

    def test_a_changed_earlier_evidence_file_is_a_different_key(self) -> None:
        first = self.record_docket(state=self.tmp / "state-a")
        path = self.manifest / "runtime_evidence" / "440.jsonl"
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        second = self.record_docket(state=self.tmp / "state-b")
        self.assertNotEqual(second.operation_key, first.operation_key)

    def test_a_later_version_being_ported_does_not_move_the_key(self) -> None:
        """A docket about 441 is not different because 442 was ported.

        The 442 evidence deliberately touches only hooks that are not in the
        docket. The case where it touches one is :class:`KnownDefectTests` 4: the
        key stays put, as asserted here, and the docket's own bytes do not.
        """
        first = self.record_docket()
        self.port("442", {CONTEXT: triple(), TIGON: triple()}, previous="441")
        second = self.record_docket()
        self.assertEqual(second.operation_key, first.operation_key)
        self.assertEqual(second.docket, first.docket)
        self.assertEqual(len(self.blobs()), 1)

    def test_an_adopted_docket_that_is_not_the_one_just_computed_is_refused(self) -> None:
        """The guard on the branch where an operation already exists.

        No longer reachable by data, and that is the fix rather than the problem.
        It used to be reached two ways — porting 442, which changed a 441 case's
        embedded `Standing` without changing its key, and recording a retirement,
        which removes a hook from `candidates`. Both are now inputs to the key, so
        identical inputs give an identical docket and this branch can fire only if
        the derivation *code* changes under a recorded operation.

        Still worth a guard, and so still worth a test: adopting silently would
        hand back a `RecordedDocket` whose `docket` reference and `document` are
        two different answers to the same question. Forced by patching the
        derivation, because no data will do it any more.
        """
        first = self.record_docket()
        real = retirement_record.build_docket

        def different(*args, **kwargs):
            document, cases = real(*args, **kwargs)
            return {**document, "policy_revision": "something-else"}, cases

        retirement_record.build_docket = different
        self.addCleanup(setattr, retirement_record, "build_docket", real)

        with self.assertRaises(RecordError) as caught:
            self.record_docket()

        self.assertIn("does not match the one just computed", str(caught.exception))
        # Nothing moved: the row and the store still hold the first answer.
        self.assertEqual(resolve(self.state, RUN_ID).docket, first.docket)
        self.assertEqual(len(self.blobs()), 1)

    def test_recording_a_retirement_is_a_different_question(self) -> None:
        """A retired hook leaves the docket, so the key must move with it.

        One of the two levers the test above used to use. `candidates` excludes a
        hook that already has a retirement, so an otherwise identical recording
        produces a docket with one case fewer — and if the key did not move, the
        operation would adopt the old reference and refuse a recording that was
        entirely legitimate.
        """
        first = self.record_docket()
        (self.tmp / "manifest" / "retirements.jsonl").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "hook_id": first.hook_ids[0],
                    "effective_from": "442",
                    "decision_id": "retire-earlier",
                    "ruled_by": "arnav",
                    "rationale": "settled in an earlier round",
                    "recorded_at": "2026-08-08T00:00:00+00:00",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        # The investigations file shrinks with the docket: `build_docket` refuses
        # an investigation for a hook that is no longer a candidate, which is the
        # neighbouring guard and is doing its job here.
        remaining = [hook for hook in first.hook_ids if hook != first.hook_ids[0]]
        second = self.record_docket(
            run_id="retire-441-again",
            investigations_path=self.investigations_file(*remaining, name="second.json"),
        )
        self.assertNotEqual(first.operation_key, second.operation_key)
        self.assertNotIn(first.hook_ids[0], second.hook_ids)
        self.assertEqual(tuple(remaining), second.hook_ids)

    def test_a_recorded_reference_always_holds_its_own_document(self) -> None:
        """The invariant the branch above protects, stated directly."""
        recorded = self.record_docket()
        self.assertEqual(
            recorded.docket.sha256,
            hashlib.sha256(canonical_json(recorded.document).encode("utf-8")).hexdigest(),
        )
        again = self.record_docket()
        self.assertEqual(
            again.docket.sha256,
            hashlib.sha256(canonical_json(again.document).encode("utf-8")).hexdigest(),
        )

    def test_a_different_version_is_a_different_key(self) -> None:
        self.port("442", {CONTEXT: triple(), DISCOVER: triple(runtime_probe="inconclusive")},
                  previous="441")
        first = self.record_docket(state=self.tmp / "state-a")
        second = self.record_docket(
            state=self.tmp / "state-b",
            version="442",
            investigations_path=self.investigations_file(HOOK_B, name="only-discover.json"),
        )
        self.assertNotEqual(second.operation_key, first.operation_key)
        self.assertEqual(second.version, "442")

    def test_the_expected_docket_may_be_checked_and_may_not_be_supplied(self) -> None:
        recorded = self.record_docket(state=self.tmp / "state-a")
        agreed = self.record_docket(
            state=self.tmp / "state-b", expect_docket_sha256=recorded.docket.sha256
        )
        self.assertEqual(agreed.docket.sha256, recorded.docket.sha256)

        with self.assertRaises(RecordError) as caught:
            self.record_docket(state=self.tmp / "state-c", expect_docket_sha256=OTHER_DIGEST)
        message = str(caught.exception)
        self.assertIn("recomputes rather than adopting", message)
        self.assertIn(recorded.docket.sha256, message)
        # Refused before anything was written: a rejected claim must not leave a
        # blob nothing points at, and the store has no sweeper.
        self.assertEqual(
            [], sorted((self.tmp / "state-c" / "cas").rglob("*.blob"))
        )


class ConflictTests(DocketCorpus):
    """Two dockets under one run is the state where nobody can say which was read."""

    def setUp(self) -> None:
        super().setUp()
        self.two_candidates()
        self._investigations = self.investigations_file()
        self.recorded = self.record_docket()

    def test_a_different_docket_under_the_same_run_is_refused(self) -> None:
        changed = self.investigations_file(name="changed.json", summary="A different finding.")
        with self.assertRaises(RecordError) as caught:
            self.record_docket(investigations_path=changed)
        message = str(caught.exception)
        self.assertIn("Two dockets under one run", message)
        self.assertIn(RUN_ID, message)

    def test_the_conflict_is_caught_before_anything_is_written(self) -> None:
        """The refusal above must not leave an orphan blob in CAS.

        This is the entire reason the check sits ahead of `begin_operation`
        rather than being left to the ledger's own authority row, which refuses
        the same thing one write too late. The store has no sweeper, so a blob
        nothing points at is permanent.
        """
        before = self.blobs()
        changed = self.investigations_file(name="changed.json", summary="A different finding.")
        with self.assertRaises(RecordError):
            self.record_docket(investigations_path=changed)
        self.assertEqual(self.blobs(), before)
        # And the recorded row still names the first docket.
        self.assertEqual(resolve(self.state, RUN_ID).docket, self.recorded.docket)

    def test_a_second_run_id_may_hold_a_second_docket(self) -> None:
        """Positive control: the refusal is about one run holding two dockets, not
        about a state root holding two."""
        changed = self.investigations_file(name="changed.json", summary="A different finding.")
        other = self.record_docket(run_id=OTHER_RUN_ID, investigations_path=changed)
        self.assertNotEqual(other.docket.sha256, self.recorded.docket.sha256)
        self.assertEqual(resolve(self.state, RUN_ID).docket, self.recorded.docket)
        self.assertEqual(resolve(self.state, OTHER_RUN_ID).docket, other.docket)


# ====================================== recording: reached from a run id alone


class RoundTripTests(DocketCorpus):
    """Record, then recover the subject from a run id and recorded state alone."""

    def setUp(self) -> None:
        super().setUp()
        self.two_candidates()
        self._investigations = self.investigations_file()
        self.recorded = self.record_docket()
        self.resolved = resolve(self.state, RUN_ID)

    def test_the_row_carries_the_coordinates_the_recording_returned(self) -> None:
        self.assertEqual(self.resolved.run_id, RUN_ID)
        self.assertEqual(self.resolved.operation_key, self.recorded.operation_key)
        self.assertEqual(self.resolved.input_sha256, self.recorded.input_sha256)
        self.assertEqual(self.resolved.version, VERSION)
        self.assertEqual(self.resolved.policy_revision, POLICY_REVISION)
        self.assertEqual(self.resolved.allowed_actor, ACTOR)
        self.assertEqual(self.resolved.hook_ids, (HOOK_A, HOOK_B))

    def test_it_returns_the_same_artifact_ref_field_for_field(self) -> None:
        """Every field, not just the digest.

        `producer_operation_id` and `input_hashes` are inside the gate's subject
        hash exactly as the SHA is, so a ref rebuilt from a row rather than loaded
        from the operation would derive a different question and the client would
        refuse a genuine gate.
        """
        self.assertIs(type(self.resolved.docket), ArtifactRef)
        for field in dataclasses.fields(ArtifactRef):
            with self.subTest(field=field.name):
                self.assertEqual(
                    getattr(self.resolved.docket, field.name),
                    getattr(self.recorded.docket, field.name),
                )
        self.assertNotEqual(self.resolved.docket.input_hashes, ())
        self.assertEqual(self.resolved.docket.producer_operation_id, self.recorded.operation_key)
        self.assertEqual(self.resolved.docket.kind, DOCKET_ARTIFACT_KIND)

    def test_the_recorded_bytes_are_the_documents_canonical_bytes(self) -> None:
        body = canonical_json(self.recorded.document).encode("utf-8")
        self.assertEqual(self.recorded.docket.sha256, hashlib.sha256(body).hexdigest())
        self.assertEqual(self.recorded.docket.size, len(body))
        self.assertEqual(self.resolved.document, self.recorded.document)

    def test_the_hook_ids_are_read_out_of_the_recorded_bytes(self) -> None:
        """Not carried alongside them. A list supplied by a caller would let a
        human be asked about hooks the pinned document does not contain, and
        `validate_submission` never re-reads the blob."""
        self.assertEqual(
            self.resolved.hook_ids,
            tuple(case["hook_id"] for case in self.resolved.document["cases"]),
        )

    def test_the_re_derived_gate_subject_is_byte_identical(self) -> None:
        """The property the whole design rests on.

        One party publishes only the subject's hash so a Workflow can wait on a
        human; another re-derives it when an answer arrives, days later, having
        seen none of the first party's state. Neither may trust the other's copy.
        """
        recorded_subject = self.subject_for(self.recorded)
        resolved_subject = self.subject_for(self.resolved)

        self.assertEqual(resolved_subject.sha256, recorded_subject.sha256)
        self.assertEqual(
            canonical_json(resolved_subject.to_dict()).encode("utf-8"),
            canonical_json(recorded_subject.to_dict()).encode("utf-8"),
        )
        self.assertEqual(
            derive_retirement_gate(resolved_subject), derive_retirement_gate(recorded_subject)
        )
        # Positive control: this hash is capable of moving. Without it, "the two
        # agree" could be true of a hash that ignores what it is given.
        moved = dataclasses.replace(self.resolved.docket, input_hashes=())
        self.assertNotEqual(
            self.subject_for(dataclasses.replace(self.resolved, docket=moved)).sha256,
            recorded_subject.sha256,
        )

    def test_resolving_twice_gives_the_same_bytes(self) -> None:
        again = resolve(self.state, RUN_ID)
        self.assertEqual(again, self.resolved)
        self.assertEqual(self.subject_for(again).sha256, self.subject_for(self.resolved).sha256)

    def test_a_read_only_ledger_reaches_the_same_subject(self) -> None:
        """The trusted client must be structurally unable to create the state it
        is checking, so the derivation has to work through a ledger it cannot
        write — and give the same answer."""
        handed = resolve_with(
            Ledger(self.ledger_path, read_only=True), ContentStore(self.cas), RUN_ID
        )
        self.assertEqual(handed, self.resolved)
        self.assertEqual(self.subject_for(handed).sha256, self.subject_for(self.recorded).sha256)

    def test_resolve_does_not_write_to_the_ledger(self) -> None:
        def fingerprint() -> tuple[int, int, str]:
            stat = self.ledger_path.stat()
            return (
                stat.st_size,
                stat.st_mtime_ns,
                hashlib.sha256(self.ledger_path.read_bytes()).hexdigest(),
            )

        before = fingerprint()
        resolve(self.state, RUN_ID)
        resolve(self.state, RUN_ID)
        self.assertEqual(fingerprint(), before)

        # Positive control: a real write moves the mtime and the digest. Without
        # it, "unchanged" could be true of a fingerprint that cannot change.
        changed = self.investigations_file(name="changed.json", summary="A different finding.")
        self.record_docket(run_id=OTHER_RUN_ID, investigations_path=changed)
        after = fingerprint()
        self.assertNotEqual(after[1], before[1], "mtime_ns did not move")
        self.assertNotEqual(after[2], before[2], "sha256 did not move")

    def test_the_recorded_kind_is_the_one_the_gate_requires(self) -> None:
        """Pinned against the real constants, so a rename breaks here rather than
        at a gate a human is already waiting on."""
        self.assertEqual(self.recorded.docket.kind, DOCKET_ARTIFACT_KIND)
        self.assertEqual(retirement_record.DOCKET_OPERATION_KIND, DOCKET_ARTIFACT_KIND)
        self.assertIsInstance(self.subject_for(self.recorded), RetirementGateRequestV1)


class ResolveRefusalTests(DocketCorpus):
    """States `record` will not produce, planted one broken thing at a time."""

    def test_a_run_that_was_never_recorded_has_no_docket(self) -> None:
        self.plant(RUN_ID, canonical_json(docket_document()).encode("utf-8"))
        with self.assertRaises(ValueError) as caught:
            resolve(self.state, "retire-999")
        self.assertIn("not recorded", str(caught.exception))

    def test_the_planted_control_resolves_cleanly(self) -> None:
        """Without it, every refusal below could be caused by the planting."""
        document = docket_document()
        reference = self.plant(RUN_ID, canonical_json(document).encode("utf-8"))
        resolved = resolve(self.state, RUN_ID)
        self.assertEqual(resolved.docket, reference)
        self.assertEqual(resolved.document, document)
        self.assertEqual(resolved.hook_ids, (HOOK_A, HOOK_B))

    def test_a_stored_docket_that_is_not_canonical_is_refused(self) -> None:
        """The bytes a human signs the hash of must be the bytes anybody else
        would produce from the same document. Indented JSON hashes differently and
        would make the subject unreproducible."""
        document = docket_document()
        self.plant(RUN_ID, json.dumps(document, indent=2).encode("utf-8"))
        with self.assertRaises(RecordError) as caught:
            resolve(self.state, RUN_ID)
        self.assertIn("not in canonical form", str(caught.exception))

    def test_a_stored_docket_whose_version_disagrees_with_its_row_is_refused(self) -> None:
        self.plant(RUN_ID, canonical_json(docket_document("441")).encode("utf-8"), version="440")
        with self.assertRaises(RecordError) as caught:
            resolve(self.state, RUN_ID)
        self.assertIn("different version than its row", str(caught.exception))

    def test_it_goes_through_the_operation_and_not_around_it(self) -> None:
        """The row carries coordinates, not authority.

        Planted with a row whose `input_sha256` is not the completed operation's.
        The blob is present and its digest matches the row, so an implementation
        that read CAS by the row's digest would answer happily; going through
        `require_completed_operation` refuses, because the operation's own checks
        are not bypassed for a caller who happens to hold a run id.
        """
        self.plant(
            RUN_ID,
            canonical_json(docket_document()).encode("utf-8"),
            row_input_sha256="0f" * 32,
        )
        with self.assertRaises(ValueError) as caught:
            resolve(self.state, RUN_ID)
        self.assertIn("does not match exact claim", str(caught.exception))

    def test_a_docket_recorded_for_one_run_is_not_reachable_from_another(self) -> None:
        self.plant(RUN_ID, canonical_json(docket_document()).encode("utf-8"))
        self.plant(OTHER_RUN_ID, canonical_json(docket_document("440")).encode("utf-8"),
                   version="440")
        self.assertEqual(resolve(self.state, RUN_ID).version, VERSION)
        self.assertEqual(resolve(self.state, OTHER_RUN_ID).version, "440")


# ============================================================ the command line


class CliTests(DocketCorpus):
    """`record` and `show`, and the exit codes a script gates on."""

    def setUp(self) -> None:
        super().setUp()
        self.two_candidates()
        self.investigations_path = self.investigations_file()

    def record_argv(self, *extra: str) -> tuple[int, str, str]:
        return self.run_cli(
            "record",
            "--run-id",
            RUN_ID,
            "--version",
            VERSION,
            "--investigations",
            str(self.investigations_path),
            "--allowed-actor",
            ACTOR,
            "--owner-token",
            OWNER_TOKEN,
            *extra,
        )

    def test_recording_exits_zero_and_prints_the_coordinates(self) -> None:
        code, out, err = self.record_argv()
        self.assertEqual(code, 0, err)
        recorded = resolve(self.state, RUN_ID)
        self.assertIn(recorded.docket.sha256, out)
        self.assertIn(recorded.operation_key, out)
        self.assertIn(HOOK_A, out)
        self.assertIn(HOOK_B, out)
        self.assertIn(ACTOR, out)
        self.assertIn(POLICY_REVISION, out)

    def test_showing_exits_zero_and_agrees_with_the_recording(self) -> None:
        _, recorded_out, _ = self.record_argv()
        code, out, err = self.run_cli("show", "--run-id", RUN_ID)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, recorded_out)

    def test_showing_the_document_prints_the_recorded_bytes(self) -> None:
        self.record_argv()
        code, out, err = self.run_cli("show", "--run-id", RUN_ID, "--document")
        self.assertEqual(code, 0, err)
        printed = json.loads(out)
        self.assertEqual(printed, resolve(self.state, RUN_ID).document)
        self.assertEqual([case["hook_id"] for case in printed["cases"]], [HOOK_A, HOOK_B])

    def test_a_refusal_exits_two_and_says_refused(self) -> None:
        code, out, err = self.record_argv("--expect-docket-sha256", OTHER_DIGEST)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("refused: "), err)
        self.assertIn("recomputes rather than adopting", err)

    def test_a_missing_investigation_exits_two(self) -> None:
        thin = self.investigations_file(HOOK_B, name="thin.json")
        code, _, err = self.run_cli(
            "record", "--run-id", RUN_ID, "--version", VERSION,
            "--investigations", str(thin), "--allowed-actor", ACTOR,
            "--owner-token", OWNER_TOKEN,
        )
        self.assertEqual(code, 2)
        self.assertIn("no investigation for", err)

    def test_a_second_docket_for_one_run_exits_two(self) -> None:
        self.assertEqual(self.record_argv()[0], 0)
        self.investigations_path = self.investigations_file(
            name="changed.json", summary="A different finding."
        )
        code, _, err = self.record_argv()
        self.assertEqual(code, 2)
        self.assertIn("Two dockets under one run", err)

    def test_the_subcommand_is_required(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                retirement_record.main(["--state-root", str(self.state)])
        self.assertNotEqual(caught.exception.code, 0)


# ============================================================== known defects


class ClosedDefectTests(DocketCorpus):
    """What the modules' own docstrings promise and the code does not do.

    `expectedFailure`, so the suite stays green today and reports an *unexpected
    success* the moment one is closed. Each asserts the documented behaviour, not
    the current one.
    """

    def setUp(self) -> None:
        super().setUp()
        self.two_candidates()
        self._investigations = self.investigations_file()

    def test_showing_a_run_that_was_never_recorded_is_a_refusal_not_a_traceback(self) -> None:
        """DEFECT 1. `main` catches `(RecordError, OSError)`; the ledger raises a
        plain `ValueError` for an unrecorded run id, which is the single most
        likely thing to mistype. `assessment_record.main`, the stated model,
        catches `ValueError` as well."""
        self.record_docket()
        code, _, err = self.run_cli("show", "--run-id", "retire-999")
        self.assertEqual(code, 2)
        self.assertIn("refused:", err)

    def test_record_refuses_an_input_file_that_is_not_there_with_its_own_error(self) -> None:
        """DEFECT 2. `record` says "one error type out of this module, matching
        `assessment_record.record`". `assessment_record._record` wraps exactly
        these two reads in a `RecordError` naming the file; this module wraps
        neither, so a library caller that catches the declared refusal type still
        gets a `FileNotFoundError` traceback."""
        with self.assertRaises(RecordError):
            self.record_docket(investigations_path=self.tmp / "nowhere.json")
        with self.assertRaises(RecordError):
            self.record_docket(manifest_path=self.tmp / "nowhere.json")

    def test_record_refuses_a_run_id_that_could_never_be_gated(self) -> None:
        """DEFECT 3. Nothing validates `run_id` or `allowed_actor` as identifiers,
        so a docket is filed and resolvable and `derived_gate_id` refuses it at the
        far end — "answerable in a test and unanswerable in production", which is
        the failure that function's docstring exists to prevent.
        `assessment_record._record` validates both before writing anything."""
        with self.assertRaises(RecordError):
            self.record_docket(run_id="retire 441!", state=self.tmp / "state-a")
        with self.assertRaises(RecordError):
            self.record_docket(allowed_actor="not an actor!", state=self.tmp / "state-b")

    def test_porting_a_later_version_does_not_change_an_earlier_docket(self) -> None:
        """DEFECT 4. `_evidence_digests` filters to `<= version` so that "a docket
        about 441 is not different because 442 was ported" — but `build_docket`
        reaches `retirement.standings`, which reads the whole series, and every
        case embeds its hook's `Standing`. Porting 442 adds `"442"` to
        `assessed_on` inside a 441 case. It fails closed (re-recording refuses with
        "the recorded docket does not match the one just computed from the same
        inputs"), so nothing is admitted wrongly — the docstring's claim is still
        false, and a docket raised before the next port and answered after it
        cannot be re-derived."""
        before, _ = build_docket(
            self.tmp,
            version=VERSION,
            investigations=self.investigations(),
            policy_revision=POLICY_REVISION,
        )
        self.port(
            "442",
            {CONTEXT: triple(), DISCOVER: triple(runtime_probe="inconclusive")},
            previous="441",
        )
        after, _ = build_docket(
            self.tmp,
            version=VERSION,
            investigations=self.investigations(),
            policy_revision=POLICY_REVISION,
        )
        self.assertEqual(canonical_json(after), canonical_json(before))


if __name__ == "__main__":
    unittest.main()

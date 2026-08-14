"""Stage 4a recorded for real, and re-derived from a run id alone.

`feature_gate.py` has been complete and unreachable since it was written, because
the thing it gates on had no producer: nothing in the tree computed a stage 4a
assessment at all. `assessment_record.py` is that producer, and the property the
whole design rests on is not "an assessment exists somewhere" — it is that a
client holding only a run id recovers *the exact recorded* `ArtifactRef`, because
`producer_operation_id` and `input_hashes` are inside the gate's subject hash. A
ref rebuilt from a row rather than loaded from the operation makes the client
refuse every genuine gate, and that failure reads as corruption rather than as
the mistake it is.

So these tests drive the real thing: a real content store and a real SQLite
ledger under a `tempfile` state root, a real begin/effect/complete cycle, and a
re-derivation through `feature_gate.derive_feature_gate_request` on both sides
whose `.sha256` values must be equal. `tests/test_submission_resolver.py` is the
model, including its ledger-file fingerprint and the positive control that a real
write moves all three components of it.

The index fixture is two files, not three. Stage 4a reads `header.json` and
`api_surface.json` and never opens the 63 MB `structural.jsonl` — building an
index without one is how that stays true. `api_surface.json` embeds a copy of the
header, exactly as `tools/indexer/build_index.py` writes it, which is what makes
`OperationKeyTests` a real re-index rather than a contrived byte change.

`ResolveRefusalTests` plants recorded state by hand rather than through `record`,
because two of the states it needs — an authority row disagreeing with its own
operation, and non-canonical bytes in CAS — are states `record` will not produce.
Each plants everything else genuinely and breaks exactly one thing, and each has
a positive control planted the same way that resolves cleanly.
"""

import dataclasses
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Iterable, Mapping

from dfinsta_pipeline import assessment_record, feature_gate
from dfinsta_pipeline.assessment import canonical_bytes, candidate_ids
from dfinsta_pipeline.assessment_record import (
    ASSESSMENT_ARTIFACT_KIND,
    ASSESSMENT_OPERATION_KIND,
    RecordError,
    operation_input,
    record,
    resolve,
)
from dfinsta_pipeline.contracts import ArtifactRef, canonical_json, canonical_sha256
from dfinsta_pipeline.hook_index import (
    API_SURFACE_FILENAME,
    HEADER_FILENAME,
    SCHEMA_VERSION,
)
from dfinsta_pipeline.ledger import Ledger
from dfinsta_pipeline.store import ContentStore

from tests.test_assessment import (
    BLOCK_DEPS,
    CURATED_MEMBERS,
    NOVEL_MEMBERS,
    surface_for,
    write_manifest,
)


RUN_ID = "run-stage4-assessment-1"
OTHER_RUN_ID = "run-stage4-assessment-2"
UNRECORDED_RUN_ID = "run-never-assessed"
ALLOWED_ACTOR = "sam.operator"
OWNER_TOKEN = "stage4-owner-1"
POLICY_REVISION = "2026-08-01"

#: The decode the fixture index claims to have been built from. Bare-hex once the
#: `sha256:` prefix `header.json` writes is stripped, which is the form
#: `SHA256_PATTERN` wants and the form the operation key is keyed on.
CONTENT_HASH = "ab" * 32
OTHER_CONTENT_HASH = "cd" * 32

#: The grouping's descriptor on Instagram 430. Named here only so the fixture
#: looks like the real thing; nothing asserts on it.
DESCRIPTOR = "LX/05jj;"


def ledger_fingerprint(path: Path) -> tuple[int, int, str]:
    """Size, nanosecond mtime and content digest of the ledger file.

    All three, because each alone is weak: a same-size overwrite keeps the size,
    a second-granularity check would miss a fast rewrite, and a digest alone
    would miss a rewrite of identical bytes. Together they are a behavioural
    statement that the file was not written — much stronger than grepping the
    derivation for write calls, which only proves nobody wrote one today. The
    pattern is `tests/test_submission_resolver.py`'s, and so is the requirement
    that a control write move all three.
    """

    stat = path.stat()
    return (stat.st_size, stat.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())


def index_header(
    *,
    decode: str,
    content_hash: str | None = CONTENT_HASH,
    generated_at: str = "2026-08-03T09:00:00Z",
) -> dict[str, Any]:
    """A header carrying the fields this stage's reader actually consults.

    `generated_at` is a real field of the real header and is the volatile one:
    `build_index.py` stamps it from the clock, so re-indexing the same decode
    changes it — and changes the bytes of both files — while `content_hash`,
    which is computed from the decode's contents, does not move.
    """
    header: dict[str, Any] = {
        "kind": "dfinsta.index.header",
        "schema_version": SCHEMA_VERSION,
        "generator": "tools/indexer/build_index.py",
        "generated_at": generated_at,
        "decode_path": decode,
        "decode_name": Path(decode).name,
        "smali_trees": ["smali", "smali_classes2"],
        "counts": {"classes": 1, "api_paths": len(CURATED_MEMBERS)},
        "resource_types_indexed": ["drawable", "id", "layout"],
    }
    if content_hash is not None:
        header["content_hash"] = f"sha256:{content_hash}"
    return header


def write_fake_index(
    directory: Path,
    *,
    content_hash: str | None = CONTENT_HASH,
    generated_at: str = "2026-08-03T09:00:00Z",
    api_paths: Mapping[str, Iterable[str]] | None = None,
    header_content_hash: Any = ...,
) -> Path:
    """Write `header.json` and `api_surface.json` in the shape the builder emits.

    Two files, not three: stage 4a's whole input is the API surface plus the hook
    list, and an index directory with no `structural.jsonl` is what keeps that
    honest — a version of the reader that reached for the structural file would
    fail here rather than quietly cost 63 MB per run.

    `api_surface.json` embeds the header, as the real one does. That is why a
    re-index changes its digest, and therefore why the operation key must not be
    keyed on that digest.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    # A FIXED string, not the temp directory's real path. The decode path is
    # written into `header.json`, hashed into the assessment artifact's
    # `input_hashes`, carried in its `ArtifactRef` into `FeatureGateRequestV1`,
    # and hashed into the gate subject — so an absolute `/tmp/tmpXXXX/...` made
    # the feature gate's `request_sha256` different on every run. That is
    # invisible to these tests, which never compare across runs, and it made the
    # committed replay-History fixtures unreproducible: `capture_history_corpus`
    # regenerated a different `subject_sha256` every time, so a deliberate
    # re-capture could not be reviewed as a diff. `derive_feature_gate_request`
    # is pure; its input was not.
    decode = "/decode/stock-430"
    header = index_header(decode=decode, content_hash=content_hash, generated_at=generated_at)
    if header_content_hash is not ...:
        header["content_hash"] = header_content_hash
    surface = {
        "header": header,
        "api_paths": surface_for({DESCRIPTOR: CURATED_MEMBERS})
        if api_paths is None
        else {literal: sorted(holders) for literal, holders in api_paths.items()},
        "resources": {},
        "resource_names_by_id": {},
        "stable_types": {},
    }
    (directory / HEADER_FILENAME).write_text(
        json.dumps(header, indent=2) + "\n", encoding="utf-8"
    )
    (directory / API_SURFACE_FILENAME).write_text(
        json.dumps(surface, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    return directory


class AssessmentRecordFixture(unittest.TestCase):
    """A temp state root, a two-file index and a loadable manifest."""

    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        # `.resolve()` because `record` resolves the state root and `/tmp` is a
        # symlink on some systems; an unresolved copy would compare unequal.
        self.tmp = Path(holder.name).resolve()
        self.state = self.tmp / "state"
        self.ledger_path = self.state / "ledger.sqlite3"
        self.index = write_fake_index(self.tmp / "index")
        self.manifest = write_manifest(self.tmp / "hooks.json")

    # ------------------------------------------------------------- recording

    def record(self, **overrides: Any):
        arguments: dict[str, Any] = {
            "state_root": self.state,
            "run_id": RUN_ID,
            "index_dir": self.index,
            "manifest_path": self.manifest,
            "allowed_actor": ALLOWED_ACTOR,
            "owner_token": OWNER_TOKEN,
        }
        arguments.update(overrides)
        return record(arguments.pop("state_root"), **arguments)

    def request_for(self, recorded) -> feature_gate.FeatureGateRequestV1:
        """The gate subject, derived from a `RecordedAssessment` and nothing else."""
        return feature_gate.derive_feature_gate_request(
            recorded.run_id,
            recorded.assessment,
            recorded.policy_revision,
            recorded.allowed_actor,
            recorded.candidate_ids,
        )

    # ---------------------------------------------------------- planted state

    def plant(
        self,
        run_id: str,
        body: bytes,
        *,
        document_sha256: str | None = None,
    ) -> ArtifactRef:
        """Record a completed assessment operation over `body` and file its row.

        Everything genuine except the one thing a test breaks: real bytes in CAS,
        a real begin/effect/complete cycle, a real authority row. `record` will
        not produce the states this exists for, and constructing them any other
        way would test the construction rather than `resolve`.
        """
        store = ContentStore(self.state / "cas")
        ledger = Ledger(self.ledger_path)
        operation_key = hashlib.sha256(f"{run_id}-operation".encode("utf-8")).hexdigest()
        input_sha256 = hashlib.sha256(f"{run_id}-input".encode("utf-8")).hexdigest()
        ref = store.put_bytes(
            kind=ASSESSMENT_ARTIFACT_KIND,
            data=body,
            producer_operation_id=operation_key,
            input_hashes=(CONTENT_HASH,),
        )
        ledger.begin_operation(
            operation_key, ASSESSMENT_OPERATION_KIND, input_sha256, OWNER_TOKEN, retry_safe=True
        )
        ledger.record_effect(operation_key, OWNER_TOKEN, ref)
        ledger.complete_operation(operation_key, ref)
        ledger.record_assessment_authority(
            {
                "run_id": run_id,
                "operation_key": operation_key,
                "input_sha256": input_sha256,
                "document_sha256": document_sha256 or ref.sha256,
                "api_surface_sha256": "1" * 64,
                "manifest_sha256": "2" * 64,
                "policy_revision": POLICY_REVISION,
                "allowed_actor": ALLOWED_ACTOR,
            }
        )
        return ref


class RoundTripTests(AssessmentRecordFixture):
    """Record, then recover it all from a run id and recorded state alone."""

    def setUp(self) -> None:
        super().setUp()
        self.recorded = self.record()
        self.resolved = resolve(self.state, RUN_ID)

    def test_resolve_returns_the_operation_key_the_recording_returned(self) -> None:
        self.assertEqual(self.resolved.run_id, RUN_ID)
        self.assertEqual(self.resolved.operation_key, self.recorded.operation_key)
        self.assertEqual(self.resolved.input_sha256, self.recorded.input_sha256)
        self.assertEqual(self.resolved.policy_revision, POLICY_REVISION)
        self.assertEqual(self.resolved.allowed_actor, ALLOWED_ACTOR)

    def test_resolve_returns_the_same_artifact_ref_field_for_field(self) -> None:
        """Every field, not just the digest.

        `canonical_sha256` recurses through dataclasses, so `producer_operation_id`
        and `input_hashes` are inside the gate's subject hash just as much as the
        SHA is. A ref that matched on digest alone would still derive a different
        request, and the client would refuse a genuine gate.
        """
        self.assertIs(type(self.resolved.assessment), ArtifactRef)
        for field in dataclasses.fields(ArtifactRef):
            with self.subTest(field=field.name):
                self.assertEqual(
                    getattr(self.resolved.assessment, field.name),
                    getattr(self.recorded.assessment, field.name),
                )
        self.assertEqual(self.resolved.assessment, self.recorded.assessment)
        self.assertEqual(
            canonical_json(self.resolved.assessment), canonical_json(self.recorded.assessment)
        )
        # Not a default-shaped ref: `input_hashes=()` is the guessed value that
        # would make every derivation disagree, so assert the real one is carried.
        self.assertNotEqual(self.resolved.assessment.input_hashes, ())
        self.assertEqual(
            self.resolved.assessment.producer_operation_id, self.recorded.operation_key
        )

    def test_resolve_returns_the_documents_own_candidate_ids(self) -> None:
        expected = tuple(f"gap:{literal}" for literal in NOVEL_MEMBERS)
        self.assertEqual(self.recorded.candidate_ids, expected)
        self.assertEqual(self.resolved.candidate_ids, expected)
        # Read out of the recorded bytes rather than carried alongside them: a
        # list supplied by a caller would let a human rule on candidates the
        # pinned document does not contain, and `validate_submission` never
        # re-reads the blob.
        self.assertEqual(candidate_ids(self.resolved.document), expected)
        self.assertEqual(self.resolved.document, self.recorded.document)

    def test_the_re_derived_gate_request_is_byte_identical(self) -> None:
        """The property the whole design rests on.

        Two parties derive this subject independently — one publishes only its
        hash so the Workflow can wait on a human, the other re-derives it when a
        submission arrives — and neither may trust the other's copy.
        """
        recorded_request = self.request_for(self.recorded)
        resolved_request = self.request_for(self.resolved)

        self.assertEqual(resolved_request.sha256, recorded_request.sha256)
        self.assertEqual(
            canonical_json(resolved_request).encode("utf-8"),
            canonical_json(recorded_request).encode("utf-8"),
        )
        self.assertEqual(
            feature_gate.derive_assessment_gate(resolved_request),
            feature_gate.derive_assessment_gate(recorded_request),
        )
        # Positive control: this hash is capable of moving. Without it, "the two
        # agree" could be true of a hash that ignores what it is given.
        moved = dataclasses.replace(self.resolved.assessment, input_hashes=())
        self.assertNotEqual(
            self.request_for(
                dataclasses.replace(self.resolved, assessment=moved)
            ).sha256,
            recorded_request.sha256,
        )

    def test_resolve_with_handles_answers_what_resolve_answers(self) -> None:
        """`resolve` is `resolve_with` plus a state root, not a second derivation.

        The handles are passed in so the trusted submission client and a
        preparing Activity call literally the same function over the same
        objects. A second implementation that opened its own connection would
        give the client an unguarded route to the ledger, around the single
        `read_only=True` that is meant to be the whole statement of what it may
        do — so the two entry points have to agree on real recorded state.
        """
        handed = assessment_record.resolve_with(
            Ledger(self.ledger_path, read_only=True), ContentStore(self.state / "cas"), RUN_ID
        )
        self.assertEqual(handed, self.resolved)
        self.assertEqual(self.request_for(handed).sha256, self.request_for(self.resolved).sha256)

    def test_the_recorded_output_ref_carries_the_kind_the_gate_requires(self) -> None:
        """Pinned against the real constant, so a rename breaks here not at a gate.

        `FeatureGateRequestV1` requires this exact kind of the ref it pins. A
        mismatch would produce a ref the gate rejects, one layer away from where
        it was made.
        """
        self.assertEqual(ASSESSMENT_ARTIFACT_KIND, feature_gate.ASSESSMENT_ARTIFACT_KIND)
        self.assertEqual(self.recorded.assessment.kind, feature_gate.ASSESSMENT_ARTIFACT_KIND)
        self.assertEqual(self.resolved.assessment.kind, feature_gate.ASSESSMENT_ARTIFACT_KIND)
        # And the recorded bytes are the document's canonical bytes, so the digest
        # a human signs is the digest of what they were shown.
        self.assertEqual(
            self.recorded.assessment.sha256,
            hashlib.sha256(canonical_bytes(self.recorded.document)).hexdigest(),
        )
        self.assertEqual(
            self.recorded.assessment.size, len(canonical_bytes(self.recorded.document))
        )


class ResolveWritesNothingTests(AssessmentRecordFixture):
    """The client re-derives through a ledger it cannot write.

    A client that can create the state it is checking is not checking anything.
    """

    def setUp(self) -> None:
        super().setUp()
        self.recorded = self.record()

    def test_resolve_does_not_touch_the_ledger_file(self) -> None:
        before = ledger_fingerprint(self.ledger_path)

        first = resolve(self.state, RUN_ID)
        second = resolve(self.state, RUN_ID)

        self.assertEqual(ledger_fingerprint(self.ledger_path), before)
        self.assertEqual(first, second)
        self.assertEqual(self.request_for(first).sha256, self.request_for(self.recorded).sha256)

    def test_the_unchanged_check_would_notice_a_real_write(self) -> None:
        """Positive control for the assertion above, component by component.

        Without it, "the fingerprint did not change" could be true because the
        fingerprint cannot change. All three parts have to be shown live, because
        each covers a different way a write could hide.

        One recording moves the mtime and the digest but not necessarily the
        size: SQLite allocates a page at a time, so a small row lands in space the
        file already had. Measured here at 4 KB pages, the third recording is what
        grows it — so the size half keeps recording until it moves rather than
        asserting a page count this test has no business knowing.
        """
        before = ledger_fingerprint(self.ledger_path)

        self.record(run_id=OTHER_RUN_ID)

        after_one = ledger_fingerprint(self.ledger_path)
        self.assertNotEqual(after_one, before)
        self.assertNotEqual(after_one[1], before[1], "mtime_ns did not move")
        self.assertNotEqual(after_one[2], before[2], "sha256 did not move")

        for extra in range(2, 10):
            self.record(run_id=f"run-control-{extra}")
            after = ledger_fingerprint(self.ledger_path)
            if after[0] != before[0]:
                break
        else:  # pragma: no cover - the file grew well before this
            self.fail("the ledger file never grew, so the size check is inert")
        self.assertNotEqual(after[1], before[1])
        self.assertNotEqual(after[2], before[2])


class IdempotenceTests(AssessmentRecordFixture):
    """The same input recorded twice is one recording; a different one is refused."""

    def test_recording_twice_returns_the_same_operation_and_ref(self) -> None:
        """A retried attempt must not fail, and must not record a second thing.

        The second call re-derives and compares rather than trusting the
        adoption, which is affordable here precisely because stage 4a is a pure
        sub-millisecond function of two byte strings.
        """
        first = self.record()
        second = self.record()

        self.assertEqual(second.operation_key, first.operation_key)
        self.assertEqual(second.assessment, first.assessment)
        self.assertEqual(second, first)
        self.assertEqual(resolve(self.state, RUN_ID).assessment, first.assessment)
        ledger = Ledger(self.ledger_path, read_only=True)
        self.assertEqual(ledger.operation_status(first.operation_key), "completed")

    def test_a_second_different_assessment_for_one_run_is_refused(self) -> None:
        """A run must not silently gain a second assessment.

        `run_id` is the primary key of the authority row for this reason: two
        different assessments for one run is the state where nobody can say which
        one the human was shown.
        """
        first = self.record()
        # A smaller block list makes `feed/timeline/` a fifth candidate, so this
        # is a genuinely different document rather than a different spelling.
        other_manifest = write_manifest(self.tmp / "other-hooks.json", deps=BLOCK_DEPS[1:])

        ledger_before = ledger_fingerprint(self.state / "ledger.sqlite3")
        blobs_before = blob_count(self.state)

        with self.assertRaises(ValueError) as caught:
            self.record(manifest_path=other_manifest)

        self.assertIn("a different assessment is already recorded", str(caught.exception))
        self.assertIn(RUN_ID, str(caught.exception))
        # Refused BEFORE anything is written. The conflict is decidable from
        # values already computed, so discovering it after two CAS blobs and a
        # completed operation would leave the ledger carrying a derivation
        # nothing references.
        self.assertEqual(ledger_fingerprint(self.state / "ledger.sqlite3"), ledger_before)
        self.assertEqual(blob_count(self.state), blobs_before)
        # The refusal is about the run, not about the document being unreadable:
        # the same document under a different run id records cleanly.
        other_run = self.record(run_id=OTHER_RUN_ID, manifest_path=other_manifest)
        self.assertNotEqual(other_run.assessment.sha256, first.assessment.sha256)
        self.assertEqual(len(other_run.candidate_ids), len(first.candidate_ids) + 1)
        # And what the first run resolves to is untouched.
        self.assertEqual(resolve(self.state, RUN_ID).assessment, first.assessment)



def blob_count(state_root) -> int:
    """How many CAS blobs exist, so "nothing was written" covers the store too."""
    return sum(1 for item in (Path(state_root) / "cas").rglob("*") if item.is_file())


class SuppliedDigestTests(AssessmentRecordFixture):
    """`expect_document_sha256` is checked, never adopted.

    It is how a caller that computed its own copy — the driver, say — has it
    checked rather than trusted. Adopting it would record a digest this input
    does not compute, which is the one thing the recomputation exists to prevent.
    """

    def test_a_wrong_expected_digest_is_refused_and_nothing_is_recorded(self) -> None:
        with self.assertRaises(RecordError) as caught:
            self.record(expect_document_sha256="0" * 64)

        self.assertIn("is not what this input computes", str(caught.exception))
        # The check runs before the state root is touched, so there is no ledger,
        # no CAS blob and no authority row to clean up.
        self.assertFalse(self.state.exists(), sorted(self.tmp.iterdir()))

    def test_the_right_expected_digest_records(self) -> None:
        # Positive control: the refusal above is about disagreement, not about
        # supplying a digest at all.
        recorded = self.record()
        again = self.record(expect_document_sha256=recorded.assessment.sha256)
        self.assertEqual(again.assessment, recorded.assessment)

    def test_the_expected_digest_is_the_documents_own(self) -> None:
        """Named separately because it says which digest the caller must supply.

        The document's canonical digest, not the CAS blob's URI and not the
        operation key — and they are different strings, so a caller that supplied
        the wrong one would be refused rather than quietly accepted.
        """
        recorded = self.record()
        expected = hashlib.sha256(canonical_bytes(recorded.document)).hexdigest()
        self.assertEqual(recorded.assessment.sha256, expected)
        self.assertNotEqual(recorded.operation_key, expected)
        for wrong in (recorded.operation_key, recorded.input_sha256):
            with self.subTest(supplied=wrong):
                with self.assertRaises(RecordError):
                    self.record(run_id=OTHER_RUN_ID, expect_document_sha256=wrong)


class RefusedInputTests(AssessmentRecordFixture):
    """What `record` will not take, and what it leaves behind when it refuses."""

    def test_an_index_header_with_no_content_hash_is_refused(self) -> None:
        """Without it the operation key would be keyed on bytes that always move.

        `api_surface.json` embeds `generated_at` and an absolute `decode_path`,
        so a rebuild of the same decode changes its digest. Keying on a header
        that declares no content hash means every re-index is a second,
        conflicting operation rather than the same one.
        """
        for label, index_dir in (
            ("missing", write_fake_index(self.tmp / "no-hash", content_hash=None)),
            ("null", write_fake_index(self.tmp / "null-hash", header_content_hash=None)),
            ("empty", write_fake_index(self.tmp / "empty-hash", header_content_hash="")),
        ):
            with self.subTest(content_hash=label):
                with self.assertRaises(RecordError) as caught:
                    self.record(index_dir=index_dir)
                self.assertIn("content_hash", str(caught.exception))
                self.assertFalse(self.state.exists())

    def test_a_run_id_that_is_not_an_identifier_is_refused(self) -> None:
        for run_id in ("", "run 1", "-leading", "run/1", "a" * 129, None, 1, b"run-1"):
            with self.subTest(run_id=run_id):
                with self.assertRaises(RecordError) as caught:
                    self.record(run_id=run_id)
                self.assertIn("run id must be an identifier", str(caught.exception))
                self.assertFalse(self.state.exists())

    def test_an_allowed_actor_that_is_not_an_identifier_is_refused(self) -> None:
        for actor in ("", "two words", "-leading", None, 1):
            with self.subTest(allowed_actor=actor):
                with self.assertRaises(RecordError) as caught:
                    self.record(allowed_actor=actor)
                self.assertIn("allowed actor must be an identifier", str(caught.exception))
                self.assertFalse(self.state.exists())

    def test_an_empty_owner_token_is_refused(self) -> None:
        for token in ("", None, 1, b"owner"):
            with self.subTest(owner_token=token):
                with self.assertRaises(RecordError) as caught:
                    self.record(owner_token=token)
                self.assertIn("owner token must be a non-empty string", str(caught.exception))
                self.assertFalse(self.state.exists())

    def test_the_same_call_records_once_the_arguments_are_valid(self) -> None:
        # Positive control for the four refusals above: nothing else about this
        # fixture is what makes them fail.
        self.assertEqual(self.record().run_id, RUN_ID)


class OperationKeyTests(AssessmentRecordFixture):
    """The operation is keyed on the decode's `content_hash`, not on file bytes.

    This is the design's central choice and nothing else pins it. Two records go
    to two state roots rather than one, because the same run recorded twice into
    one root is the idempotence question, and this is a question about the key.
    """

    def surface_digest(self, index_dir: Path) -> str:
        return hashlib.sha256((index_dir / API_SURFACE_FILENAME).read_bytes()).hexdigest()

    def test_a_re_index_of_the_same_decode_keeps_the_operation_key(self) -> None:
        """The key is idempotent under a re-index. The recording is not — see below.

        Two state roots, because re-recording a re-indexed surface for the same
        run into the SAME root raises `A different assessment is already recorded
        for this run` today: `record_assessment_authority`'s payload carries
        `api_surface_sha256`, which is exactly the byte-level digest a re-index
        moves and the operation key deliberately ignores. That is reported rather
        than asserted here, so that fixing it does not have to touch this test —
        this one is about the key.
        """
        reindexed = write_fake_index(
            self.tmp / "index-rebuilt", generated_at="2026-08-03T17:45:12Z"
        )
        # The fixture has to bite: if the two files were byte-identical this test
        # would pass no matter what the key was keyed on.
        self.assertNotEqual(
            self.surface_digest(self.index), self.surface_digest(reindexed), "fixture is inert"
        )

        first = self.record()
        second = self.record(state_root=self.tmp / "state-2", index_dir=reindexed)

        self.assertEqual(second.operation_key, first.operation_key)
        self.assertEqual(second.input_sha256, first.input_sha256)
        self.assertEqual(second.assessment.sha256, first.assessment.sha256)

    def test_a_different_decode_content_hash_changes_the_operation_key(self) -> None:
        """The other half: the key moves when the thing it names moves.

        The document is identical across these two — the assessment does not
        depend on the content hash — so the key moving is entirely the key's own
        doing, and a key that ignored the content hash would collide two decodes
        into one operation.
        """
        other_decode = write_fake_index(
            self.tmp / "index-other-decode", content_hash=OTHER_CONTENT_HASH
        )

        first = self.record()
        second = self.record(state_root=self.tmp / "state-2", index_dir=other_decode)

        self.assertNotEqual(second.operation_key, first.operation_key)
        self.assertNotEqual(second.input_sha256, first.input_sha256)
        self.assertEqual(second.document, first.document)
        self.assertEqual(second.assessment.sha256, first.assessment.sha256)

    def test_the_key_moves_with_the_manifest_and_the_run(self) -> None:
        # The remaining two inputs, for completeness: a client rebuilding this key
        # from the authority row needs all four to be the four that went in.
        first = self.record()
        for label, overrides in (
            ("run", {"run_id": OTHER_RUN_ID}),
            (
                "manifest",
                {"manifest_path": write_manifest(self.tmp / "m2.json", deps=BLOCK_DEPS[1:])},
            ),
            (
                "policy_revision",
                {
                    "manifest_path": write_manifest(
                        self.tmp / "m3.json", policy_revision="2026-09-09"
                    )
                },
            ),
        ):
            with self.subTest(changed=label):
                other = self.record(state_root=self.tmp / f"state-{label}", **overrides)
                self.assertNotEqual(other.operation_key, first.operation_key)


class ResolveRefusalTests(AssessmentRecordFixture):
    """What the client refuses rather than deriving a subject from."""

    def setUp(self) -> None:
        super().setUp()
        self.recorded = self.record()

    def test_a_run_the_ledger_has_not_seen_is_refused(self) -> None:
        """It must raise, never return an assessment covering nothing.

        An empty answer here would reach `FeatureGateRequestV1` as a gate over no
        candidates — a human approving nothing, with completeness holding
        vacuously.
        """
        with self.assertRaises(ValueError) as caught:
            resolve(self.state, UNRECORDED_RUN_ID)
        self.assertEqual(str(caught.exception), "Assessment authority is not recorded")
        # The recorded run still resolves, so this is about the run id.
        self.assertEqual(resolve(self.state, RUN_ID).assessment, self.recorded.assessment)

    def test_an_output_digest_disagreeing_with_the_authority_row_is_refused(self) -> None:
        """The row is an index into the operation, not a second source of truth.

        If the two are allowed to disagree, whichever one a reader happens to
        consult decides what the human signed.
        """
        body = canonical_bytes(self.recorded.document)
        planted = self.plant(OTHER_RUN_ID, body, document_sha256="0" * 64)
        self.assertNotEqual(planted.sha256, "0" * 64)

        with self.assertRaises(RecordError) as caught:
            resolve(self.state, OTHER_RUN_ID)
        self.assertIn("does not match the authority row", str(caught.exception))

    def test_non_canonical_recorded_bytes_are_refused(self) -> None:
        """A human must not sign the digest of something other than what they saw.

        The bytes in CAS are what the digest pins. If re-canonicalising them
        changes them, the recorded document is not in the form both derivations
        agree on, and the two sides of the gate would hash different things.
        """
        indented = json.dumps(self.recorded.document, indent=2).encode("utf-8")
        self.assertNotEqual(indented, canonical_bytes(self.recorded.document))
        self.assertEqual(json.loads(indented), self.recorded.document)  # valid JSON
        self.plant(OTHER_RUN_ID, indented)

        with self.assertRaises(RecordError) as caught:
            resolve(self.state, OTHER_RUN_ID)
        self.assertIn("not canonical", str(caught.exception))

    def test_canonical_bytes_planted_the_same_way_resolve_cleanly(self) -> None:
        # Positive control for the two refusals above: state planted this way is
        # resolvable, so each refusal is about the one thing that test broke.
        body = canonical_bytes(self.recorded.document)
        planted = self.plant(OTHER_RUN_ID, body)

        resolved = resolve(self.state, OTHER_RUN_ID)

        self.assertEqual(resolved.assessment, planted)
        self.assertEqual(resolved.document, self.recorded.document)
        self.assertEqual(resolved.candidate_ids, self.recorded.candidate_ids)


if __name__ == "__main__":
    unittest.main()


class ObservationsJoinTheOperationKeyTests(unittest.TestCase):
    """Device evidence is *in* the document, so it must be *in* the key.

    `rulings_sha256` was added to `operation_input` for exactly this reason, and
    the comment beside it says what happens without it: a store that grew since
    the last record computes the same operation key and a different document, and
    `record` refuses with a message about two derivations disagreeing — which is
    true, and names the wrong cause. A corpus walked between two records does the
    same thing one argument further along.

    These are unit tests on the pure key function, deliberately. The failure they
    describe only shows up on the *second* record of one run, which no test that
    records once can reach.
    """

    def test_a_walked_corpus_changes_the_key(self) -> None:
        before = operation_input("r", "a" * 64, "b" * 64, "2026-08-01", "", "")
        after = operation_input("r", "a" * 64, "b" * 64, "2026-08-01", "", "c" * 64)
        self.assertNotEqual(before, after)
        self.assertNotEqual(
            canonical_sha256(before), canonical_sha256(after),
            "a corpus walked between two records must not reuse the key",
        )

    def test_the_field_is_present_even_when_nothing_was_measured(self) -> None:
        """Absent is spelled `""`, the way `rulings_sha256` spells it, so a
        machine with no observations computes a stable key rather than a
        differently-shaped one."""
        payload = operation_input("r", "a" * 64, "b" * 64, "2026-08-01")
        self.assertIn("observations_sha256", payload)
        self.assertEqual("", payload["observations_sha256"])

    def test_the_digest_is_over_contents_and_not_a_listing(self) -> None:
        """Rows are rewritten in place. On 2026-08-13 superseded 440 sessions were
        withdrawn from a file that kept its name, and a digest over a directory
        listing would not have noticed."""
        from dfinsta_pipeline.assessment_record import _observations_digest

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = root / "manifest" / "observations"
            store.mkdir(parents=True)
            self.assertEqual("", _observations_digest(root), "no store is no digest")

            (store / "439.jsonl").write_text('{"a": 1}\n', encoding="utf-8")
            first = _observations_digest(root)
            self.assertTrue(first)

            (store / "439.jsonl").write_text('{"a": 2}\n', encoding="utf-8")
            self.assertNotEqual(first, _observations_digest(root),
                                "the same filename with different rows is a different corpus")

            (store / "440.jsonl").write_text('{"a": 2}\n', encoding="utf-8")
            self.assertNotEqual(_observations_digest(root), first,
                                "a version measured is a different corpus too")

    def test_an_empty_store_directory_reads_as_no_store(self) -> None:
        from dfinsta_pipeline.assessment_record import _observations_digest

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "manifest" / "observations").mkdir(parents=True)
            self.assertEqual("", _observations_digest(Path(tmp)))


class ObservationsRootIsNamedNotGuessedTests(unittest.TestCase):
    """Omitting the store means no device evidence, never "look in the CWD".

    The first version of this defaulted to `Path(".")`, which made every test that
    records an assessment read the **real** committed corpus: the document then
    depended on the process's working directory and on whether anyone had walked
    a phone since. A recorded assessment is the thing a human signs, and it must
    not vary with either.

    "Nobody looked" is also the fail-safe answer, because the gate refuses to
    `block` or `offer_toggle` on it.
    """

    def test_the_field_is_absent_from_the_key_when_no_store_is_named(self) -> None:
        payload = operation_input("r", "a" * 64, "b" * 64, "2026-08-01", "")
        self.assertEqual("", payload["observations_sha256"])

    def test_the_digest_helper_is_never_called_with_a_bare_default(self) -> None:
        """A regression pin on the signature itself: the parameter has no
        filesystem default, so a caller cannot omit it and get the CWD."""
        import inspect

        from dfinsta_pipeline.assessment_record import _record, record

        for function in (record, _record):
            with self.subTest(function=function.__name__):
                default = inspect.signature(function).parameters["observations_root"].default
                self.assertIsNone(default, "the default must be None, not a path")

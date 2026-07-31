import hashlib
import json
import os
import stat
import tempfile
import unittest
from dataclasses import asdict, fields, replace
from pathlib import Path
from unittest import mock

import dfinsta_pipeline.source_admission as source_admission
from dfinsta_pipeline.contracts import ArtifactRef, GateDecision, canonical_json, canonical_sha256
from dfinsta_pipeline.ledger import Ledger
from dfinsta_pipeline.port_contracts import (
    ApktoolFullRebuildBackend,
    HookIntent,
    IntentResolution,
    IntentSpecV2,
    ResolutionSpecV3,
    SourceFile,
    TargetIdentity,
)
from dfinsta_pipeline.replay_contracts import (
    AdmittedReplay,
    AdmittedReplayV3,
    CapabilityBinding,
    GatePreparedEnvelopeV2,
    ReplayRequest,
    ReplayRunSpecV1,
    SourceManifestV1,
    ToolchainProfile,
    admit_replay,
)
from dfinsta_pipeline.source_admission import (
    SourceAdmissionError,
    SourceAdmissionReport,
    SourceAdmissionReportV2,
    admit_source_bundle,
    admit_source_bundle_v2,
    source_tree_sha256,
    verify_staged_source,
    verify_staged_source_v2,
)
from dfinsta_pipeline.verifier import decoded_tree_sha256
from tests.test_phase_b_replay_contracts import admit_v3, fixture_v3


ROOT = Path(__file__).resolve().parents[1]
SPECS = ROOT / "pipeline_specs"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def artifact_ref(kind: str, payload: bytes) -> ArtifactRef:
    sha256 = digest(payload)
    return ArtifactRef(
        1,
        kind,
        sha256,
        len(payload),
        f"cas://sha256/{sha256}",
        "fixture-producer",
        (),
    )


def admitted(records: tuple[SourceFile, ...]) -> AdmittedReplay:
    intent = IntentSpecV2(
        2,
        "policy-source-1",
        (
            HookIntent(
                "source-hook",
                "feature",
                "retain",
                "Synthetic source admission intent",
                ("smali_edit",),
                (),
                (),
            ),
        ),
    )
    profile = ToolchainProfile(
        1,
        "synthetic-full",
        "apktool_full_rebuild",
        (
            CapabilityBinding("build", "a" * 64),
            CapabilityBinding("decode", "b" * 64),
        ),
        (),
    )
    manifest = SourceManifestV1(records)
    stock_payload = b"synthetic stock APK"
    stock_ref = artifact_ref("stock-apk", stock_payload)
    resolution = ResolutionSpecV3(
        3,
        intent.sha256,
        TargetIdentity("example.app", "synthetic", 7, stock_ref.sha256, "monolithic"),
        manifest.sha256,
        ApktoolFullRebuildBackend(
            "apktool_full_rebuild", profile.profile_id, ("classes.dex",)
        ),
        (IntentResolution("source-hook", "implemented", None),),
        (),
        (),
    )
    payloads = {
        "intent-spec": canonical_json(intent).encode("utf-8"),
        "resolution-spec": canonical_json(resolution).encode("utf-8"),
        "source-manifest-v1": canonical_json(manifest.records).encode("utf-8"),
        "toolchain-profile": canonical_json(profile).encode("utf-8"),
    }
    refs = {kind: artifact_ref(kind, payload) for kind, payload in payloads.items()}
    run_spec = ReplayRunSpecV1(
        1,
        "source-run",
        stock_ref.sha256,
        intent.sha256,
        resolution.sha256,
        manifest.sha256,
        profile.sha256,
        ("a" * 64, "b" * 64),
        "source-gate",
        "8" * 64,
        "9" * 64,
        "operator-1",
        intent.policy_revision,
        "monolithic",
    )
    request = ReplayRequest(
        1,
        run_spec.sha256,
        stock_ref,
        refs["intent-spec"],
        refs["resolution-spec"],
        refs["source-manifest-v1"],
        refs["toolchain-profile"],
        (),
    )
    decision = GateDecision(
        1,
        "source-decision",
        "source-attempt",
        run_spec.allowed_actor,
        run_spec.run_id,
        run_spec.gate_id,
        run_spec.sha256,
        run_spec.gate_admission_sha256,
        run_spec.gate_prepared_sha256,
        run_spec.policy_revision,
        "approve",
        "Approved synthetic source admission",
        "2026-01-01T00:00:00Z",
    )
    resolved = {
        canonical_sha256(stock_ref): stock_payload,
        **{
            canonical_sha256(refs[kind]): payload
            for kind, payload in payloads.items()
        },
    }
    return admit_replay(
        run_spec,
        request,
        decision,
        lambda candidate: candidate == decision,
        lambda artifact: resolved[canonical_sha256(artifact)],
    )


def recorded(replay: AdmittedReplay):
    return lambda candidate: candidate == replay


def admitted_v3(records: tuple[SourceFile, ...]) -> AdmittedReplayV3:
    base = admit_v3(fixture_v3())
    manifest = SourceManifestV1(records)
    source_payload = canonical_json(manifest.records).encode("utf-8")
    source_ref = artifact_ref("source-manifest-v1", source_payload)

    resolution = replace(base.resolution, source_bundle_sha256=manifest.sha256)
    resolution_payload = canonical_json(resolution).encode("utf-8")
    resolution_ref = artifact_ref("resolution-spec", resolution_payload)
    gate_prepared = replace(
        base.gate_prepared,
        resolution=resolution_ref,
        source_manifest=source_ref,
    )
    gate_payload = canonical_json(gate_prepared).encode("utf-8")
    gate_inputs = (
        gate_prepared.stock_apk.sha256,
        gate_prepared.intent.sha256,
        gate_prepared.resolution.sha256,
        gate_prepared.source_manifest.sha256,
        gate_prepared.toolchain_profile.sha256,
        *(framework.artifact.sha256 for framework in gate_prepared.frameworks),
        *(tool.artifact.sha256 for tool in gate_prepared.tools),
    )
    gate_sha256 = digest(gate_payload)
    gate_ref = ArtifactRef(
        1,
        "replay-gate-prepared-v2",
        gate_sha256,
        len(gate_payload),
        f"cas://sha256/{gate_sha256}",
        "fixture-producer",
        gate_inputs,
    )
    run_spec = replace(
        base.run_spec,
        resolution_sha256=resolution.sha256,
        source_manifest_sha256=manifest.sha256,
        gate_prepared_sha256=gate_ref.sha256,
        gate_prepared_ref_sha256=canonical_sha256(gate_ref),
    )
    request = replace(
        base.request,
        run_spec_sha256=run_spec.sha256,
        gate_prepared=gate_ref,
        resolution=resolution_ref,
        source_manifest=source_ref,
    )
    decision = replace(
        base.decision,
        subject_sha256=run_spec.sha256,
        prepared_sha256=gate_ref.sha256,
    )
    return AdmittedReplayV3(
        3,
        run_spec,
        request,
        decision,
        base.intent,
        resolution,
        manifest,
        base.profile,
        GatePreparedEnvelopeV2.from_dict(json.loads(gate_payload)),
        base.executor_capabilities,
    )


def load_manifest(target: int) -> tuple[SourceFile, ...]:
    path = SPECS / "source_manifests" / f"instagram_{target}.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    return SourceManifestV1.from_json_value(value).records


class SourceAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.source = self.base / "source"
        self.attempt = self.base / "attempt"
        self.source.mkdir()
        self.attempt.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, relative: str, data: bytes) -> SourceFile:
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return SourceFile(relative, digest(data))

    def ledger_for(self, replay: AdmittedReplayV3, name: str = "ledger.sqlite3") -> Ledger:
        ledger = Ledger(self.base / name)
        ledger.record_decision(replay.decision)
        ledger.record_admitted_replay_v3(replay)
        return ledger

    def test_tiny_success_exact_report_modes_and_no_extras(self) -> None:
        first = self.write("a/first.bin", b"first\x00")
        second = self.write("z.txt", b"second")
        self.write("undeclared.txt", b"do not copy")
        replay = admitted((first, second))

        fsync = source_admission.os.fsync
        with mock.patch.object(source_admission.os, "fsync", wraps=fsync) as fsync_mock:
            report = admit_source_bundle(
                replay, self.source, self.attempt, recorded(replay)
            )
        destination = self.attempt / "admitted-source"

        self.assertEqual(
            report,
            SourceAdmissionReport(
                1,
                replay.sha256,
                replay.source_manifest.sha256,
                source_tree_sha256(destination),
                2,
                "admitted-source",
                True,
            ),
        )
        self.assertEqual(report.sha256, canonical_sha256(report))
        self.assertEqual(report.staged_tree_sha256, decoded_tree_sha256(destination))
        self.assertEqual(
            sorted(path.relative_to(destination).as_posix() for path in destination.rglob("*")),
            ["a", "a/first.bin", "z.txt"],
        )
        self.assertEqual((destination / "a/first.bin").read_bytes(), b"first\x00")
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o555)
        self.assertEqual(stat.S_IMODE((destination / "a").stat().st_mode), 0o555)
        self.assertEqual(stat.S_IMODE((destination / "z.txt").stat().st_mode), 0o444)
        self.assertGreaterEqual(fsync_mock.call_count, 5)
        verify_staged_source(report, replay, destination)

    def test_v1_report_hash_is_compatible(self) -> None:
        report = SourceAdmissionReport(
            1,
            "a" * 64,
            "b" * 64,
            "c" * 64,
            2,
            "admitted-source",
            True,
        )
        self.assertEqual(
            tuple(field.name for field in fields(SourceAdmissionReport)),
            (
                "schema_version",
                "admitted_replay_sha256",
                "source_manifest_sha256",
                "staged_tree_sha256",
                "file_count",
                "relative_destination",
                "passed",
            ),
        )
        self.assertEqual(
            report.sha256,
            "d756d375a1463ee5dafa477d3fc49cf1a2dca89a0ecef74fbe77e66836313928",
        )

    def test_v2_happy_path_field_order_hash_and_verification(self) -> None:
        first = self.write("a/first.bin", b"first\x00")
        second = self.write("z.txt", b"second")
        self.write("undeclared.txt", b"do not copy")
        replay = admitted_v3((first, second))
        ledger = self.ledger_for(replay)

        report = admit_source_bundle_v2(replay, self.source, self.attempt, ledger)
        destination = self.attempt / "admitted-source"

        self.assertEqual(
            tuple(field.name for field in fields(SourceAdmissionReportV2)),
            (
                "schema_version",
                "admitted_replay_sha256",
                "source_manifest_sha256",
                "staged_tree_sha256",
                "file_count",
                "relative_destination",
                "passed",
            ),
        )
        self.assertEqual(
            report,
            SourceAdmissionReportV2(
                2,
                replay.sha256,
                replay.source_manifest.sha256,
                source_tree_sha256(destination),
                2,
                "admitted-source",
                True,
            ),
        )
        self.assertEqual(report.sha256, canonical_sha256(report))
        self.assertEqual(
            report.sha256,
            "8c8ea944686a9d4342749b988e43d40c56225366a94a4ae0d994d8677bdf76c4",
        )
        self.assertFalse((destination / "undeclared.txt").exists())
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o555)
        self.assertEqual(stat.S_IMODE((destination / "a").stat().st_mode), 0o555)
        self.assertEqual(stat.S_IMODE((destination / "z.txt").stat().st_mode), 0o444)
        verify_staged_source_v2(report, replay, destination, ledger)

    def test_windows_device_names_admit_on_linux_v1_and_v2(self) -> None:
        record = self.write("smali/X/AUX.smali", b".class public LX/AUX;\n")
        v1_attempt = self.attempt / "v1"
        v2_attempt = self.attempt / "v2"
        v1_attempt.mkdir()
        v2_attempt.mkdir()

        replay_v1 = admitted((record,))
        report_v1 = admit_source_bundle(
            replay_v1, self.source, v1_attempt, recorded(replay_v1)
        )
        verify_staged_source(report_v1, replay_v1, v1_attempt / "admitted-source")

        replay_v2 = admitted_v3((record,))
        ledger = self.ledger_for(replay_v2)
        report_v2 = admit_source_bundle_v2(replay_v2, self.source, v2_attempt, ledger)
        verify_staged_source_v2(
            report_v2, replay_v2, v2_attempt / "admitted-source", ledger
        )

    def test_v2_missing_authority_precedes_runtime_paths_and_filesystem(self) -> None:
        replay = admitted_v3((SourceFile("missing", digest(b"missing")),))
        ledger = Ledger(self.base / "unrecorded.sqlite3")
        report = SourceAdmissionReportV2(
            2,
            replay.sha256,
            replay.source_manifest.sha256,
            "0" * 64,
            1,
            "admitted-source",
            True,
        )
        shadow = mock.Mock(return_value=replay)
        ledger.require_admitted_replay_v3 = shadow  # type: ignore[method-assign]
        helpers = ("_require_runtime", "_directory_path", "_lexists", "_preflight", "_tree_records")
        patches = [mock.patch.object(source_admission, name) for name in helpers]
        mocks = [patch.start() for patch in patches]
        self.addCleanup(lambda: [patch.stop() for patch in patches])

        with self.assertRaisesRegex(ValueError, "authority"):
            admit_source_bundle_v2(replay, "invalid", "invalid", ledger)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "authority"):
            verify_staged_source_v2(report, replay, "invalid", ledger)  # type: ignore[arg-type]
        shadow.assert_not_called()
        for helper in mocks:
            helper.assert_not_called()

    def test_v2_real_ledger_normalizes_to_distinct_concrete_objects(self) -> None:
        record = self.write("normalized", b"normalized")
        replay = admitted_v3((record,))
        ledger = self.ledger_for(replay)

        normalized = Ledger.require_admitted_replay_v3(ledger, replay)
        self.assertEqual(normalized, replay)
        self.assertIsNot(normalized, replay)
        self.assertIsNot(normalized.source_manifest, replay.source_manifest)
        report = admit_source_bundle_v2(replay, self.source, self.attempt, ledger)
        self.assertEqual(report.admitted_replay_sha256, normalized.sha256)
        self.assertEqual(report.source_manifest_sha256, normalized.source_manifest.sha256)
        self.assertTrue((self.attempt / "admitted-source/normalized").is_file())

    def test_v2_nested_caller_mutation_after_record_fails_closed(self) -> None:
        record = self.write("file", b"source")
        replay = admitted_v3((record,))
        ledger = self.ledger_for(replay)
        object.__setattr__(
            replay.source_manifest,
            "records",
            (SourceFile("other", digest(b"other")),),
        )

        with mock.patch.object(source_admission, "_require_runtime") as runtime_check:
            with self.assertRaisesRegex(ValueError, "authority does not match"):
                admit_source_bundle_v2(replay, self.source, self.attempt, ledger)
        runtime_check.assert_not_called()
        self.assertEqual(tuple(self.attempt.iterdir()), ())

    def test_v2_report_construction_and_decoder_are_strict(self) -> None:
        report = SourceAdmissionReportV2(
            2,
            "a" * 64,
            "b" * 64,
            "c" * 64,
            0,
            "admitted-source",
            True,
        )
        value = asdict(report)
        self.assertEqual(SourceAdmissionReportV2.from_dict(value), report)

        invalid = (
            {**value, "schema_version": True},
            {**value, "schema_version": 1},
            {**value, "file_count": True},
            {**value, "file_count": -1},
            {**value, "relative_destination": 1},
            {**value, "relative_destination": "other"},
            {**value, "passed": 1},
            {**value, "passed": False},
            {**value, "admitted_replay_sha256": 1},
            {**value, "admitted_replay_sha256": "a" * 63},
            {**value, "admitted_replay_sha256": "A" * 64},
            {**value, "source_manifest_sha256": "B" * 64},
            {**value, "staged_tree_sha256": "g" * 64},
            {key: item for key, item in value.items() if key != "passed"},
            {**value, "extra": "field"},
        )
        for mutation in invalid:
            with self.subTest(mutation=mutation), self.assertRaises((TypeError, ValueError)):
                SourceAdmissionReportV2.from_dict(mutation)
        with self.assertRaises(TypeError):
            SourceAdmissionReportV2.from_dict([])

    def test_v2_exact_ledger_candidate_and_report_types_are_required(self) -> None:
        class LedgerSubclass(Ledger):
            pass

        class ReplaySubclass(AdmittedReplayV3):
            pass

        class ReportSubclass(SourceAdmissionReportV2):
            pass

        replay = admitted_v3(())
        ledger = self.ledger_for(replay)
        replay_subclass = ReplaySubclass(
            *(getattr(replay, field.name) for field in fields(AdmittedReplayV3))
        )
        report = SourceAdmissionReportV2(
            2, replay.sha256, replay.source_manifest.sha256, digest(b""), 0, "admitted-source", True
        )
        report_subclass = ReportSubclass(
            *(getattr(report, field.name) for field in fields(SourceAdmissionReportV2))
        )
        ledger_subclass = LedgerSubclass(self.base / "subclass.sqlite3")

        for candidate, authority in (
            (replay_subclass, ledger),
            (object(), ledger),
            (replay, ledger_subclass),
        ):
            with self.subTest(candidate=type(candidate), ledger=type(authority)):
                with self.assertRaises(TypeError):
                    admit_source_bundle_v2(  # type: ignore[arg-type]
                        candidate, self.source, self.attempt, authority
                    )
                with self.assertRaises(TypeError):
                    verify_staged_source_v2(  # type: ignore[arg-type]
                        report, candidate, self.attempt, authority
                    )
        with self.assertRaises(TypeError):
            verify_staged_source_v2(report_subclass, replay, self.attempt, ledger)

    def test_v2_source_hash_mutation_fails_without_destination(self) -> None:
        record = self.write("file", b"admitted")
        replay = admitted_v3((record,))
        ledger = self.ledger_for(replay)
        (self.source / "file").write_bytes(b"mutated")

        with self.assertRaisesRegex(SourceAdmissionError, "SHA-256 mismatch"):
            admit_source_bundle_v2(replay, self.source, self.attempt, ledger)
        self.assertEqual(tuple(self.attempt.iterdir()), ())

    def test_v2_overlap_symlink_and_destination_no_clobber(self) -> None:
        record = self.write("file", b"source")
        replay = admitted_v3((record,))
        ledger = self.ledger_for(replay)
        for source, attempt in (
            (self.source, self.source),
            (self.source, self.base),
        ):
            with self.subTest(source=source, attempt=attempt), self.assertRaises(
                SourceAdmissionError
            ):
                admit_source_bundle_v2(replay, source, attempt, ledger)

        linked = self.base / "linked-source-v2"
        linked.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(SourceAdmissionError):
            admit_source_bundle_v2(replay, linked, self.attempt, ledger)

        destination = self.attempt / "admitted-source"
        destination.mkdir()
        marker = destination / "marker"
        marker.write_bytes(b"existing")
        with self.assertRaises(SourceAdmissionError):
            admit_source_bundle_v2(replay, self.source, self.attempt, ledger)
        self.assertEqual(marker.read_bytes(), b"existing")

    def test_v2_report_relationship_mutations_and_version_interchange_fail(self) -> None:
        record = self.write("file", b"source")
        replay = admitted_v3((record,))
        ledger = self.ledger_for(replay)
        report = admit_source_bundle_v2(replay, self.source, self.attempt, ledger)
        destination = self.attempt / "admitted-source"
        mutations = (
            replace(report, admitted_replay_sha256="0" * 64),
            replace(report, source_manifest_sha256="0" * 64),
            replace(report, staged_tree_sha256="0" * 64),
            replace(report, file_count=2),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(SourceAdmissionError):
                verify_staged_source_v2(mutation, replay, destination, ledger)

        for field_name, value in (
            ("schema_version", True),
            ("admitted_replay_sha256", "A" * 64),
            ("file_count", True),
            ("passed", 1),
        ):
            mutation = SourceAdmissionReportV2.from_dict(asdict(report))
            object.__setattr__(mutation, field_name, value)
            with self.subTest(field=field_name), self.assertRaises((TypeError, ValueError)):
                verify_staged_source_v2(mutation, replay, destination, ledger)

        v1 = SourceAdmissionReport(
            1,
            report.admitted_replay_sha256,
            report.source_manifest_sha256,
            report.staged_tree_sha256,
            report.file_count,
            report.relative_destination,
            report.passed,
        )
        with self.assertRaises(TypeError):
            verify_staged_source_v2(v1, replay, destination, ledger)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            verify_staged_source(report, admitted((record,)), destination)  # type: ignore[arg-type]

    def test_v2_publication_race_does_not_clobber_competitor(self) -> None:
        record = self.write("file", b"source")
        replay = admitted_v3((record,))
        ledger = self.ledger_for(replay)
        publish = source_admission._publish_without_overwrite

        def compete(source: Path, destination: Path) -> None:
            destination.mkdir()
            (destination / "competitor").write_bytes(b"keep")
            publish(source, destination)

        with mock.patch.object(
            source_admission, "_publish_without_overwrite", side_effect=compete
        ), self.assertRaises(SourceAdmissionError):
            admit_source_bundle_v2(replay, self.source, self.attempt, ledger)
        destination = self.attempt / "admitted-source"
        self.assertEqual((destination / "competitor").read_bytes(), b"keep")
        self.assertEqual(tuple(self.attempt.iterdir()), (destination,))

    def test_v2_post_publication_fsync_failure_leaves_destination(self) -> None:
        record = self.write("file", b"source")
        replay = admitted_v3((record,))
        ledger = self.ledger_for(replay)
        fsync_directory = source_admission._fsync_directory

        def fail_after_publish(path: Path) -> None:
            if path == self.attempt and (path / "admitted-source").exists():
                raise OSError("injected attempt fsync failure")
            fsync_directory(path)

        with mock.patch.object(
            source_admission, "_fsync_directory", side_effect=fail_after_publish
        ), self.assertRaises(SourceAdmissionError):
            admit_source_bundle_v2(replay, self.source, self.attempt, ledger)
        self.assertEqual(
            (self.attempt / "admitted-source/file").read_bytes(), b"source"
        )

    def test_v2_verify_rejects_extra_missing_symlink_and_tamper(self) -> None:
        for mutation in ("extra", "missing", "symlink", "tamper"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(
                dir=self.base
            ) as location:
                root = Path(location)
                source = root / "source"
                attempt = root / "attempt"
                source.mkdir()
                attempt.mkdir()
                (source / "file").write_bytes(b"source")
                replay = admitted_v3((SourceFile("file", digest(b"source")),))
                ledger = Ledger(root / "ledger.sqlite3")
                ledger.record_decision(replay.decision)
                ledger.record_admitted_replay_v3(replay)
                report = admit_source_bundle_v2(replay, source, attempt, ledger)
                destination = attempt / "admitted-source"
                os.chmod(destination, 0o755)
                if mutation == "extra":
                    (destination / "extra").write_bytes(b"extra")
                elif mutation == "missing":
                    (destination / "file").unlink()
                elif mutation == "symlink":
                    (destination / "file").unlink()
                    (destination / "file").symlink_to(source / "file")
                else:
                    os.chmod(destination / "file", 0o644)
                    (destination / "file").write_bytes(b"tampered")
                with self.assertRaises(SourceAdmissionError):
                    verify_staged_source_v2(report, replay, destination, ledger)

    def test_v2_authority_survives_ledger_restart(self) -> None:
        record = self.write("file", b"source")
        replay = admitted_v3((record,))
        ledger_path = self.base / "restart.sqlite3"
        ledger = Ledger(ledger_path)
        ledger.record_decision(replay.decision)
        ledger.record_admitted_replay_v3(replay)

        restarted = Ledger(ledger_path)
        report = admit_source_bundle_v2(replay, self.source, self.attempt, restarted)
        verify_staged_source_v2(
            report, replay, self.attempt / "admitted-source", restarted
        )

    def test_staged_bytes_do_not_follow_later_source_mutation(self) -> None:
        record = self.write("file", b"admitted")
        replay = admitted((record,))
        admit_source_bundle(replay, self.source, self.attempt, recorded(replay))
        (self.source / "file").write_bytes(b"changed")
        self.assertEqual((self.attempt / "admitted-source/file").read_bytes(), b"admitted")

    def test_verify_staged_source_rejects_mutation(self) -> None:
        record = self.write("file", b"admitted")
        replay = admitted((record,))
        report = admit_source_bundle(replay, self.source, self.attempt, recorded(replay))
        destination = self.attempt / "admitted-source"
        os.chmod(destination, 0o755)
        os.chmod(destination / "file", 0o644)
        (destination / "file").write_bytes(b"mutated")
        with self.assertRaises(SourceAdmissionError):
            verify_staged_source(report, replay, destination)

    def test_runtime_scope_rejects_before_callback_reads_or_mutation(self) -> None:
        record = self.write("file", b"source")
        replay = admitted((record,))
        callback = mock.Mock(return_value=True)
        cases = (
            mock.patch.object(source_admission.os, "name", "nt"),
            mock.patch.object(source_admission.os, "supports_dir_fd", set()),
            mock.patch.object(source_admission.os, "O_NOFOLLOW", 0),
            mock.patch.object(
                source_admission,
                "_load_renameat2",
                side_effect=SourceAdmissionError("renameat2 unavailable"),
            ),
        )
        for capability in cases:
            with self.subTest(capability=capability):
                callback.reset_mock()
                with capability, mock.patch.object(
                    source_admission, "_read_relative"
                ) as read_relative:
                    with self.assertRaises(SourceAdmissionError):
                        admit_source_bundle(replay, self.source, self.attempt, callback)
                    callback.assert_not_called()
                    read_relative.assert_not_called()
                    self.assertEqual(tuple(self.attempt.iterdir()), ())

    def test_recorded_admission_predicate_is_strict_and_precedes_source_reads(self) -> None:
        replay = admitted((SourceFile("missing", digest(b"missing")),))
        predicates = (
            lambda _: False,
            lambda _: 1,
            mock.Mock(side_effect=RuntimeError("ledger unavailable")),
        )
        for predicate in predicates:
            with self.subTest(predicate=predicate), mock.patch.object(
                source_admission, "_read_relative"
            ) as read_relative:
                with self.assertRaises(SourceAdmissionError):
                    admit_source_bundle(replay, self.source, self.attempt, predicate)
                read_relative.assert_not_called()
                self.assertEqual(tuple(self.attempt.iterdir()), ())

    def test_missing_tampered_symlink_ancestor_symlink_and_nonregular_fail(self) -> None:
        cases = ("missing", "tampered", "symlink", "ancestor", "directory")
        for case in cases:
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory(dir=self.base) as location:
                    source = Path(location) / "source"
                    attempt = Path(location) / "attempt"
                    source.mkdir()
                    attempt.mkdir()
                    expected = digest(b"expected")
                    if case == "tampered":
                        (source / "item").write_bytes(b"wrong")
                        relative = "item"
                    elif case == "symlink":
                        (source / "target").write_bytes(b"expected")
                        (source / "item").symlink_to("target")
                        relative = "item"
                    elif case == "ancestor":
                        (source / "real").mkdir()
                        (source / "real/item").write_bytes(b"expected")
                        (source / "link").symlink_to("real", target_is_directory=True)
                        relative = "link/item"
                    elif case == "directory":
                        (source / "item").mkdir()
                        relative = "item"
                    else:
                        relative = "item"
                    with self.assertRaises(SourceAdmissionError):
                        replay = admitted((SourceFile(relative, expected),))
                        admit_source_bundle(replay, source, attempt, recorded(replay))
                    self.assertEqual(tuple(attempt.iterdir()), ())

    def test_destination_exists_without_clobber(self) -> None:
        record = self.write("file", b"source")
        destination = self.attempt / "admitted-source"
        destination.mkdir()
        marker = destination / "marker"
        marker.write_bytes(b"existing")
        replay = admitted((record,))
        with self.assertRaises(SourceAdmissionError):
            admit_source_bundle(replay, self.source, self.attempt, recorded(replay))
        self.assertEqual(marker.read_bytes(), b"existing")

    def test_publication_race_does_not_clobber_competitor(self) -> None:
        record = self.write("file", b"source")
        replay = admitted((record,))
        publish = source_admission._publish_without_overwrite

        def compete(source: Path, destination: Path) -> None:
            destination.mkdir()
            (destination / "competitor").write_bytes(b"keep")
            publish(source, destination)

        with mock.patch.object(
            source_admission, "_publish_without_overwrite", side_effect=compete
        ), self.assertRaises(SourceAdmissionError):
            admit_source_bundle(replay, self.source, self.attempt, recorded(replay))
        destination = self.attempt / "admitted-source"
        self.assertEqual((destination / "competitor").read_bytes(), b"keep")
        self.assertEqual(tuple(self.attempt.iterdir()), (destination,))

    def test_fsync_failure_after_publication_leaves_destination_for_quarantine(self) -> None:
        record = self.write("file", b"source")
        replay = admitted((record,))
        fsync_directory = source_admission._fsync_directory

        def fail_after_publish(path: Path) -> None:
            if path == self.attempt and (path / "admitted-source").exists():
                raise OSError("injected attempt fsync failure")
            fsync_directory(path)

        with mock.patch.object(
            source_admission, "_fsync_directory", side_effect=fail_after_publish
        ), self.assertRaises(SourceAdmissionError):
            admit_source_bundle(replay, self.source, self.attempt, recorded(replay))
        self.assertEqual(
            (self.attempt / "admitted-source/file").read_bytes(), b"source"
        )

    def test_unsafe_path_conflicts_fail_before_reads_or_temp_creation(self) -> None:
        manifests = (
            (SourceFile("A", "0" * 64), SourceFile("a", "0" * 64)),
            (SourceFile("A", "0" * 64), SourceFile("a/child", "0" * 64)),
            (SourceFile("trailing. ", "0" * 64),),
            (SourceFile("control\x7f", "0" * 64),),
            *((SourceFile(f"invalid{character}", "0" * 64),) for character in '<>"|?*'),
        )
        for records in manifests:
            with self.subTest(records=records):
                replay = admitted(records)
                with mock.patch.object(source_admission, "_read_relative") as read_relative:
                    with self.assertRaises(SourceAdmissionError):
                        admit_source_bundle(
                            replay, self.source, self.attempt, recorded(replay)
                        )
                    read_relative.assert_not_called()
                    self.assertEqual(tuple(self.attempt.iterdir()), ())

    def test_root_overlap_and_type_are_strict(self) -> None:
        record = self.write("file", b"source")
        replay = admitted((record,))
        invalid = (
            (str(self.source), self.attempt),
            (self.source / "file", self.attempt),
            (self.source, self.source),
            (self.source, self.source / "nested"),
            (self.source, self.base),
        )
        (self.source / "nested").mkdir()
        for source, attempt in invalid:
            with self.subTest(source=source, attempt=attempt):
                with self.assertRaises((TypeError, SourceAdmissionError)):
                    admit_source_bundle(  # type: ignore[arg-type]
                        replay, source, attempt, recorded(replay)
                    )

        linked = self.base / "linked-source"
        linked.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(SourceAdmissionError):
            admit_source_bundle(replay, linked, self.attempt, recorded(replay))

    def test_empty_manifest(self) -> None:
        replay = admitted(())
        report = admit_source_bundle(replay, self.source, self.attempt, recorded(replay))
        destination = self.attempt / "admitted-source"
        self.assertEqual(report.file_count, 0)
        self.assertEqual(report.staged_tree_sha256, digest(b""))
        self.assertEqual(source_tree_sha256(destination), digest(b""))
        self.assertEqual(tuple(destination.iterdir()), ())

    def test_source_tree_hash_rejects_links_and_nonregular_files(self) -> None:
        (self.source / "file").write_bytes(b"data")
        (self.source / "link").symlink_to("file")
        with self.assertRaises(SourceAdmissionError):
            source_tree_sha256(self.source)
        (self.source / "link").unlink()
        fifo = self.source / "fifo"
        os.mkfifo(fifo)
        with self.assertRaises(SourceAdmissionError):
            source_tree_sha256(self.source)

    def test_provisioned_manifests_admit_all_tracked_sources(self) -> None:
        expected = {
            340: (
                112,
                "2c63cbed0d2e9638b641a4207fd07b3b6c206595573a9b887a12a11e7eec5a84",
            ),
            430: (
                5,
                "697599a490eadb160becd5035613df65bcd1c6ea3e4c81fea2df60cea63ad656",
            ),
        }
        for target, (count, tree_sha256) in expected.items():
            with self.subTest(target=target):
                attempt = self.base / f"attempt-{target}"
                attempt.mkdir()
                records = load_manifest(target)
                replay = admitted(records)
                report = admit_source_bundle(replay, ROOT, attempt, recorded(replay))
                self.assertEqual(len(records), count)
                self.assertEqual(report.file_count, count)
                self.assertEqual(report.staged_tree_sha256, tree_sha256)
                self.assertEqual(
                    report.staged_tree_sha256,
                    source_tree_sha256(attempt / "admitted-source"),
                )

                attempt_v2 = self.base / f"attempt-v2-{target}"
                attempt_v2.mkdir()
                replay_v2 = admitted_v3(records)
                ledger = self.ledger_for(replay_v2, f"ledger-{target}.sqlite3")
                report_v2 = admit_source_bundle_v2(replay_v2, ROOT, attempt_v2, ledger)
                self.assertEqual(report_v2.file_count, count)
                self.assertEqual(report_v2.staged_tree_sha256, tree_sha256)
                verify_staged_source_v2(
                    report_v2,
                    replay_v2,
                    attempt_v2 / "admitted-source",
                    ledger,
                )

    def test_admission_source_has_no_target_specific_paths(self) -> None:
        source = (ROOT / "src/dfinsta_pipeline/source_admission.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("dfinsta_source_1.4.1", source)
        self.assertNotIn("dfinsta_source_430", source)


if __name__ == "__main__":
    unittest.main()

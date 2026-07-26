import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dfinsta_pipeline.source_admission as source_admission
from dfinsta_pipeline.contracts import ArtifactRef, GateDecision, canonical_json, canonical_sha256
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
    CapabilityBinding,
    ReplayRequest,
    ReplayRunSpecV1,
    SourceManifestV1,
    ToolchainProfile,
    admit_replay,
)
from dfinsta_pipeline.source_admission import (
    SourceAdmissionError,
    SourceAdmissionReport,
    admit_source_bundle,
    source_tree_sha256,
    verify_staged_source,
)
from dfinsta_pipeline.verifier import decoded_tree_sha256


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

    def test_portable_path_conflicts_fail_before_reads_or_temp_creation(self) -> None:
        manifests = (
            (SourceFile("A", "0" * 64), SourceFile("a", "0" * 64)),
            (SourceFile("A", "0" * 64), SourceFile("a/child", "0" * 64)),
            (SourceFile("CON.txt", "0" * 64),),
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
                "9265cf6be03890781bc2938796afa77098b3e116a8f9b631b4349bd14589a57f",
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

    def test_admission_source_has_no_target_specific_paths(self) -> None:
        source = (ROOT / "src/dfinsta_pipeline/source_admission.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("dfinsta_source_1.4.1", source)
        self.assertNotIn("dfinsta_source_430", source)


if __name__ == "__main__":
    unittest.main()

import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from dfinsta_pipeline.apply import apply_port
from dfinsta_pipeline.compiler import compile_port
from dfinsta_pipeline.port_contracts import (
    AppendManifestComponents,
    AppendResourceEntries,
    DeletePath,
    IntentSpecV2,
    OverlayTree,
    ReplaceResourceEntry,
    ResolutionSpecV3,
    SmaliEdit,
)


ROOT = Path(__file__).resolve().parents[1]
SPECS = ROOT / "tests" / "fixtures" / "phase_b"
DECODES = {
    340: ROOT / "work" / "1.4.1-reconstruction" / "stock-340",
    430: ROOT / "work" / "430-clean-build-v2" / "stock-430",
}
CLASS_RE = re.compile(r"^\.class\s+.*?(L[^\s]+;)$")


def load(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def descriptor(path: Path) -> str:
    matches = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = CLASS_RE.fullmatch(line.strip())
        if match:
            matches.append(match.group(1))
    if len(matches) != 1:
        raise AssertionError(f"Expected one descriptor in {path}")
    return matches[0]


def descriptor_path(decode: Path, wanted: str) -> Path:
    relative = wanted[1:-1]
    parent, basename = relative.rsplit("/", 1)
    candidates = sorted(decode.glob(f"smali*/{parent}/{basename}*.smali"))
    matches = [path for path in candidates if descriptor(path) == wanted]
    if len(matches) != 1:
        raise AssertionError(f"Descriptor {wanted} resolved to {len(matches)} files")
    return matches[0]


def copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


class ProvisionedApplyFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        intent = IntentSpecV2.from_dict(load(SPECS / "intent_v2.json"))
        cls.specs = {
            target: compile_port(
                intent,
                ResolutionSpecV3.from_dict(
                    load(SPECS / "resolutions" / f"instagram_{target}.json")
                ),
            )
            for target in DECODES
        }

    def apply_target(self, target: int, expected_count: int) -> None:
        decode = DECODES[target]
        if not decode.is_dir():
            self.skipTest(f"provisioned {target} clean decode is unavailable")
        spec = self.specs[target]
        self.assertEqual(len(spec.operations), expected_count)
        with tempfile.TemporaryDirectory(prefix=f"phase-b-apply-{target}-") as directory:
            work_tree = Path(directory) / "work"
            work_tree.mkdir()

            descriptors = {
                operation.descriptor
                for operation in spec.operations
                if isinstance(operation, SmaliEdit)
            }
            for wanted in sorted(descriptors):
                source = descriptor_path(decode, wanted)
                copy_file(source, work_tree / source.relative_to(decode))

            archive_paths = {
                operation.archive_path
                for operation in spec.operations
                if isinstance(
                    operation,
                    (
                        AppendResourceEntries,
                        ReplaceResourceEntry,
                        AppendManifestComponents,
                        DeletePath,
                    ),
                )
            }
            for relative in sorted(archive_paths):
                copy_file(decode / relative, work_tree / relative)

            for operation in spec.operations:
                if not isinstance(operation, OverlayTree):
                    continue
                for source_file in operation.source_files:
                    if not source_file.relative_path.endswith(".smali"):
                        continue
                    source = ROOT / operation.source_prefix / source_file.relative_path
                    wanted = descriptor(source)
                    relative = wanted[1:-1]
                    parent, basename = relative.rsplit("/", 1)
                    collisions = [
                        path
                        for path in decode.glob(f"smali*/{parent}/{basename}*.smali")
                        if descriptor(path) == wanted
                    ]
                    self.assertEqual(collisions, [], wanted)

            first = apply_port(spec, work_tree, ROOT)
            second = apply_port(spec, work_tree, ROOT)
            expected_ids = tuple(operation.operation_id for operation in spec.operations)
            self.assertEqual(tuple(result.operation_id for result in first.results), expected_ids)
            self.assertEqual(tuple(result.operation_id for result in second.results), expected_ids)
            self.assertEqual(len(first.results), expected_count)
            self.assertTrue(all(result.status == "applied" for result in first.results))
            self.assertTrue(all(result.status == "already_applied" for result in second.results))

    def test_apply_all_340_operations_to_provisioned_mini_tree(self) -> None:
        self.apply_target(340, 59)

    def test_apply_all_430_operations_to_provisioned_mini_tree(self) -> None:
        self.apply_target(430, 8)


if __name__ == "__main__":
    unittest.main()

import dataclasses
import inspect
import unittest

from dfinsta_pipeline.compiler import TargetPortSpec, compile_port
from dfinsta_pipeline.contracts import canonical_sha256
from dfinsta_pipeline.port_contracts import (
    DexEntrySetEquality,
    IntentSpecV2,
    OperationPostcondition,
    ResolutionSpecV2,
)
from tests.test_phase_b_contracts import intent_data, resolution_340, resolution_430


class PhaseBCompilerTests(unittest.TestCase):
    def compile(self, resolution_data: dict[str, object]) -> TargetPortSpec:
        return compile_port(
            IntentSpecV2.from_dict(intent_data()),
            ResolutionSpecV2.from_dict(resolution_data),
        )

    def test_compiles_full_rebuild_and_graft_without_target_branches(self) -> None:
        full = self.compile(resolution_340())
        graft = self.compile(resolution_430())

        self.assertEqual(full.backend.kind, "apktool_full_rebuild")
        self.assertEqual(graft.backend.kind, "stock_dex_graft")
        self.assertEqual(full.intent_sha256, graft.intent_sha256)
        source = inspect.getsource(__import__("dfinsta_pipeline.compiler", fromlist=["*"]))
        for target_literal in ("340", "430", "LX/", "clips/", "classes20"):
            self.assertNotIn(target_literal, source)

    def test_generates_operation_and_backend_proofs(self) -> None:
        compiled = self.compile(resolution_430())
        proofs = {
            assertion.operation_id: assertion
            for assertion in compiled.assertions
            if isinstance(assertion, OperationPostcondition)
        }
        self.assertEqual(set(proofs), {operation.operation_id for operation in compiled.operations})
        for operation in compiled.operations:
            self.assertEqual(
                proofs[operation.operation_id].operation_sha256,
                canonical_sha256(operation),
            )
        dex_proof = next(
            assertion
            for assertion in compiled.assertions
            if assertion.assertion_id == "backend.final-dex-entries"
        )
        self.assertIsInstance(dex_proof, DexEntrySetEquality)
        self.assertEqual(dex_proof.entries, compiled.backend.final_dex_entries)
        self.assertEqual(
            tuple(assertion.assertion_id for assertion in compiled.assertions),
            tuple(sorted(assertion.assertion_id for assertion in compiled.assertions)),
        )

    def test_compiled_hash_is_stable_and_contains_no_physical_paths(self) -> None:
        first = self.compile(resolution_340())
        second = self.compile(resolution_340())
        self.assertEqual(first, second)
        self.assertEqual(first.sha256, second.sha256)
        self.assertFalse(
            {"path", "workspace_path", "output_path"}
            & {field.name for field in dataclasses.fields(first)}
        )

    def test_rejects_intent_hash_mismatch(self) -> None:
        data = resolution_340()
        data["intent_sha256"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "not bound"):
            self.compile(data)

    def test_rejects_unknown_retired_disallowed_and_uncovered_intents(self) -> None:
        unknown = resolution_340()
        unknown["operations"][0]["intent_ids"] = ["unknown"]
        retired = resolution_340()
        retired["operations"][0]["intent_ids"] = ["retire-legacy"]
        disallowed = resolution_340()
        disallowed["operations"][0]["kind"] = "delete_path"
        disallowed["operations"][0] = {
            "operation_id": "delete-bin",
            "kind": "delete_path",
            "intent_ids": ["block-feed"],
            "archive_path": "assets/data.bin",
            "expected_present": True,
        }
        uncovered_intent = intent_data()
        uncovered_intent["hooks"].append(
            {
                "hook_id": "uncovered",
                "feature_id": "uncovered",
                "disposition": "retain",
                "description": "A retained hook without a target operation",
                "allowed_strategies": ["smali_edit"],
                "semantic_dependencies": [],
                "forbidden_fallbacks": [],
            }
        )

        with self.assertRaisesRegex(ValueError, "unknown intent"):
            self.compile(unknown)
        with self.assertRaisesRegex(ValueError, "retired intent"):
            self.compile(retired)
        with self.assertRaisesRegex(ValueError, "not allowed"):
            self.compile(disallowed)
        uncovered_resolution = resolution_340()
        uncovered_resolution["intent_sha256"] = IntentSpecV2.from_dict(uncovered_intent).sha256
        with self.assertRaisesRegex(ValueError, "no operation"):
            compile_port(
                IntentSpecV2.from_dict(uncovered_intent),
                ResolutionSpecV2.from_dict(uncovered_resolution),
            )

    def test_rejects_incomplete_graft_preservation(self) -> None:
        data = resolution_430()
        data["additional_assertions"][0]["exclusions"].remove("classes6.dex")
        with self.assertRaisesRegex(ValueError, "do not match"):
            self.compile(data)

        extra = resolution_430()
        extra["additional_assertions"][0]["exclusions"].append("classes2.dex")
        extra["additional_assertions"][0]["exclusions"].sort()
        with self.assertRaisesRegex(ValueError, "do not match"):
            self.compile(extra)

    def test_rejects_fixture_topology_disagreement(self) -> None:
        data = resolution_430()
        data["additional_assertions"][2]["entries"] = data["additional_assertions"][2][
            "entries"
        ][:-1]
        with self.assertRaisesRegex(ValueError, "topology"):
            self.compile(data)

        descriptor = resolution_430()
        descriptor["additional_assertions"][1]["dex_entry"] = "classes99.dex"
        with self.assertRaisesRegex(ValueError, "outside"):
            self.compile(descriptor)

        orphan = resolution_430()
        orphan["backend"]["add_dex_entries"].append("classes21.dex")
        orphan["additional_assertions"][0]["exclusions"].append("classes21.dex")
        orphan["additional_assertions"][0]["exclusions"].sort()
        orphan["additional_assertions"][2]["entries"].append("classes21.dex")
        with self.assertRaisesRegex(ValueError, "overlay producers"):
            self.compile(orphan)

    def test_rejects_fixture_supplied_operation_postcondition(self) -> None:
        data = resolution_340()
        data["additional_assertions"].append(
            {
                "assertion_id": "spoofed.final",
                "kind": "operation_postcondition",
                "operation_id": "edit-eight-endpoints",
                "operation_sha256": "f" * 64,
            }
        )
        with self.assertRaisesRegex(ValueError, "compiler-owned"):
            self.compile(data)

    def test_rejects_backend_incompatible_operations_and_destination_collisions(self) -> None:
        incompatible = resolution_430()
        incompatible["operations"].append(
            {
                "operation_id": "append-resource",
                "kind": "append_resource_entries",
                "intent_ids": ["block-feed"],
                "archive_path": "res/values/strings.xml",
                "entries": [
                    {
                        "resource_type": "string",
                        "name": "dfinsta_test",
                        "canonical_xml": "<string name=\"dfinsta_test\">Test</string>",
                    }
                ],
            }
        )
        with self.assertRaisesRegex(ValueError, "cannot apply"):
            self.compile(incompatible)

        collision = resolution_340()
        collision["operations"].append(dict(collision["operations"][1]))
        collision["operations"][-1]["operation_id"] = "duplicate-resource"
        with self.assertRaisesRegex(ValueError, "same destination"):
            self.compile(collision)

        delete_intent = intent_data()
        delete_intent["hooks"][0]["allowed_strategies"].insert(2, "delete_path")
        delete_resolution = resolution_340()
        delete_resolution["intent_sha256"] = IntentSpecV2.from_dict(delete_intent).sha256
        delete_resolution["operations"].append(
            {
                "operation_id": "delete-resource-file",
                "kind": "delete_path",
                "intent_ids": ["block-feed"],
                "archive_path": "res/values/strings.xml",
                "expected_present": True,
            }
        )
        with self.assertRaisesRegex(ValueError, "conflicts"):
            compile_port(
                IntentSpecV2.from_dict(delete_intent),
                ResolutionSpecV2.from_dict(delete_resolution),
            )

    def test_compiled_hash_changes_with_meaningful_input(self) -> None:
        first = self.compile(resolution_340())
        changed = resolution_340()
        changed["operations"][0]["payload"] = ["    const-string v0, \"different\""]
        second = self.compile(changed)
        self.assertNotEqual(first.sha256, second.sha256)


if __name__ == "__main__":
    unittest.main()

import dataclasses
import unittest
from dataclasses import asdict

from dfinsta_pipeline.contracts import canonical_sha256
from dfinsta_pipeline.port_contracts import (
    ArchiveEntryNamesAndBytesPreservedExcept,
    DescriptorsPresent,
    DexStringSubstringsAbsent,
    IntentResolution,
    IntentSpecV2,
    ResolutionSpecV2,
    ResolutionSpecV3,
    SourceFile,
)


def intent_data() -> dict[str, object]:
    return {
        "schema_version": 2,
        "policy_revision": "policy-2",
        "hooks": [
            {
                "hook_id": "block-feed",
                "feature_id": "feed",
                "disposition": "retain",
                "description": "Block feed requests before dispatch",
                "allowed_strategies": [
                    "append_manifest_components",
                    "append_resource_entries",
                    "overlay_tree",
                    "smali_edit",
                ],
                "semantic_dependencies": ["request-uri"],
                "forbidden_fallbacks": ["response-rewrite"],
            },
            {
                "hook_id": "retire-legacy",
                "feature_id": "legacy",
                "disposition": "retire",
                "description": "Retire the legacy response hook",
                "allowed_strategies": [],
                "semantic_dependencies": [],
                "forbidden_fallbacks": [],
            },
        ],
    }


def source_files() -> list[dict[str, str]]:
    return [
        {"relative_path": "com/dfinstagram/Preference.smali", "sha256": "c" * 64},
        {"relative_path": "com/dfinstagram/hooks.smali", "sha256": "d" * 64},
    ]


def manifest_sha() -> str:
    return canonical_sha256(tuple(SourceFile.from_dict(item) for item in source_files()))


def base_resolution(backend: dict[str, object], operations: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": 2,
        "intent_sha256": canonical_sha256(IntentSpecV2.from_dict(intent_data())),
        "target": {
            "package_name": "com.instagram.android",
            "version_name": "synthetic",
            "version_code": 1,
            "apk_sha256": "a" * 64,
            "composition": "monolithic",
        },
        "source_bundle_sha256": "b" * 64,
        "backend": backend,
        "operations": operations,
        "additional_assertions": [],
    }


def resolution_340() -> dict[str, object]:
    data = base_resolution(
        {
            "kind": "apktool_full_rebuild",
            "profile_id": "apktool-2.9.3-aapt1",
            "dex_entries": ["classes.dex", "classes2.dex", "classes3.dex"],
        },
        [
            {
                "operation_id": "edit-eight-endpoints",
                "kind": "smali_edit",
                "intent_ids": ["block-feed"],
                "descriptor": "LX/501;",
                "method_signature": "rewrite(Ljava/lang/String;)Ljava/lang/String;",
                "mode": "replace",
                "match_policy": "all",
                "occurrence": None,
                "precondition_sequence": ["    const-string v0, \"stock_endpoint\""],
                "expected_precondition_count": 8,
                "payload": ["    const-string v0, \"blocked_endpoint\""],
                "final_sequence": ["    const-string v0, \"blocked_endpoint\""],
                "expected_final_count": 8,
            },
            {
                "operation_id": "append-settings-resources",
                "kind": "append_resource_entries",
                "intent_ids": ["block-feed"],
                "archive_path": "res/values/strings.xml",
                "entries": [
                    {
                        "resource_type": "string",
                        "name": "dfinsta_feed",
                        "canonical_xml": "<string name=\"dfinsta_feed\">Feed</string>",
                    }
                ],
            },
            {
                "operation_id": "append-settings-activity",
                "kind": "append_manifest_components",
                "intent_ids": ["block-feed"],
                "archive_path": "AndroidManifest.xml",
                "components": [
                    {
                        "tag": "activity",
                        "android_name": "com.dfinstagram.Preference",
                        "canonical_xml": "<activity android:name=\"com.dfinstagram.Preference\" />",
                    }
                ],
            },
        ],
    )
    return data


def resolution_430() -> dict[str, object]:
    stock = ["classes.dex", *(f"classes{number}.dex" for number in range(2, 20))]
    data = base_resolution(
        {
            "kind": "stock_dex_graft",
            "profile_id": "stock-graft-v1",
            "stock_dex_entries": stock,
            "replace_dex_entries": ["classes.dex", "classes3.dex", "classes4.dex", "classes6.dex"],
            "add_dex_entries": ["classes20.dex"],
        },
        [
            {
                "operation_id": "edit-profile-menu",
                "kind": "smali_edit",
                "intent_ids": ["block-feed"],
                "descriptor": "LX/077K;",
                "method_signature": "A00(Landroid/content/Context;Lcom/instagram/common/session/UserSession;LX/077F;LX/0JxZ;)Landroid/widget/ImageView;",
                "mode": "insert_before",
                "match_policy": "occurrence",
                "occurrence": 0,
                "precondition_sequence": ["    return v0"],
                "expected_precondition_count": 1,
                "payload": ["    invoke-static {}, Lcom/dfinstagram/Settings;->open()V"],
                "final_sequence": ["    invoke-static {}, Lcom/dfinstagram/Settings;->open()V"],
                "expected_final_count": 1,
            },
            {
                "operation_id": "overlay-custom-code",
                "kind": "overlay_tree",
                "intent_ids": ["block-feed"],
                "source_prefix": "custom/smali",
                "target_prefix": "smali_classes20",
                "source_files": source_files(),
                "source_manifest_sha256": manifest_sha(),
                "collision_policy": "forbid",
            },
        ],
    )
    data["additional_assertions"] = [
        {
            "assertion_id": "archive-preserved",
            "kind": "archive_preservation_except",
            "exclusions": [
                "META-INF/MANIFEST.MF",
                "classes.dex",
                "classes20.dex",
                "classes3.dex",
                "classes4.dex",
                "classes6.dex",
            ],
        },
        {
            "assertion_id": "archive-signatures-absent",
            "kind": "archive_entries_absent",
            "entries": ["META-INF/MANIFEST.MF"],
        },
        {
            "assertion_id": "descriptor-set",
            "kind": "descriptor_set_equality",
            "dex_entry": "classes20.dex",
            "descriptors": ["Lcom/dfinstagram/Preference;", "Lcom/dfinstagram/hooks;"],
        },
        {
            "assertion_id": "dex-set",
            "kind": "dex_entry_set_equality",
            "entries": [*stock, "classes20.dex"],
        },
    ]
    return data


def target_intent_data() -> dict[str, object]:
    data = intent_data()
    data["hooks"].append(
        {
            "hook_id": "settings-ui",
            "feature_id": "settings",
            "disposition": "retain",
            "description": "Expose target settings controls",
            "allowed_strategies": [
                "append_manifest_components",
                "append_resource_entries",
                "overlay_tree",
                "smali_edit",
            ],
            "semantic_dependencies": [],
            "forbidden_fallbacks": [],
        },
    )
    return data


def resolution_v3_340() -> dict[str, object]:
    data = resolution_340()
    data["schema_version"] = 3
    data["intent_sha256"] = IntentSpecV2.from_dict(target_intent_data()).sha256
    data["intent_statuses"] = [
        {"intent_id": "block-feed", "status": "implemented", "rationale": None},
        {
            "intent_id": "retire-legacy",
            "status": "omitted",
            "rationale": "Globally retired intent",
        },
        {"intent_id": "settings-ui", "status": "implemented", "rationale": None},
    ]
    for operation in data["operations"][1:]:
        operation["intent_ids"] = ["settings-ui"]
    return data


def resolution_v3_430() -> dict[str, object]:
    data = resolution_430()
    data["schema_version"] = 3
    data["intent_sha256"] = IntentSpecV2.from_dict(target_intent_data()).sha256
    data["intent_statuses"] = [
        {
            "intent_id": "block-feed",
            "status": "omitted",
            "rationale": "The 430 target does not implement feed blocking yet",
        },
        {
            "intent_id": "retire-legacy",
            "status": "omitted",
            "rationale": "Globally retired intent",
        },
        {"intent_id": "settings-ui", "status": "implemented", "rationale": None},
    ]
    for operation in data["operations"]:
        operation["intent_ids"] = ["settings-ui"]
    return data


class PhaseBContractTests(unittest.TestCase):
    def test_closed_presence_and_dex_substring_assertions_are_strict(self) -> None:
        descriptors = DescriptorsPresent.from_dict(
            {
                "assertion_id": "required-descriptors",
                "kind": "descriptors_present",
                "dex_entry": "classes2.dex",
                "descriptors": ["Lsample/Required;"],
            }
        )
        self.assertEqual(descriptors.descriptors, ("Lsample/Required;",))
        substrings = DexStringSubstringsAbsent.from_dict(
            {
                "assertion_id": "forbidden-substrings",
                "kind": "dex_string_substrings_absent",
                "dex_entry": "classes2.dex",
                "substrings": ["forbidden", "legacy"],
            }
        )
        self.assertEqual(substrings.substrings, ("forbidden", "legacy"))

        for values in ([], ["legacy", "legacy"], ["legacy", "forbidden"]):
            data = {
                "assertion_id": "forbidden-substrings",
                "kind": "dex_string_substrings_absent",
                "dex_entry": "classes2.dex",
                "substrings": values,
            }
            with self.subTest(values=values), self.assertRaises(ValueError):
                DexStringSubstringsAbsent.from_dict(data)

    def test_v3_status_round_trip_and_target_fixtures(self) -> None:
        for data in (resolution_v3_340(), resolution_v3_430()):
            resolution = ResolutionSpecV3.from_dict(data)
            self.assertEqual(ResolutionSpecV3.from_dict(asdict(resolution)), resolution)
            self.assertEqual(resolution.sha256, canonical_sha256(asdict(resolution)))
            self.assertEqual(
                tuple(status.intent_id for status in resolution.intent_statuses),
                tuple(sorted(status.intent_id for status in resolution.intent_statuses)),
            )
        self.assertEqual(
            ResolutionSpecV3.from_dict(resolution_v3_430()).intent_statuses[0].status,
            "omitted",
        )

    def test_v3_status_rationale_and_order_are_strict(self) -> None:
        invalid_statuses = [
            {"intent_id": "block-feed", "status": "unknown", "rationale": None},
            {"intent_id": "block-feed", "status": "implemented", "rationale": "because"},
            {"intent_id": "block-feed", "status": "omitted", "rationale": None},
            {"intent_id": "block-feed", "status": "omitted", "rationale": "   "},
            {"intent_id": "block-feed", "status": "omitted", "rationale": "x" * 2049},
        ]
        for status in invalid_statuses:
            with self.subTest(status=status["status"], rationale=status["rationale"]), self.assertRaises(
                (TypeError, ValueError)
            ):
                IntentResolution.from_dict(status)

        unordered = resolution_v3_340()
        unordered["intent_statuses"] = list(reversed(unordered["intent_statuses"]))
        duplicate = resolution_v3_340()
        duplicate["intent_statuses"].insert(1, dict(duplicate["intent_statuses"][0]))
        for data in (unordered, duplicate):
            with self.assertRaises(ValueError):
                ResolutionSpecV3.from_dict(data)

        accepted = IntentResolution.from_dict(
            {"intent_id": "block-feed", "status": "omitted", "rationale": "x" * 2048}
        )
        self.assertEqual(len(accepted.rationale), 2048)

    def test_v3_status_changes_resolution_hash(self) -> None:
        first = ResolutionSpecV3.from_dict(resolution_v3_430())
        changed = resolution_v3_430()
        changed["intent_statuses"][0]["rationale"] = "Deferred on this target"
        second = ResolutionSpecV3.from_dict(changed)
        self.assertNotEqual(first.sha256, second.sha256)

    def test_representative_340_full_rebuild(self) -> None:
        resolution = ResolutionSpecV2.from_dict(resolution_340())
        edit = resolution.operations[0]
        self.assertEqual(edit.match_policy, "all")
        self.assertIsNone(edit.occurrence)
        self.assertEqual(edit.expected_precondition_count, 8)
        self.assertEqual(edit.expected_final_count, 8)
        self.assertEqual(resolution.backend.final_dex_entries, resolution.backend.dex_entries)
        self.assertEqual(resolution.operations[1].entries[0].identity, ("string", "dfinsta_feed"))
        self.assertEqual(resolution.operations[2].components[0].identity, ("activity", "com.dfinstagram.Preference"))

    def test_real_shaped_430_graft_and_preservation(self) -> None:
        resolution = ResolutionSpecV2.from_dict(resolution_430())
        self.assertEqual(len(resolution.backend.stock_dex_entries), 19)
        self.assertEqual(
            resolution.backend.replace_dex_entries,
            ("classes.dex", "classes3.dex", "classes4.dex", "classes6.dex"),
        )
        self.assertEqual(len(resolution.backend.final_dex_entries), 20)
        self.assertEqual(resolution.backend.final_dex_entries[-1], "classes20.dex")
        self.assertEqual(resolution.backend.final_dex_entries, resolution.additional_assertions[3].entries)
        edit = resolution.operations[0]
        self.assertEqual((edit.match_policy, edit.occurrence), ("occurrence", 0))
        overlay = resolution.operations[1]
        self.assertEqual(overlay.exact_target_files, overlay.source_files)
        self.assertEqual(overlay.source_manifest_sha256, canonical_sha256(overlay.source_files))
        preservation = resolution.additional_assertions[0]
        self.assertIsInstance(preservation, ArchiveEntryNamesAndBytesPreservedExcept)
        self.assertIn("NamesAndBytesPreserved", type(preservation).__name__)
        self.assertNotIn("flags", {field.name for field in dataclasses.fields(preservation)})

    def test_canonical_round_trip_and_hash_stability(self) -> None:
        intent = IntentSpecV2.from_dict(intent_data())
        for data in (resolution_340(), resolution_430()):
            resolution = ResolutionSpecV2.from_dict(data)
            self.assertEqual(ResolutionSpecV2.from_dict(asdict(resolution)), resolution)
            self.assertEqual(resolution.sha256, canonical_sha256(asdict(resolution)))
        self.assertEqual(IntentSpecV2.from_dict(asdict(intent)), intent)
        self.assertEqual(intent.sha256, canonical_sha256(asdict(intent)))

    def test_retire_allows_no_strategy_but_retain_does_not(self) -> None:
        self.assertEqual(IntentSpecV2.from_dict(intent_data()).hooks[1].allowed_strategies, ())
        invalid = intent_data()
        invalid["hooks"][0]["allowed_strategies"] = []
        with self.assertRaises(ValueError):
            IntentSpecV2.from_dict(invalid)

    def test_set_values_require_canonical_sorted_unique_order(self) -> None:
        mutations = []
        hooks = intent_data()
        hooks["hooks"] = list(reversed(hooks["hooks"]))
        mutations.append((IntentSpecV2, hooks))
        strategies = intent_data()
        strategies["hooks"][0]["allowed_strategies"] = ["smali_edit", "append_resource_entries"]
        mutations.append((IntentSpecV2, strategies))
        intents = resolution_340()
        intents["operations"][0]["intent_ids"] = ["retire-legacy", "block-feed"]
        mutations.append((ResolutionSpecV2, intents))
        dex = resolution_430()
        dex["backend"]["stock_dex_entries"] = ["classes.dex", "classes10.dex", "classes2.dex"]
        mutations.append((ResolutionSpecV2, dex))
        assertions = resolution_430()
        assertions["additional_assertions"] = list(reversed(assertions["additional_assertions"]))
        mutations.append((ResolutionSpecV2, assertions))
        source = resolution_430()
        source["operations"][1]["source_files"] = list(reversed(source_files()))
        mutations.append((ResolutionSpecV2, source))
        resources = resolution_340()
        resources["operations"][1]["entries"] = [
            {
                "resource_type": "string",
                "name": "z_last",
                "canonical_xml": "<string name=\"z_last\">Z</string>",
            },
            *resources["operations"][1]["entries"],
        ]
        mutations.append((ResolutionSpecV2, resources))
        components = resolution_340()
        components["operations"][2]["components"] = [
            {
                "tag": "service",
                "android_name": "com.dfinstagram.Service",
                "canonical_xml": "<service android:name=\"com.dfinstagram.Service\" />",
            },
            *components["operations"][2]["components"],
        ]
        mutations.append((ResolutionSpecV2, components))
        exclusions = resolution_430()
        exclusions["additional_assertions"][0]["exclusions"] = list(
            reversed(exclusions["additional_assertions"][0]["exclusions"])
        )
        mutations.append((ResolutionSpecV2, exclusions))
        descriptors = resolution_430()
        descriptors["additional_assertions"][2]["descriptors"] = list(
            reversed(descriptors["additional_assertions"][2]["descriptors"])
        )
        mutations.append((ResolutionSpecV2, descriptors))
        for decoder, data in mutations:
            with self.subTest(decoder=decoder.__name__), self.assertRaises(ValueError):
                decoder.from_dict(data)

    def test_smali_match_policy_invariants(self) -> None:
        all_with_index = resolution_340()
        all_with_index["operations"][0]["occurrence"] = 0
        indexed_without_index = resolution_430()
        indexed_without_index["operations"][0]["occurrence"] = None
        nullable_method = resolution_430()
        nullable_method["operations"][0]["method_signature"] = None
        boolean_index = resolution_430()
        boolean_index["operations"][0]["occurrence"] = False
        for data in (all_with_index, indexed_without_index, nullable_method, boolean_index):
            with self.assertRaises((TypeError, ValueError)):
                ResolutionSpecV2.from_dict(data)

    def test_closed_resource_operations_and_exact_identities(self) -> None:
        replacement = resolution_340()
        replacement["operations"] = [
            {
                "operation_id": "replace-resource",
                "kind": "replace_resource_entry",
                "intent_ids": ["block-feed"],
                "archive_path": "res/values/strings.xml",
                "before": {
                    "resource_type": "string",
                    "name": "dfinsta_feed",
                    "canonical_xml": "<string name=\"dfinsta_feed\">Old</string>",
                },
                "after": {
                    "resource_type": "string",
                    "name": "dfinsta_feed",
                    "canonical_xml": "<string name=\"dfinsta_feed\">New</string>",
                },
            },
            {
                "operation_id": "delete-bin",
                "kind": "delete_path",
                "intent_ids": ["block-feed"],
                "archive_path": "assets/drawables.bin",
                "expected_present": True,
            },
        ]
        parsed = ResolutionSpecV2.from_dict(replacement)
        self.assertEqual(parsed.operations[0].before.identity, parsed.operations[0].after.identity)
        self.assertTrue(parsed.operations[1].expected_present)

        changed_identity = replacement
        changed_identity["operations"][0]["after"]["name"] = "other"
        with self.assertRaises(ValueError):
            ResolutionSpecV2.from_dict(changed_identity)

    def test_strict_malformed_nested_data(self) -> None:
        cases = []
        unknown = resolution_430()
        unknown["operations"][0]["extra"] = True
        cases.append(unknown)
        missing = resolution_430()
        del missing["operations"][1]["source_files"][0]["sha256"]
        cases.append(missing)
        string_array = resolution_430()
        string_array["operations"][0]["intent_ids"] = "block-feed"
        cases.append(string_array)
        bad_sha = resolution_430()
        bad_sha["operations"][1]["source_manifest_sha256"] = "A" * 64
        cases.append(bad_sha)
        manifest_mismatch = resolution_430()
        manifest_mismatch["operations"][1]["source_manifest_sha256"] = "e" * 64
        cases.append(manifest_mismatch)
        duplicate_source = resolution_430()
        duplicate_source["operations"][1]["source_files"].append(
            dict(duplicate_source["operations"][1]["source_files"][0])
        )
        cases.append(duplicate_source)
        unsafe = resolution_430()
        unsafe["operations"][1]["source_files"][0]["relative_path"] = "../escape.smali"
        cases.append(unsafe)
        empty_operations = resolution_430()
        empty_operations["operations"] = []
        cases.append(empty_operations)
        collision = resolution_430()
        collision["backend"]["add_dex_entries"] = ["classes19.dex"]
        cases.append(collision)
        for index, data in enumerate(cases):
            with self.subTest(index=index), self.assertRaises((TypeError, ValueError)):
                ResolutionSpecV2.from_dict(data)

    def test_smali_contract_has_no_decode_path_or_dex_placement(self) -> None:
        resolution = ResolutionSpecV2.from_dict(resolution_430())
        edit = resolution.operations[0]
        names = {field.name for field in dataclasses.fields(edit)}
        self.assertFalse({"archive_path", "path", "dex_entry", "dex_index"} & names)
        self.assertEqual(edit.intent_ids, ("block-feed",))


if __name__ == "__main__":
    unittest.main()

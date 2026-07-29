import dataclasses
import unittest
from dataclasses import asdict

from dfinsta_pipeline.contracts import ArtifactRef, canonical_sha256
from dfinsta_pipeline.replay_contracts import (
    ReplayBackendCompositionV1,
    ReplayPatchedApkReceiptV1,
)


def ref(
    kind: str,
    digest: str,
    producer: str,
    inputs: tuple[str, ...] = (),
) -> ArtifactRef:
    return ArtifactRef(
        1,
        kind,
        digest,
        1,
        f"cas://sha256/{digest}",
        producer,
        inputs,
    )


def composition(kind: str = "apktool_full_rebuild") -> ReplayBackendCompositionV1:
    intermediate = "b" * 64
    return ReplayBackendCompositionV1(
        1,
        kind,
        "synthetic-full",
        "1" * 64,
        "a" * 64,
        intermediate,
        intermediate if kind == "apktool_full_rebuild" else "c" * 64,
        ("classes.dex", "classes2.dex"),
        () if kind == "apktool_full_rebuild" else ("classes.dex",),
        () if kind == "apktool_full_rebuild" else ("classes2.dex",),
        0 if kind == "apktool_full_rebuild" else 3,
        () if kind == "apktool_full_rebuild" else ("META-INF/CERT.RSA",),
        True,
    )


def receipt(*, with_framework: bool = False) -> ReplayPatchedApkReceiptV1:
    apply_key = "2" * 64
    build_key = "3" * 64
    completed_apply = ref("replay-patched-tree-receipt-v1", "4" * 64, apply_key)
    patched_tree = ref("decoded-tree-manifest-v1", "5" * 64, apply_key)
    stock = ref("stock-apk", "a" * 64, "stock-producer")
    framework_receipt = None
    framework_manifest = None
    framework_semantic = None
    framework_hashes: tuple[str, ...] = ()
    if with_framework:
        framework_key = "6" * 64
        framework_receipt = ref(
            "replay-framework-cache-receipt-v1", "7" * 64, framework_key
        )
        framework_manifest = ref(
            "decoded-tree-manifest-v1", "8" * 64, framework_key
        )
        framework_semantic = "9" * 64
        framework_hashes = (
            canonical_sha256(framework_receipt),
            canonical_sha256(framework_manifest),
            framework_semantic,
        )
    execution_inputs = (
        "0" * 64,
        canonical_sha256(completed_apply),
        canonical_sha256(patched_tree),
        "d" * 64,
        "e" * 64,
        canonical_sha256(stock),
        "f" * 64,
        "1" * 64,
        "2" * 64,
        "3" * 64,
        "4" * 64,
        *framework_hashes,
    )
    backend = composition()
    intermediate = ref("intermediate-apk", "b" * 64, build_key, execution_inputs)
    patched_inputs = (
        *execution_inputs,
        canonical_sha256(intermediate),
        backend.sha256,
    )
    patched = ref("final-apk", "b" * 64, build_key, patched_inputs)
    return ReplayPatchedApkReceiptV1(
        1,
        "0" * 64,
        completed_apply,
        patched_tree,
        "d" * 64,
        "e" * 64,
        stock,
        "synthetic-full",
        "f" * 64,
        "build",
        "1" * 64,
        "2" * 64,
        "3" * 64,
        "4" * 64,
        framework_receipt,
        framework_manifest,
        framework_semantic,
        intermediate,
        backend,
        patched,
        build_key,
        True,
    )


class ReplayBackendCompositionContractTests(unittest.TestCase):
    def test_canonical_roundtrip_and_hash(self) -> None:
        value = composition()
        self.assertEqual(ReplayBackendCompositionV1.from_dict(asdict(value)), value)
        self.assertEqual(value.sha256, canonical_sha256(value))
        self.assertTrue(value.__dataclass_params__.frozen)
        self.assertEqual(value.__slots__, tuple(field.name for field in dataclasses.fields(value)))

    def test_strict_fields_types_and_collections(self) -> None:
        value = asdict(composition("stock_dex_graft"))
        mutations = (
            {key: item for key, item in value.items() if key != "passed"},
            {**value, "unknown": 1},
            {**value, "schema_version": True},
            {**value, "retained_entry_count": False},
            {**value, "passed": 1},
            {**value, "final_dex_entries": ["classes2.dex", "classes.dex"]},
            {**value, "final_dex_entries": ["classes.dex", "classes.dex"]},
            {**value, "replaced_entries": ["classes2.dex", "classes.dex"]},
            {**value, "added_entries": ["classes2.dex", "classes2.dex"]},
            {
                **value,
                "stripped_signature_entries": ["META-INF/Z.RSA", "META-INF/Z.RSA"],
            },
            {**value, "stripped_signature_entries": ["res/raw/value"]},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises((TypeError, ValueError)):
                ReplayBackendCompositionV1.from_dict(mutation)

    def test_backend_relational_invariants(self) -> None:
        full = asdict(composition())
        graft = asdict(composition("stock_dex_graft"))
        numeric_order = {
            **full,
            "final_dex_entries": ["classes.dex", "classes2.dex", "classes10.dex"],
        }
        self.assertEqual(
            ReplayBackendCompositionV1.from_dict(numeric_order).final_dex_entries,
            ("classes.dex", "classes2.dex", "classes10.dex"),
        )
        mutations = (
            {**full, "output_sha256": "c" * 64},
            {**full, "replaced_entries": ["classes.dex"]},
            {**full, "added_entries": ["classes2.dex"]},
            {**full, "retained_entry_count": 1},
            {**full, "stripped_signature_entries": ["META-INF/CERT.RSA"]},
            {**graft, "replaced_entries": ["classes3.dex"]},
            {**graft, "added_entries": ["classes.dex"]},
            {**graft, "replaced_entries": ["classes.dex"], "added_entries": ["classes.dex"]},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                ReplayBackendCompositionV1.from_dict(mutation)

    def test_signature_entries_preserve_stock_archive_order(self) -> None:
        value = dataclasses.replace(
            composition("stock_dex_graft"),
            stripped_signature_entries=(
                "META-INF/MANIFEST.MF",
                "meta-inf/CERT.sf",
                "META-INF/SIG-CUSTOM",
            ),
        )
        self.assertEqual(
            ReplayBackendCompositionV1.from_dict(asdict(value)),
            value,
        )


class ReplayPatchedApkReceiptContractTests(unittest.TestCase):
    def test_canonical_roundtrip_hash_and_lineage(self) -> None:
        value = receipt(with_framework=True)
        self.assertEqual(ReplayPatchedApkReceiptV1.from_dict(asdict(value)), value)
        self.assertEqual(value.sha256, canonical_sha256(value))
        self.assertEqual(value.intermediate_apk.input_hashes, value.execution_input_hashes)
        self.assertEqual(value.patched_apk.input_hashes, value.patched_apk_input_hashes)
        self.assertEqual(
            value.receipt_input_hashes,
            (*value.patched_apk_input_hashes, canonical_sha256(value.patched_apk)),
        )

    def test_strict_fields_operation_and_success(self) -> None:
        value = asdict(receipt())
        mutations = (
            {key: item for key, item in value.items() if key != "success"},
            {**value, "unknown": 1},
            {**value, "schema_version": True},
            {**value, "role": "decode"},
            {**value, "operation_key": "A" * 64},
            {**value, "success": 1},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises((TypeError, ValueError)):
                ReplayPatchedApkReceiptV1.from_dict(mutation)

    def test_framework_fields_are_all_or_none(self) -> None:
        absent = asdict(receipt())
        present = asdict(receipt(with_framework=True))
        mutations = (
            {**absent, "framework_cache_semantic_sha256": "9" * 64},
            {**present, "completed_framework_cache_receipt": None},
            {**present, "framework_cache_manifest": None},
            {**present, "framework_cache_semantic_sha256": None},
            {
                **present,
                "framework_cache_manifest": {
                    **present["framework_cache_manifest"],
                    "producer_operation_id": "a" * 64,
                },
            },
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises((TypeError, ValueError)):
                ReplayPatchedApkReceiptV1.from_dict(mutation)

    def test_kinds_producers_lineage_and_report_bindings(self) -> None:
        value = asdict(receipt())
        mutations = []
        for field, kind in (
            ("completed_patched_tree_receipt", "other"),
            ("patched_tree_manifest", "other"),
            ("stock_apk", "final-apk"),
            ("intermediate_apk", "final-apk"),
            ("patched_apk", "intermediate-apk"),
        ):
            mutations.append({**value, field: {**value[field], "kind": kind}})
        mutations.extend(
            (
                {
                    **value,
                    "patched_tree_manifest": {
                        **value["patched_tree_manifest"],
                        "producer_operation_id": "a" * 64,
                    },
                },
                {
                    **value,
                    "intermediate_apk": {
                        **value["intermediate_apk"],
                        "producer_operation_id": "a" * 64,
                    },
                },
                {
                    **value,
                    "patched_apk": {
                        **value["patched_apk"],
                        "producer_operation_id": "a" * 64,
                    },
                },
                {
                    **value,
                    "intermediate_apk": {
                        **value["intermediate_apk"],
                        "input_hashes": value["intermediate_apk"]["input_hashes"][:-1],
                    },
                },
                {
                    **value,
                    "patched_apk": {
                        **value["patched_apk"],
                        "input_hashes": value["patched_apk"]["input_hashes"][:-1],
                    },
                },
                {
                    **value,
                    "composition": {**value["composition"], "stock_sha256": "0" * 64},
                },
                {
                    **value,
                    "composition": {
                        **value["composition"],
                        "intermediate_sha256": "0" * 64,
                        "output_sha256": "0" * 64,
                    },
                },
                {
                    **value,
                    "composition": {**value["composition"], "output_sha256": "0" * 64},
                },
                {
                    **value,
                    "composition": {
                        **value["composition"],
                        "backend_profile_id": "different-profile",
                    },
                },
            )
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises((TypeError, ValueError)):
                ReplayPatchedApkReceiptV1.from_dict(mutation)


if __name__ == "__main__":
    unittest.main()

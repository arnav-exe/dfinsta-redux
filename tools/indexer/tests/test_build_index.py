"""Tests for the stage-2 per-version indexer.

The fixture is a synthetic decode built in a temp dir: three smali files across
three ``smali_classesN`` trees plus a tiny ``res/values/public.xml``.  The real
1.7 GB 439 decode is deliberately NOT a dependency of any test here -- it is
used only for the manual timing/sanity run documented in the module docstring
of ``build_index.py``.

Every check that can silently pass has a negative twin: a stale index that must
be reported stale, literals that must NOT be treated as API paths, and a
same-length content edit that a size-based hash would miss.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import build_index  # noqa: E402
from build_index import (  # noqa: E402
    API_SURFACE_FILENAME,
    HEADER_FILENAME,
    STRUCTURAL_FILENAME,
    build_index as run_build,
    check_index,
    is_obfuscated,
    is_smali_tree,
    list_smali_trees,
    looks_like_api_path,
    parse_smali,
    smali_tree_sort_key,
)


TIGON = """\
.class public final Lcom/instagram/api/tigon/TigonServiceLayer;
.super Ljava/lang/Object;
.source "TigonServiceLayer.java"

# interfaces
.implements Lcom/instagram/common/api/base/ServiceLayer;
.implements LX/0Aa1;


# static fields
.field public static final ENDPOINT:Ljava/lang/String; = "feed/timeline/"


# direct methods
.method public constructor <init>(LX/0Aa1;)V
    .locals 0

    const-string v0, "discover/topical_explore/"

    return-void
.end method

.method public startRequest(Lcom/facebook/tigon/TigonRequest;)V
    .locals 2

    const-string/jumbo v1, "video/mp4"

    const-string v1, "getDevServerName()Ljava/lang/String;"

    const-string v1, "Ljava/lang/String;"

    const-string v1, "com/instagram/api/tigon/TigonServiceLayer"

    return-void
.end method
"""

CLIPS_HOST = """\
.class public final LX/04tC;
.super Ljava/lang/Object;
.source ""


# direct methods
.method public static final A00(LX/0Ciw;Ljava/lang/String;)LX/02VJ;
    .locals 14

    const-string v8, "clips/discover/"

    const-string/jumbo v9, "clips/discover/stream/"

    const-string v10, "video/refresh_resources/%s/"

    return-object v0
.end method

.method public A01()V
    .locals 0

    return-void
.end method
"""

OTHER_HOST = """\
.class public LX/0Di2;
.super LX/04tC;

.implements Ljava/lang/Runnable;

.method public run()V
    .locals 1

    const-string v0, "clips/discover/"

    const-string v0, "n/a"

    const-string v0, "text_feed/{post_id}/replies_in_ig/"

    return-void
.end method
"""

# Same shape, but every directive is indented -- exercises the tolerant
# fallback parse rather than the column-0 fast path.
INDENTED = """\
    .class public LX/0Weird;
    .super Ljava/lang/Object;

    .method public go()V
        const-string v0, "friendships/show/"
        return-void
    .end method
"""

PUBLIC_XML = """\
<?xml version="1.0" encoding="utf-8"?>
<resources>
    <public type="anim" name="bounce" id="0x7f010000" />
    <public type="drawable" name="instagram_arrow_back_24" id="0x7f080001" />
    <public type="drawable" name="instagram_reels_outline_24" id="0x7f080002" />
    <public type="id" name="action_bar_textview_title" id="0x7f0b0001" />
    <public type="layout" name="action_bar_title" id="0x7f0e0001" />
    <public type="string" name="only_one_of_many_strings" id="0x7f120001" />
</resources>
"""


def make_decode(root: Path) -> Path:
    """Build a synthetic decode: 3 smali trees + res/values/public.xml."""
    decode = root / "stock-test"
    files = {
        "smali/com/instagram/api/tigon/TigonServiceLayer.smali": TIGON,
        "smali_classes2/X/04tC.smali": CLIPS_HOST,
        "smali_classes10/X/0Di2.smali": OTHER_HOST,
        "res/values/public.xml": PUBLIC_XML,
    }
    for relpath, text in files.items():
        target = decode / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return decode


def read_rows(out_dir: Path) -> tuple[dict, list[dict]]:
    lines = (out_dir / STRUCTURAL_FILENAME).read_text(encoding="utf-8").splitlines()
    return json.loads(lines[0]), [json.loads(line) for line in lines[1:]]


def read_api_surface(out_dir: Path) -> dict:
    return json.loads((out_dir / API_SURFACE_FILENAME).read_text(encoding="utf-8"))


class IndexerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.decode = make_decode(self.root)
        self.out = self.root / "index"
        self.addCleanup(self._tmp.cleanup)

    def build(self, **kwargs) -> dict:
        return run_build(self.decode, self.out, **kwargs)


class TestSmaliParsing(IndexerTestCase):
    def test_descriptor_super_interfaces_methods(self) -> None:
        parsed = parse_smali(TIGON.encode("utf-8"))
        self.assertEqual(parsed["descriptor"], "Lcom/instagram/api/tigon/TigonServiceLayer;")
        self.assertEqual(parsed["super"], "Ljava/lang/Object;")
        self.assertEqual(
            parsed["interfaces"],
            ["Lcom/instagram/common/api/base/ServiceLayer;", "LX/0Aa1;"],
        )
        self.assertEqual(
            parsed["methods"],
            ["<init>(LX/0Aa1;)V", "startRequest(Lcom/facebook/tigon/TigonRequest;)V"],
        )

    def test_class_with_no_interfaces_has_empty_list(self) -> None:
        parsed = parse_smali(CLIPS_HOST.encode("utf-8"))
        self.assertEqual(parsed["interfaces"], [])
        self.assertEqual(parsed["methods"], ["A00(LX/0Ciw;Ljava/lang/String;)LX/02VJ;", "A01()V"])

    def test_non_object_super_is_recorded(self) -> None:
        parsed = parse_smali(OTHER_HOST.encode("utf-8"))
        self.assertEqual(parsed["super"], "LX/04tC;")
        self.assertEqual(parsed["interfaces"], ["Ljava/lang/Runnable;"])

    def test_indented_directives_fall_back_to_tolerant_parse(self) -> None:
        parsed = parse_smali(INDENTED.encode("utf-8"))
        self.assertEqual(parsed["descriptor"], "LX/0Weird;")
        self.assertEqual(parsed["super"], "Ljava/lang/Object;")
        self.assertEqual(parsed["methods"], ["go()V"])
        self.assertIn("friendships/show/", parsed["api_paths"])

    def test_file_without_class_directive_yields_none(self) -> None:
        parsed = parse_smali(b"# nothing here\n")
        self.assertIsNone(parsed["descriptor"])

    def test_const_string_jumbo_and_field_initialisers_are_scanned(self) -> None:
        tigon = parse_smali(TIGON.encode("utf-8"))
        clips = parse_smali(CLIPS_HOST.encode("utf-8"))
        # .field initialiser
        self.assertIn("feed/timeline/", tigon["api_paths"])
        # const-string/jumbo
        self.assertIn("clips/discover/stream/", clips["api_paths"])


class TestApiPathClassifier(unittest.TestCase):
    def test_accepts_real_endpoint_shapes(self) -> None:
        for value in (
            "clips/discover/",
            "feed/timeline/",
            "discover/topical_explore/",
            "clips/discover/interest/stream/",
            "video/refresh_resources/%s/",
            "text_feed/{post_id}/replies_in_ig/",
            "api/v1/clips/autoplay_configs/",
            "direct_v2/get_folders/",
        ):
            with self.subTest(value=value):
                self.assertTrue(looks_like_api_path(value))

    def test_rejects_descriptors_signatures_and_noise(self) -> None:
        for value in (
            "Ljava/lang/String;",                      # type descriptor
            "LX/04tC;",                                # obfuscated descriptor
            "getDevServerName()Ljava/lang/String;",    # method signature
            "com/instagram/api/tigon/TigonServiceLayer",  # class path, uppercase
            "video/mp4",                               # bare MIME type
            "audio/mpeg",
            "application/mp4",
            "n/a",                                     # too short
            "feed",                                    # no slash
            "kb/s abr:",                               # whitespace
            "^(?:https?:\\\\/\\\\/)?(?:www\\\\.)?meta",  # escapes / punctuation
            "%d/%d+",                                  # format string
            "https://",                                # bare URI scheme
            "content://",
            "ig://",
            "emoji:/",
            "",
        ):
            with self.subTest(value=value):
                self.assertFalse(looks_like_api_path(value))

    def test_accepts_bytes_and_str_identically(self) -> None:
        self.assertTrue(looks_like_api_path(b"clips/discover/"))
        self.assertFalse(looks_like_api_path(b"Ljava/lang/String;"))

    def test_multi_segment_path_under_a_mime_top_level_is_kept(self) -> None:
        # "video/mp4" is noise; "video/refresh_resources/%s/" is a real endpoint.
        self.assertFalse(looks_like_api_path("video/mp4"))
        self.assertTrue(looks_like_api_path("video/refresh_resources/%s/"))

    def test_scheme_with_a_path_is_kept(self) -> None:
        # Only the bare scheme is dropped; a scheme carrying a path is API surface.
        self.assertFalse(looks_like_api_path("https://"))
        self.assertTrue(looks_like_api_path("https://i.instagram.com/api/v1/"))
        self.assertTrue(looks_like_api_path("https://b-graph.facebook.com/graphql"))

    def test_single_segment_realtime_topics_survive(self) -> None:
        # MQTT topic names are real API surface and carry no second segment;
        # they must not be filtered out along with the bare schemes.
        for value in ("/ig_realtime_sub", "/fbns_msg", "/pubsub", "/ig_send_message"):
            with self.subTest(value=value):
                self.assertTrue(looks_like_api_path(value))


class TestNameClassification(unittest.TestCase):
    def test_obfuscated_detection(self) -> None:
        self.assertTrue(is_obfuscated("LX/04tC;"))
        self.assertTrue(is_obfuscated("LX/0Di2;"))
        self.assertFalse(is_obfuscated("Lcom/instagram/api/tigon/TigonServiceLayer;"))
        self.assertFalse(is_obfuscated("Lcom/facebook/redex/annotations/IgnoreStringLiterals;"))
        self.assertFalse(is_obfuscated("Lkotlinx/coroutines/Job;"))

    def test_smali_tree_names(self) -> None:
        self.assertTrue(is_smali_tree("smali"))
        self.assertTrue(is_smali_tree("smali_classes20"))
        self.assertFalse(is_smali_tree("smali_classes"))
        self.assertFalse(is_smali_tree("smali_assets"))
        self.assertFalse(is_smali_tree("res"))

    def test_tree_order_is_natural_not_lexicographic(self) -> None:
        names = ["smali_classes10", "smali", "smali_classes2", "smali_classes20"]
        self.assertEqual(
            sorted(names, key=smali_tree_sort_key),
            ["smali", "smali_classes2", "smali_classes10", "smali_classes20"],
        )


class TestStructuralIndex(IndexerTestCase):
    def test_one_row_per_class_with_paths_and_trees(self) -> None:
        self.build()
        _header, rows = read_rows(self.out)
        by_descriptor = {row["descriptor"]: row for row in rows}
        self.assertEqual(
            sorted(by_descriptor),
            ["LX/04tC;", "LX/0Di2;", "Lcom/instagram/api/tigon/TigonServiceLayer;"],
        )

        tigon = by_descriptor["Lcom/instagram/api/tigon/TigonServiceLayer;"]
        self.assertEqual(tigon["path"], "smali/com/instagram/api/tigon/TigonServiceLayer.smali")
        self.assertEqual(tigon["tree"], "smali")
        self.assertEqual(tigon["super"], "Ljava/lang/Object;")
        self.assertEqual(
            tigon["interfaces"],
            ["Lcom/instagram/common/api/base/ServiceLayer;", "LX/0Aa1;"],
        )
        self.assertIn("startRequest(Lcom/facebook/tigon/TigonRequest;)V", tigon["methods"])
        self.assertFalse(tigon["obfuscated"])

        clips = by_descriptor["LX/04tC;"]
        self.assertEqual(clips["path"], "smali_classes2/X/04tC.smali")
        self.assertEqual(clips["tree"], "smali_classes2")
        self.assertTrue(clips["obfuscated"])

        # DEX placement matters for the graft: the tree must be recorded, and
        # smali_classes10 must not be confused with smali_classes2 or smali.
        self.assertEqual(by_descriptor["LX/0Di2;"]["tree"], "smali_classes10")

    def test_paths_are_relative_to_the_decode(self) -> None:
        self.build()
        _header, rows = read_rows(self.out)
        for row in rows:
            with self.subTest(descriptor=row["descriptor"]):
                self.assertFalse(Path(row["path"]).is_absolute())
                self.assertTrue((self.decode / row["path"]).is_file())

    def test_tree_inventory_and_counts(self) -> None:
        header = self.build()
        self.assertEqual(header["smali_trees"], ["smali", "smali_classes2", "smali_classes10"])
        self.assertEqual(header["counts"]["classes"], 3)
        self.assertEqual(
            header["counts"]["classes_by_tree"],
            {"smali": 1, "smali_classes2": 1, "smali_classes10": 1},
        )
        self.assertEqual(header["counts"]["methods"], 5)

    def test_list_smali_trees_ignores_non_smali_directories(self) -> None:
        self.assertEqual(
            list_smali_trees(self.decode),
            ["smali", "smali_classes2", "smali_classes10"],
        )


class TestApiSurfaceIndex(IndexerTestCase):
    def test_api_path_maps_to_every_containing_descriptor(self) -> None:
        self.build()
        api_paths = read_api_surface(self.out)["api_paths"]
        self.assertEqual(api_paths["clips/discover/"], ["LX/04tC;", "LX/0Di2;"])
        self.assertEqual(api_paths["clips/discover/stream/"], ["LX/04tC;"])
        self.assertEqual(
            api_paths["feed/timeline/"],
            ["Lcom/instagram/api/tigon/TigonServiceLayer;"],
        )
        self.assertEqual(
            api_paths["discover/topical_explore/"],
            ["Lcom/instagram/api/tigon/TigonServiceLayer;"],
        )
        self.assertEqual(api_paths["text_feed/{post_id}/replies_in_ig/"], ["LX/0Di2;"])

    def test_non_api_literals_never_enter_the_index(self) -> None:
        self.build()
        api_paths = read_api_surface(self.out)["api_paths"]
        for value in (
            "video/mp4",
            "n/a",
            "Ljava/lang/String;",
            "getDevServerName()Ljava/lang/String;",
            "com/instagram/api/tigon/TigonServiceLayer",
        ):
            with self.subTest(value=value):
                self.assertNotIn(value, api_paths)

    def test_stable_types_only_and_mapped_to_paths(self) -> None:
        header = self.build()
        stable = read_api_surface(self.out)["stable_types"]
        self.assertEqual(
            stable,
            {
                "Lcom/instagram/api/tigon/TigonServiceLayer;": (
                    "smali/com/instagram/api/tigon/TigonServiceLayer.smali"
                )
            },
        )
        for descriptor in stable:
            self.assertFalse(is_obfuscated(descriptor))
        self.assertEqual(header["counts"]["stable_types"], 1)
        self.assertEqual(header["counts"]["obfuscated_types"], 2)

    def test_resource_ids_by_type(self) -> None:
        self.build()
        resources = read_api_surface(self.out)["resources"]
        self.assertEqual(
            resources["drawable"],
            {
                "instagram_arrow_back_24": "0x7f080001",
                "instagram_reels_outline_24": "0x7f080002",
            },
        )
        self.assertEqual(resources["id"], {"action_bar_textview_title": "0x7f0b0001"})
        self.assertEqual(resources["layout"], {"action_bar_title": "0x7f0e0001"})

    def test_reverse_resource_lookup(self) -> None:
        self.build()
        names_by_id = read_api_surface(self.out)["resource_names_by_id"]
        self.assertEqual(names_by_id["0x7f080002"], "drawable/instagram_reels_outline_24")
        self.assertEqual(names_by_id["0x7f0e0001"], "layout/action_bar_title")

    def test_strings_are_not_indexed_but_their_shortfall_is_recorded(self) -> None:
        header = self.build()
        surface = read_api_surface(self.out)
        self.assertNotIn("string", surface["resources"])
        self.assertNotIn("anim", surface["resources"])
        # The counts of every public.xml type are still reported, which is what
        # makes the sparse-string-encoding shortfall visible instead of silent.
        self.assertEqual(header["public_xml_type_counts"]["string"], 1)
        self.assertEqual(header["public_xml_type_counts"]["drawable"], 2)
        self.assertIn("sparse", header["string_resources"].lower())

    def test_resource_types_are_configurable(self) -> None:
        self.build(resource_types=("anim",))
        resources = read_api_surface(self.out)["resources"]
        self.assertEqual(resources, {"anim": {"bounce": "0x7f010000"}})

    def test_missing_public_xml_is_not_fatal(self) -> None:
        os.remove(self.decode / "res" / "values" / "public.xml")
        header = self.build()
        self.assertEqual(header["counts"]["resources"], {"drawable": 0, "id": 0, "layout": 0})
        self.assertEqual(header["content_hash_inputs"]["resource_files"], 0)


class TestHeaderAndStaleness(IndexerTestCase):
    def test_header_identity_fields(self) -> None:
        header = self.build()
        self.assertEqual(header["kind"], build_index.HEADER_KIND)
        self.assertEqual(header["decode_path"], str(self.decode.resolve()))
        self.assertTrue(header["content_hash"].startswith("sha256:"))
        self.assertEqual(header["content_hash_inputs"]["smali_files"], 3)
        self.assertEqual(header["content_hash_inputs"]["resource_files"], 1)
        self.assertIn("total", header["timings_seconds"])

    def test_per_version_warning_is_carried_in_the_header(self) -> None:
        header = self.build()
        warning = header["warning"].lower()
        self.assertIn("per-version", warning)
        self.assertIn("recycled", warning)
        # and in the module docstring, where a reader will actually meet it
        self.assertIn("NEVER JOIN ON AN OBFUSCATED NAME ACROSS", build_index.__doc__)

    def test_same_header_identity_in_all_three_outputs(self) -> None:
        self.build()
        on_disk = json.loads((self.out / HEADER_FILENAME).read_text(encoding="utf-8"))
        embedded, _rows = read_rows(self.out)
        surface = read_api_surface(self.out)["header"]
        for key in ("kind", "schema_version", "decode_path", "content_hash", "counts"):
            with self.subTest(key=key):
                self.assertEqual(on_disk[key], embedded[key])
                self.assertEqual(on_disk[key], surface[key])

    def test_fresh_index_checks_out(self) -> None:
        self.build()
        result = check_index(self.decode, self.out)
        self.assertTrue(result["fresh"], result["reasons"])
        self.assertEqual(result["smali_files"], 3)

    def test_edited_smali_makes_the_index_stale(self) -> None:
        self.build()
        target = self.decode / "smali_classes2" / "X" / "04tC.smali"
        target.write_text(CLIPS_HOST + "\n# appended\n", encoding="utf-8")
        result = check_index(self.decode, self.out)
        self.assertFalse(result["fresh"])
        self.assertTrue(any("content hash" in reason for reason in result["reasons"]))

    def test_same_length_edit_is_still_detected(self) -> None:
        # A size-and-mtime inventory would miss this; a content hash must not.
        self.build()
        target = self.decode / "smali_classes2" / "X" / "04tC.smali"
        original = target.read_text(encoding="utf-8")
        mutated = original.replace("clips/discover/stream/", "clips/discover/stresm/")
        self.assertEqual(len(mutated), len(original))
        self.assertNotEqual(mutated, original)
        target.write_text(mutated, encoding="utf-8")
        self.assertFalse(check_index(self.decode, self.out)["fresh"])

    def test_added_and_removed_smali_files_are_detected(self) -> None:
        self.build()
        extra = self.decode / "smali_classes2" / "X" / "0New.smali"
        extra.write_text(".class public LX/0New;\n.super Ljava/lang/Object;\n", encoding="utf-8")
        self.assertFalse(check_index(self.decode, self.out)["fresh"])
        os.remove(extra)
        self.assertTrue(check_index(self.decode, self.out)["fresh"])

    def test_edited_public_xml_makes_the_index_stale(self) -> None:
        self.build()
        public_xml = self.decode / "res" / "values" / "public.xml"
        public_xml.write_text(
            PUBLIC_XML.replace('id="0x7f080001"', 'id="0x7f080009"'), encoding="utf-8"
        )
        self.assertFalse(check_index(self.decode, self.out)["fresh"])

    def test_index_from_a_different_decode_is_rejected(self) -> None:
        # The dangerous case: a 430 index pointed at a 439 decode.  Identical
        # content is not enough; the recorded decode path must match too.
        self.build()
        twin = self.root / "twin"
        twin.mkdir()
        shutil.copytree(self.decode, twin / "stock-test")
        result = check_index(twin / "stock-test", self.out)
        self.assertFalse(result["fresh"])
        self.assertTrue(any("built from" in reason for reason in result["reasons"]))

    def test_schema_version_bump_invalidates_an_old_index(self) -> None:
        self.build()
        header_path = self.out / HEADER_FILENAME
        header = json.loads(header_path.read_text(encoding="utf-8"))
        header["schema_version"] = build_index.SCHEMA_VERSION - 1
        header_path.write_text(json.dumps(header), encoding="utf-8")
        result = check_index(self.decode, self.out)
        self.assertFalse(result["fresh"])
        self.assertTrue(any("schema_version" in reason for reason in result["reasons"]))


class TestBuildBehaviour(IndexerTestCase):
    def test_decode_is_never_written_to(self) -> None:
        before = {
            path: path.read_bytes()
            for path in sorted(self.decode.rglob("*"))
            if path.is_file()
        }
        self.build()
        after = {
            path: path.read_bytes()
            for path in sorted(self.decode.rglob("*"))
            if path.is_file()
        }
        self.assertEqual(before, after)

    def test_parallel_build_matches_serial_build_byte_for_byte(self) -> None:
        self.build()
        serial_rows = (self.out / STRUCTURAL_FILENAME).read_text(encoding="utf-8").splitlines()[1:]
        serial_hash = json.loads((self.out / HEADER_FILENAME).read_text(encoding="utf-8"))[
            "content_hash"
        ]

        parallel_out = self.root / "index-parallel"
        header = run_build(self.decode, parallel_out, jobs=2)
        parallel_rows = (
            (parallel_out / STRUCTURAL_FILENAME).read_text(encoding="utf-8").splitlines()[1:]
        )
        self.assertEqual(serial_rows, parallel_rows)
        self.assertEqual(serial_hash, header["content_hash"])
        self.assertEqual(read_api_surface(self.out)["api_paths"], read_api_surface(parallel_out)["api_paths"])

    def test_shard_scratch_directory_is_cleaned_up(self) -> None:
        self.build(jobs=2)
        self.assertFalse((self.out / ".shards").exists())

    def test_rebuild_is_deterministic_apart_from_the_timestamp(self) -> None:
        first = self.build()
        second = run_build(self.decode, self.root / "index2")
        ignored = {"generated_at", "timings_seconds", "decode_path", "jobs"}
        self.assertEqual(
            {key: value for key, value in first.items() if key not in ignored},
            {key: value for key, value in second.items() if key not in ignored},
        )

    def test_missing_decode_raises(self) -> None:
        with self.assertRaises(NotADirectoryError):
            run_build(self.root / "nope", self.out)


class TestCli(IndexerTestCase):
    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = build_index.main(argv)
        return code, stdout.getvalue()

    def test_build_then_check_exit_codes(self) -> None:
        code, _ = self.run_cli([str(self.decode), "--out", str(self.out), "--quiet"])
        self.assertEqual(code, 0)

        code, output = self.run_cli([str(self.decode), "--out", str(self.out), "--check"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output)["fresh"])

        (self.decode / "smali_classes2" / "X" / "04tC.smali").write_text(
            CLIPS_HOST + "\n# drift\n", encoding="utf-8"
        )
        code, output = self.run_cli([str(self.decode), "--out", str(self.out), "--check"])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(output)["fresh"])

    def test_quiet_suppresses_the_stdout_summary(self) -> None:
        _code, quiet_output = self.run_cli([str(self.decode), "--out", str(self.out), "--quiet"])
        self.assertEqual(quiet_output, "")
        _code, loud_output = self.run_cli([str(self.decode), "--out", str(self.out)])
        self.assertIn("content_hash", loud_output)

    def test_resource_types_flag(self) -> None:
        self.run_cli(
            [
                str(self.decode),
                "--out",
                str(self.out),
                "--quiet",
                "--resource-types",
                "drawable,anim",
            ]
        )
        resources = read_api_surface(self.out)["resources"]
        self.assertEqual(sorted(resources), ["anim", "drawable"])

    def test_rejects_zero_jobs(self) -> None:
        with self.assertRaises(SystemExit):
            self.run_cli([str(self.decode), "--out", str(self.out), "--jobs", "0"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

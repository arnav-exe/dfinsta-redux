"""Tests for the per-decode index reader.

The index this module reads is 70 MB per version and is gitignored, so almost
every test here builds a tiny synthetic index in a temp directory in exactly the
shape `tools/indexer/build_index.py` writes: `header.json`, `api_surface.json`,
and a `structural.jsonl` whose first line is the header and whose remaining
lines are class rows. Binding the unit tests to `work/index-430` would make them
slow, version-locked, and unrunnable on a fresh clone.

The synthetic api_paths fixture is a faithful miniature of the real 430 one: the
three Reels literals sit in overlapping but unequal sets of classes, and exactly
one class carries all three. That shape is the entire justification for
`descriptors_with_all_literals`, so the fixture is built to make a union-vs-
intersection mistake produce a provably different answer rather than a
coincidentally equal one.

`MutationTests` does not test new behaviour. It re-attacks three guards that
already have positive tests, from the direction a broken implementation would
take, so that "the guard is present" and "the guard bites" are separate claims.

`RealIndexTests` is the one test that touches `work/index-430` and
`work/index-439`; it skips when they are absent.

`KnownGapTests` holds characterisation tests: they pin behaviour that is
arguably wrong so that a future fix fails loudly rather than silently changing
what the reader raises.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from dfinsta_pipeline.hook_index import (
    API_SURFACE_FILENAME,
    HEADER_FILENAME,
    SCHEMA_VERSION,
    STRUCTURAL_FILENAME,
    ClassRow,
    HookIndex,
    IndexUnusable,
)


ROOT = Path(__file__).resolve().parents[1]
INDEX_430 = ROOT / "work" / "index-430"
INDEX_439 = ROOT / "work" / "index-439"

REELS_LITERALS = ["clips/discover/", "clips/homecoming/", "clips/discover/stream/"]


# --------------------------------------------------------------------- fixture

# Four classes, mirroring the shape of the real structural rows. `LX/05t2;` is
# the descriptor the 430 index really gives for the Reels request builder, and
# `LX/04Pn;` / `LX/0aOK;` really do share two of its three literals: the fixture
# reproduces that overlap because it is what makes co-location discriminating.
ROWS = [
    {
        "kind": "dfinsta.index.class",
        "descriptor": "LX/05t2;",
        "path": "smali_classes3/LX/05t2.smali",
        "tree": "smali_classes3",
        "super": "Ljava/lang/Object;",
        "interfaces": [],
        "methods": ["A00()Ljava/lang/String;", "A01(Ljava/lang/String;)V"],
        "obfuscated": True,
    },
    {
        "kind": "dfinsta.index.class",
        "descriptor": "LX/04Pn;",
        "path": "smali_classes7/LX/04Pn.smali",
        "tree": "smali_classes7",
        "super": "LX/0Aaa;",
        "interfaces": ["Ljava/lang/Runnable;"],
        "methods": ["run()V"],
        "obfuscated": True,
    },
    {
        "kind": "dfinsta.index.class",
        "descriptor": "LX/0aOK;",
        "path": "smali_classes9/LX/0aOK.smali",
        "tree": "smali_classes9",
        "super": "Ljava/lang/Object;",
        "interfaces": [],
        "methods": ["A02()V"],
        "obfuscated": True,
    },
    {
        "kind": "dfinsta.index.class",
        "descriptor": "Lcom/instagram/app/InstagramAppShell;",
        "path": "smali_classes3/com/instagram/app/InstagramAppShell.smali",
        "tree": "smali_classes3",
        "super": "Landroid/app/Application;",
        "interfaces": [],
        "methods": ["onCreate()V"],
        "obfuscated": False,
    },
]

# Only `LX/05t2;` carries all three clips literals. `LX/0aOK;` carries two of
# them and `LX/04Pn;` a different two, so union and intersection cannot agree.
API_PATHS = {
    "clips/discover/": ["LX/04Pn;", "LX/05t2;", "LX/0aOK;"],
    "clips/discover/stream/": ["LX/04Pn;", "LX/05t2;"],
    "clips/homecoming/": ["LX/05t2;", "LX/0aOK;"],
    "friendships/create/": ["LX/04Pn;"],
}

RESOURCES = {
    "drawable": {
        "instagram_menu_outline_24": "0x7f0824e6",
        "instagram_x_pano_outline_24": "0x7f0826f0",
    },
    "id": {"action_bar_button_action": "0x7f0b0042"},
    "layout": {"action_bar_item": "0x7f0e0011"},
}

STABLE_TYPES = {
    "Lcom/instagram/api/tigon/TigonServiceLayer;": (
        "smali_classes5/com/instagram/api/tigon/TigonServiceLayer.smali"
    ),
    "Lcom/instagram/app/InstagramAppShell;": (
        "smali_classes3/com/instagram/app/InstagramAppShell.smali"
    ),
}


def make_header(decode: str, **overrides: object) -> dict:
    """A header carrying the fields the reader actually consults."""
    header: dict = {
        "kind": "dfinsta.index.header",
        "schema_version": SCHEMA_VERSION,
        "generator": "tools/indexer/build_index.py",
        "decode_path": decode,
        "decode_name": Path(decode).name,
        "content_hash": "sha256:" + "ab" * 32,
        "smali_trees": ["smali_classes3", "smali_classes7", "smali_classes9"],
        "counts": {"classes": len(ROWS), "api_paths": len(API_PATHS)},
        "resource_types_indexed": ["drawable", "id", "layout"],
    }
    header.update(overrides)
    return header


def write_index(
    directory: Path,
    *,
    decode: str = "/decodes/stock-430",
    header_overrides: dict | None = None,
    header_drop: tuple[str, ...] = (),
    header_line: dict | None = None,
    rows: list | None = None,
    api_paths: dict | None = None,
    resources: dict | None = None,
    stable_types: dict | None = None,
    structural_text: str | None = None,
    drop: tuple[str, ...] = (),
) -> Path:
    """Write a synthetic index in exactly the three-file shape the builder emits.

    `header_line` overrides only the object on line 1 of `structural.jsonl`,
    which is how the "the header line is not a class row" tests poison it.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    header = make_header(decode, **(header_overrides or {}))
    for key in header_drop:
        header.pop(key, None)
    rows = ROWS if rows is None else rows
    surface = {
        "header": header,
        "api_paths": API_PATHS if api_paths is None else api_paths,
        "resources": RESOURCES if resources is None else resources,
        "resource_names_by_id": {"0x7f0824e6": "drawable/instagram_menu_outline_24"},
        "stable_types": STABLE_TYPES if stable_types is None else stable_types,
    }

    if HEADER_FILENAME not in drop:
        (directory / HEADER_FILENAME).write_text(
            json.dumps(header, indent=2) + "\n", encoding="utf-8"
        )
    if API_SURFACE_FILENAME not in drop:
        (directory / API_SURFACE_FILENAME).write_text(
            json.dumps(surface, separators=(",", ":")) + "\n", encoding="utf-8"
        )
    if STRUCTURAL_FILENAME not in drop:
        if structural_text is None:
            lines = [json.dumps(header_line if header_line is not None else header)]
            lines.extend(json.dumps(row, separators=(",", ":")) for row in rows)
            structural_text = "\n".join(lines) + "\n"
        (directory / STRUCTURAL_FILENAME).write_text(structural_text, encoding="utf-8")
    return directory


class IndexTestCase(unittest.TestCase):
    """Gives every test its own temp root and a one-line way to build an index."""

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        # `.resolve()` because the header stores a resolved path (build_index.py
        # resolves before recording it) and `/tmp` is a symlink on some systems.
        self.decode = (self.tmp / "stock-430").resolve()
        self.decode.mkdir()

    def build(self, name: str = "index", **kwargs: object) -> Path:
        kwargs.setdefault("decode", str(self.decode))
        return write_index(self.tmp / name, **kwargs)  # type: ignore[arg-type]

    def load(self, name: str = "index", **kwargs: object) -> HookIndex:
        return HookIndex.load(self.build(name, **kwargs))


class LoadTests(IndexTestCase):
    """`load` is the only place a broken index can be rejected cheaply."""

    def test_a_complete_index_loads_and_exposes_its_header(self):
        index = self.load()
        self.assertEqual(index.decode_path, str(self.decode))
        self.assertEqual(index.content_hash, "sha256:" + "ab" * 32)
        self.assertEqual(index.resource_types, ("drawable", "id", "layout"))
        self.assertEqual(index.header["schema_version"], SCHEMA_VERSION)
        self.assertEqual(index.literal_count, len(API_PATHS))

    def test_accepts_a_string_path(self):
        # The annotation is `Path | str`; callers pass argparse output either way.
        index = HookIndex.load(str(self.build()))
        self.assertEqual(index.class_count(), len(ROWS))

    def test_resource_types_is_a_tuple_so_it_cannot_be_edited_in_place(self):
        self.assertIsInstance(self.load().resource_types, tuple)

    def test_the_header_property_is_a_copy_not_the_live_mapping(self):
        index = self.load()
        header = index.header
        header["decode_path"] = "/somewhere/else"
        self.assertEqual(index.header["decode_path"], str(self.decode))
        self.assertEqual(index.decode_path, str(self.decode))

    def test_a_missing_header_is_unusable(self):
        with self.assertRaises(IndexUnusable) as caught:
            HookIndex.load(self.build(drop=(HEADER_FILENAME,)))
        self.assertIn("incomplete", str(caught.exception))
        self.assertIn(HEADER_FILENAME, str(caught.exception))

    def test_a_missing_api_surface_is_unusable(self):
        with self.assertRaises(IndexUnusable) as caught:
            HookIndex.load(self.build(drop=(API_SURFACE_FILENAME,)))
        self.assertIn("incomplete", str(caught.exception))
        self.assertIn(API_SURFACE_FILENAME, str(caught.exception))

    def test_a_missing_structural_index_is_unusable(self):
        """Caught at load, not at the first lookup.

        `structural.jsonl` is only read lazily, so without this check a reader
        would construct happily and fail deep inside host search instead.
        """
        with self.assertRaises(IndexUnusable) as caught:
            HookIndex.load(self.build(drop=(STRUCTURAL_FILENAME,)))
        self.assertIn(STRUCTURAL_FILENAME, str(caught.exception))

    def test_an_index_directory_that_does_not_exist_is_unusable(self):
        with self.assertRaises(IndexUnusable):
            HookIndex.load(self.tmp / "never-built")

    def test_malformed_header_json_is_unusable(self):
        directory = self.build()
        (directory / HEADER_FILENAME).write_text("{not json", encoding="utf-8")
        with self.assertRaises(IndexUnusable) as caught:
            HookIndex.load(directory)
        self.assertIn("malformed", str(caught.exception))

    def test_malformed_api_surface_json_is_unusable(self):
        directory = self.build()
        (directory / API_SURFACE_FILENAME).write_text('{"api_paths": ', encoding="utf-8")
        with self.assertRaises(IndexUnusable) as caught:
            HookIndex.load(directory)
        self.assertIn("malformed", str(caught.exception))

    def test_a_truncated_index_is_unusable_rather_than_empty(self):
        # An interrupted build leaves a zero-byte file; that must not read as a
        # valid index with no classes.
        directory = self.build()
        (directory / API_SURFACE_FILENAME).write_text("", encoding="utf-8")
        with self.assertRaises(IndexUnusable):
            HookIndex.load(directory)

    def test_a_wrong_schema_version_is_unusable(self):
        """The reader knows one layout; a different one is not forwards-readable.

        Silently accepting version N+1 would mean reading fields that mean
        something else, which is the same class of wrong answer the whole module
        exists to prevent.
        """
        for version in (0, 2, "1", None):
            with self.subTest(version=version):
                with self.assertRaises(IndexUnusable) as caught:
                    HookIndex.load(
                        self.build(f"v{version!r}", header_overrides={"schema_version": version})
                    )
                message = str(caught.exception)
                self.assertIn("schema_version", message)
                self.assertIn(repr(version), message)
                self.assertIn(str(SCHEMA_VERSION), message)

    def test_a_header_with_no_schema_version_at_all_is_unusable(self):
        with self.assertRaises(IndexUnusable) as caught:
            HookIndex.load(self.build("noschema", header_drop=("schema_version",)))
        self.assertIn("schema_version", str(caught.exception))

    def test_an_api_surface_missing_every_optional_section_still_loads(self):
        # `resources`/`stable_types`/`api_paths` are all `.get`-with-default, so a
        # minimal surface must degrade to empty answers, not to an exception.
        directory = self.build()
        (directory / API_SURFACE_FILENAME).write_text("{}", encoding="utf-8")
        index = HookIndex.load(directory)
        self.assertEqual(index.literal_count, 0)
        self.assertEqual(index.descriptors_with_literal("clips/discover/"), ())
        self.assertIsNone(index.stable_type_path("Lcom/instagram/app/InstagramAppShell;"))


class DecodeBindingTests(IndexTestCase):
    """`LX/05t2;` is a Reels builder on 430 and an unrelated class on 439.

    A cross-decode lookup therefore returns a confident wrong answer rather than
    a miss, and nothing downstream can tell the difference. `assert_matches` is
    the only place that mistake is detectable, so these tests care as much about
    the exception text as about the exception.
    """

    def test_for_decode_accepts_the_decode_the_header_names(self):
        index = HookIndex.for_decode(self.build(), self.decode)
        self.assertEqual(index.decode_path, str(self.decode))
        self.assertEqual(index.path_for("LX/05t2;"), "smali_classes3/LX/05t2.smali")

    def test_assert_matches_returns_none_for_the_right_decode(self):
        index = self.load()
        self.assertIsNone(index.assert_matches(self.decode))

    def test_accepts_a_string_decode_path(self):
        self.assertIsNone(self.load().assert_matches(str(self.decode)))

    def test_accepts_a_trailing_slash(self):
        self.assertIsNone(self.load().assert_matches(str(self.decode) + "/"))

    def test_accepts_an_unresolved_path_that_resolves_to_the_same_place(self):
        """Callers pass whatever came off the command line.

        Rejecting `work/x/../x` would be a false alarm on the correct decode,
        which trains people to route around the guard.
        """
        dotted = self.decode.parent / "elsewhere" / ".." / self.decode.name
        self.assertIsNone(self.load().assert_matches(dotted))

    def test_accepts_a_relative_path_that_resolves_to_the_same_place(self):
        index = self.load()
        previous = os.getcwd()
        os.chdir(self.decode.parent)
        try:
            self.assertIsNone(index.assert_matches(self.decode.name))
        finally:
            os.chdir(previous)

    def test_accepts_a_symlink_to_the_named_decode(self):
        link = self.tmp / "stock-430-link"
        try:
            os.symlink(self.decode, link)
        except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
            self.skipTest("symlinks unavailable")
        self.assertIsNone(self.load().assert_matches(link))

    def test_rejects_any_other_decode(self):
        other = self.tmp / "stock-439"
        other.mkdir()
        index = self.load()
        with self.assertRaises(IndexUnusable) as caught:
            index.assert_matches(other)
        message = str(caught.exception)
        # The message has to name both sides: "this index is wrong" is useless
        # without saying which decode it was built from.
        self.assertIn(str(self.decode), message)
        self.assertIn(str(other), message)
        self.assertIn("built from", message)
        self.assertIn("recycled", message)

    def test_rejects_a_sibling_whose_name_is_a_prefix_of_the_real_one(self):
        # Guards against a `startswith`-style comparison ever creeping in.
        sibling = Path(str(self.decode) + "-v2")
        sibling.mkdir()
        with self.assertRaises(IndexUnusable):
            self.load().assert_matches(sibling)

    def test_rejects_a_decode_that_does_not_exist(self):
        # A typo'd path resolves to a real absolute string; it must not be
        # accepted just because resolution succeeded.
        with self.assertRaises(IndexUnusable):
            self.load().assert_matches(self.tmp / "typo-430")

    def test_for_decode_raises_before_returning_a_reader(self):
        other = self.tmp / "stock-439"
        other.mkdir()
        with self.assertRaises(IndexUnusable):
            HookIndex.for_decode(self.build(), other)

    def test_for_decode_still_reports_a_broken_index_as_unusable(self):
        # Load failures must not be masked by the decode check or vice versa.
        with self.assertRaises(IndexUnusable) as caught:
            HookIndex.for_decode(self.build(drop=(HEADER_FILENAME,)), self.decode)
        self.assertIn("incomplete", str(caught.exception))

    def test_a_header_with_no_decode_path_matches_nothing(self):
        """"Built from an unknown decode" must fail closed, not open.

        A header with nothing to compare against is the state a hand-edited or
        pre-schema index would be in; it must match no decode at all rather than
        every decode.
        """
        index = HookIndex.load(self.build(header_drop=("decode_path",)))
        self.assertEqual(index.decode_path, "")
        with self.assertRaises(IndexUnusable):
            index.assert_matches(self.decode)
        with self.assertRaises(IndexUnusable):
            HookIndex.for_decode(self.build("nopath2", header_drop=("decode_path",)), self.decode)


class StructuralLookupTests(IndexTestCase):
    def setUp(self):
        super().setUp()
        self.index = self.load()

    def test_path_for_returns_the_decode_relative_path(self):
        self.assertEqual(self.index.path_for("LX/05t2;"), "smali_classes3/LX/05t2.smali")
        self.assertEqual(
            self.index.path_for("Lcom/instagram/app/InstagramAppShell;"),
            "smali_classes3/com/instagram/app/InstagramAppShell.smali",
        )

    def test_path_for_an_absent_descriptor_is_none(self):
        # "This version has no such class" is a normal answer for an obfuscated
        # descriptor carried in from another version, not an error.
        self.assertIsNone(self.index.path_for("LX/99zz;"))
        self.assertIsNone(self.index.path_for(""))

    def test_has_agrees_with_path_for(self):
        for descriptor in ("LX/05t2;", "LX/99zz;", "Lcom/instagram/app/InstagramAppShell;"):
            with self.subTest(descriptor=descriptor):
                self.assertEqual(
                    self.index.has(descriptor), self.index.path_for(descriptor) is not None
                )

    def test_has_is_false_for_an_absent_descriptor(self):
        self.assertFalse(self.index.has("LX/99zz;"))
        self.assertTrue(self.index.has("LX/05t2;"))

    def test_row_for_returns_every_recorded_field(self):
        row = self.index.row_for("LX/04Pn;")
        self.assertIsInstance(row, ClassRow)
        assert row is not None
        self.assertEqual(row.descriptor, "LX/04Pn;")
        self.assertEqual(row.path, "smali_classes7/LX/04Pn.smali")
        self.assertEqual(row.tree, "smali_classes7")
        self.assertEqual(row.super_descriptor, "LX/0Aaa;")
        self.assertEqual(row.interfaces, ("Ljava/lang/Runnable;",))
        self.assertEqual(row.methods, ("run()V",))
        self.assertTrue(row.obfuscated)

    def test_row_for_an_absent_descriptor_is_none(self):
        self.assertIsNone(self.index.row_for("LX/99zz;"))

    def test_row_for_records_a_stable_type_as_not_obfuscated(self):
        row = self.index.row_for("Lcom/instagram/app/InstagramAppShell;")
        assert row is not None
        self.assertFalse(row.obfuscated)
        self.assertEqual(row.super_descriptor, "Landroid/app/Application;")

    def test_class_count_counts_the_class_rows(self):
        self.assertEqual(self.index.class_count(), len(ROWS))

    def test_the_header_line_is_not_counted_as_a_class(self):
        """A naive `for line in file` reader would add a bogus fifth row.

        Line 1 of `structural.jsonl` is the header object, not a class. Counting
        it would inflate `class_count` and, worse, put a non-class entry into the
        descriptor map that host search could match against.
        """
        self.assertEqual(self.index.class_count(), 4)
        self.assertIsNone(self.index.path_for("dfinsta.index.header"))

    def test_the_header_line_is_skipped_even_when_it_looks_like_a_class_row(self):
        # Positive proof that line 1 is skipped rather than merely surviving
        # because a real header lacks `descriptor`: this one has both keys.
        poisoned = make_header(str(self.decode))
        poisoned["descriptor"] = "LHeaderPoison;"
        poisoned["path"] = "header/poison.smali"
        index = HookIndex.load(self.build("poisoned", header_line=poisoned))
        self.assertFalse(index.has("LHeaderPoison;"))
        self.assertIsNone(index.row_for("LHeaderPoison;"))
        self.assertEqual(index.class_count(), len(ROWS))

    def test_blank_lines_in_the_structural_index_are_ignored(self):
        text = "\n".join(
            [json.dumps(make_header(str(self.decode)))]
            + [json.dumps(ROWS[0]), "", "   ", json.dumps(ROWS[1]), ""]
        )
        index = HookIndex.load(self.build("blanks", structural_text=text + "\n"))
        self.assertEqual(index.class_count(), 2)
        self.assertTrue(index.has("LX/05t2;"))

    def test_a_structural_index_with_only_a_header_holds_no_classes(self):
        index = HookIndex.load(self.build("headeronly", rows=[]))
        self.assertEqual(index.class_count(), 0)
        self.assertFalse(index.has("LX/05t2;"))
        self.assertIsNone(index.path_for("LX/05t2;"))
        self.assertIsNone(index.row_for("LX/05t2;"))


class ClassRowTests(unittest.TestCase):
    def test_from_dict_defaults_every_optional_field(self):
        row = ClassRow.from_dict({"descriptor": "LFoo;", "path": "p", "tree": "smali"})
        self.assertIsNone(row.super_descriptor)
        self.assertEqual(row.interfaces, ())
        self.assertEqual(row.methods, ())
        self.assertFalse(row.obfuscated)

    def test_from_dict_coerces_sequences_to_tuples(self):
        row = ClassRow.from_dict(
            {
                "descriptor": "LFoo;",
                "path": "p",
                "tree": "smali",
                "interfaces": ["LA;"],
                "methods": ["f()V"],
                "obfuscated": 1,
            }
        )
        self.assertIsInstance(row.interfaces, tuple)
        self.assertIsInstance(row.methods, tuple)
        # `obfuscated` is coerced, not stored: a JSON int must not leak through.
        self.assertIs(row.obfuscated, True)

    def test_is_frozen(self):
        row = ClassRow.from_dict({"descriptor": "LFoo;", "path": "p", "tree": "smali"})
        with self.assertRaises(Exception):
            row.path = "other"  # type: ignore[misc]


class LiteralTests(IndexTestCase):
    def setUp(self):
        super().setUp()
        self.index = self.load()

    def test_returns_every_class_holding_the_literal(self):
        self.assertEqual(
            self.index.descriptors_with_literal("clips/discover/"),
            ("LX/04Pn;", "LX/05t2;", "LX/0aOK;"),
        )

    def test_returns_a_tuple(self):
        # Callers pass this straight into evidence dictionaries; a live list would
        # let one of them mutate the reader's own state.
        result = self.index.descriptors_with_literal("clips/discover/")
        self.assertIsInstance(result, tuple)

    def test_an_unknown_literal_is_an_empty_tuple(self):
        self.assertEqual(self.index.descriptors_with_literal("clips/nope/"), ())
        self.assertEqual(self.index.descriptors_with_literal(""), ())

    def test_the_stored_order_is_preserved_rather_than_re_sorted(self):
        # The builder writes each bucket sorted, so the reader does not re-sort.
        # Pinned so the contract is known rather than assumed.
        index = HookIndex.load(
            self.build("scrambled", api_paths={"a/b/": ["LX/0zz;", "LX/00a;", "LX/0mm;"]})
        )
        self.assertEqual(
            index.descriptors_with_literal("a/b/"), ("LX/0zz;", "LX/00a;", "LX/0mm;")
        )

    def test_literal_is_indexed_distinguishes_absent_from_empty(self):
        """"No class has it" and "it was never a candidate" are different answers.

        Only API-path-shaped strings are indexed, so an empty result alone cannot
        tell a dropped endpoint from a mis-authored manifest literal.
        """
        self.assertTrue(self.index.literal_is_indexed("clips/discover/"))
        self.assertFalse(self.index.literal_is_indexed("Distraction-free settings"))
        self.assertEqual(self.index.descriptors_with_literal("Distraction-free settings"), ())

    def test_literal_count_reports_the_indexed_literals(self):
        self.assertEqual(self.index.literal_count, 4)


class CoLocationTests(IndexTestCase):
    """`descriptors_with_all_literals` is what actually finds the Reels host.

    Each clips literal alone points at several classes -- analytics maps and
    prefetch allowlists carry them too -- so only the intersection identifies the
    class that builds the outgoing request path.
    """

    def setUp(self):
        super().setUp()
        self.index = self.load()

    def test_all_three_literals_narrow_to_the_one_request_builder(self):
        self.assertEqual(self.index.descriptors_with_all_literals(REELS_LITERALS), ("LX/05t2;",))

    def test_each_literal_alone_is_ambiguous(self):
        # The premise of the whole method: no single literal is discriminating.
        for literal in REELS_LITERALS:
            with self.subTest(literal=literal):
                self.assertGreater(len(self.index.descriptors_with_literal(literal)), 1)

    def test_adding_a_literal_narrows_the_result_monotonically(self):
        sizes = [
            len(self.index.descriptors_with_all_literals(REELS_LITERALS[:count]))
            for count in (1, 2, 3)
        ]
        self.assertEqual(sizes, [3, 2, 1])

    def test_intersection_not_union(self):
        # `LX/0aOK;` holds discover + homecoming; `LX/04Pn;` holds discover +
        # stream. Under union both survive; under intersection neither does.
        result = self.index.descriptors_with_all_literals(REELS_LITERALS)
        self.assertNotIn("LX/0aOK;", result)
        self.assertNotIn("LX/04Pn;", result)

    def test_a_single_literal_matches_that_literal_alone(self):
        self.assertEqual(
            set(self.index.descriptors_with_all_literals(["clips/homecoming/"])),
            set(self.index.descriptors_with_literal("clips/homecoming/")),
        )

    def test_an_empty_iterable_is_an_empty_tuple(self):
        """Not "every class": an empty requirement set intersects to everything.

        Returning the whole index for `[]` would hand host search 181,000
        candidates and call it a successful fingerprint match.
        """
        for empty in ([], (), iter(())):
            with self.subTest(empty=type(empty).__name__):
                self.assertEqual(self.index.descriptors_with_all_literals(empty), ())

    def test_one_absent_literal_empties_the_whole_result(self):
        """Fail closed: a partial match is not a match.

        If the app drops one of the three endpoints, the honest answer is "the
        host must be re-established", not "here is the class that still has two
        of them".
        """
        for absent in ("clips/nope/", "", "Distraction-free settings"):
            with self.subTest(absent=absent):
                self.assertEqual(
                    self.index.descriptors_with_all_literals(REELS_LITERALS + [absent]), ()
                )

    def test_an_absent_literal_wins_even_in_first_position(self):
        # The loop seeds `common` from the first bucket, so ordering must not
        # change the answer.
        self.assertEqual(
            self.index.descriptors_with_all_literals(["clips/nope/"] + REELS_LITERALS), ()
        )

    def test_literals_that_share_no_class_are_an_empty_tuple(self):
        # Both are indexed and both have classes; they are simply never together.
        self.assertTrue(self.index.literal_is_indexed("friendships/create/"))
        self.assertTrue(self.index.literal_is_indexed("clips/homecoming/"))
        self.assertEqual(
            self.index.descriptors_with_all_literals(
                ["friendships/create/", "clips/homecoming/"]
            ),
            (),
        )

    def test_the_result_is_sorted_and_deterministic(self):
        """The intersection is built from sets, whose iteration order is not stable.

        Candidate order decides which class the resolver reads first and which
        descriptor lands in the report, so an unsorted result would make runs
        differ from each other for no reason.
        """
        index = HookIndex.load(
            self.build(
                "scrambled",
                api_paths={
                    "a/b/": ["LX/0zz;", "LX/00a;", "LX/0mm;"],
                    "a/c/": ["LX/0mm;", "LX/0zz;", "LX/00a;"],
                },
            )
        )
        result = index.descriptors_with_all_literals(["a/b/", "a/c/"])
        self.assertEqual(result, ("LX/00a;", "LX/0mm;", "LX/0zz;"))
        self.assertEqual(list(result), sorted(result))
        for _ in range(5):
            self.assertEqual(index.descriptors_with_all_literals(["a/b/", "a/c/"]), result)

    def test_returns_a_tuple(self):
        self.assertIsInstance(self.index.descriptors_with_all_literals(REELS_LITERALS), tuple)

    def test_a_repeated_literal_does_not_change_the_answer(self):
        self.assertEqual(
            self.index.descriptors_with_all_literals(REELS_LITERALS * 2), ("LX/05t2;",)
        )

    def test_accepts_a_one_shot_iterator(self):
        # The parameter is `Iterable`; an implementation that walked it twice
        # would silently see an empty second pass and match everything.
        self.assertEqual(
            self.index.descriptors_with_all_literals(iter(REELS_LITERALS)), ("LX/05t2;",)
        )


class ResourceTests(IndexTestCase):
    """Resource ids are per-version: 99.1% of drawables were renumbered 430->439."""

    def setUp(self):
        super().setUp()
        self.index = self.load()

    def test_returns_the_id_for_an_indexed_type(self):
        self.assertEqual(
            self.index.resource_id("drawable", "instagram_menu_outline_24"), "0x7f0824e6"
        )
        self.assertEqual(
            self.index.resource_id("id", "action_bar_button_action"), "0x7f0b0042"
        )
        self.assertEqual(self.index.resource_id("layout", "action_bar_item"), "0x7f0e0011")

    def test_an_unknown_name_of_an_indexed_type_is_none(self):
        # The index could answer and the answer is "this version has no such
        # drawable" -- a real, usable result.
        self.assertIsNone(self.index.resource_id("drawable", "no_such_drawable"))
        self.assertIsNone(self.index.resource_id("id", ""))

    def test_an_unindexed_type_raises_rather_than_returning_none(self):
        """"Not indexed" and "not present" must not collapse into one answer.

        String ids are unresolvable under sparse resource encoding, so `None`
        here would let "this index cannot answer" pass as "the app does not have
        it" -- and `resource_id` is the one answer nothing downstream re-derives
        from the decode, so nothing would catch it.
        """
        with self.assertRaises(IndexUnusable) as caught:
            self.index.resource_id("string", "app_name")
        message = str(caught.exception)
        self.assertIn("'string'", message)
        self.assertIn("not indexed", message)
        self.assertIn("drawable, id, layout", message)
        self.assertIn("sparse resource encoding", message)

    def test_every_unindexed_type_raises_not_just_string(self):
        for resource_type in ("string", "color", "dimen", "style", "plurals", "", "DRAWABLE"):
            with self.subTest(resource_type=resource_type):
                with self.assertRaises(IndexUnusable):
                    self.index.resource_id(resource_type, "anything")

    def test_an_indexed_type_absent_from_the_surface_is_none_not_a_raise(self):
        # The header says the type was indexed, so the index can answer; it just
        # found nothing. That is a `None`, not a refusal.
        index = HookIndex.load(self.build("noresmap", resources={"drawable": {}}))
        self.assertIsNone(index.resource_id("id", "action_bar_button_action"))

    def test_an_index_with_no_resource_types_refuses_everything(self):
        index = HookIndex.load(
            self.build("nores", header_overrides={"resource_types_indexed": []})
        )
        self.assertEqual(index.resource_types, ())
        with self.assertRaises(IndexUnusable) as caught:
            index.resource_id("drawable", "instagram_menu_outline_24")
        self.assertIn("none", str(caught.exception))


class StableTypeTests(IndexTestCase):
    def setUp(self):
        super().setUp()
        self.index = self.load()

    def test_returns_the_path_of_a_non_obfuscated_class(self):
        self.assertEqual(
            self.index.stable_type_path("Lcom/instagram/app/InstagramAppShell;"),
            "smali_classes3/com/instagram/app/InstagramAppShell.smali",
        )

    def test_an_unknown_descriptor_is_none(self):
        self.assertIsNone(self.index.stable_type_path("Lcom/instagram/Nope;"))

    def test_an_obfuscated_descriptor_is_not_a_stable_type(self):
        # `LX/05t2;` is in the structural index but must never be in stable_types.
        self.assertTrue(self.index.has("LX/05t2;"))
        self.assertIsNone(self.index.stable_type_path("LX/05t2;"))

    def test_it_answers_without_reading_the_structural_index(self):
        """The point of `stable_types` is skipping the 63 MB JSONL entirely.

        Deleting the file after load proves the lookup never opened it.
        """
        directory = self.build("standalone")
        index = HookIndex.load(directory)
        (directory / STRUCTURAL_FILENAME).unlink()
        self.assertEqual(
            index.stable_type_path("Lcom/instagram/api/tigon/TigonServiceLayer;"),
            "smali_classes5/com/instagram/api/tigon/TigonServiceLayer.smali",
        )

    def test_it_agrees_with_the_structural_index_where_both_know_a_class(self):
        descriptor = "Lcom/instagram/app/InstagramAppShell;"
        self.assertEqual(self.index.stable_type_path(descriptor), self.index.path_for(descriptor))


class LazyLoadingTests(IndexTestCase):
    """`_load_rows` populates the path cache too, so the two must never disagree.

    Whichever accessor runs first decides which cache is filled. If the shared
    `_paths` cache ended up holding a different mapping depending on that order,
    `has`/`path_for` would answer differently from one run to the next for
    reasons no caller can see.
    """

    EXPECTED_PATHS = {row["descriptor"]: row["path"] for row in ROWS}

    def assert_consistent(self, index: HookIndex):
        self.assertEqual(index.class_count(), len(ROWS))
        for descriptor, path in self.EXPECTED_PATHS.items():
            self.assertTrue(index.has(descriptor))
            self.assertEqual(index.path_for(descriptor), path)
            row = index.row_for(descriptor)
            assert row is not None
            self.assertEqual(row.path, path)
        self.assertFalse(index.has("LX/99zz;"))
        self.assertIsNone(index.path_for("LX/99zz;"))
        self.assertIsNone(index.row_for("LX/99zz;"))

    def test_paths_first_then_rows_agree(self):
        index = self.load()
        self.assertEqual(index.path_for("LX/05t2;"), "smali_classes3/LX/05t2.smali")
        row = index.row_for("LX/05t2;")
        assert row is not None
        self.assertEqual(row.path, "smali_classes3/LX/05t2.smali")
        self.assert_consistent(index)

    def test_rows_first_then_paths_agree(self):
        index = self.load()
        row = index.row_for("LX/05t2;")
        assert row is not None
        self.assertEqual(row.path, "smali_classes3/LX/05t2.smali")
        self.assertEqual(index.path_for("LX/05t2;"), "smali_classes3/LX/05t2.smali")
        self.assert_consistent(index)

    def test_the_two_orders_produce_identical_answers(self):
        paths_first = self.load("a")
        paths_first.path_for("LX/05t2;")
        paths_first.row_for("LX/05t2;")
        rows_first = self.load("b")
        rows_first.row_for("LX/05t2;")
        rows_first.path_for("LX/05t2;")
        for descriptor in list(self.EXPECTED_PATHS) + ["LX/99zz;"]:
            with self.subTest(descriptor=descriptor):
                self.assertEqual(paths_first.path_for(descriptor), rows_first.path_for(descriptor))
                self.assertEqual(paths_first.has(descriptor), rows_first.has(descriptor))
        self.assertEqual(paths_first.class_count(), rows_first.class_count())

    def test_nothing_is_read_until_a_structural_lookup_is_made(self):
        # Load must not touch the 63 MB file: the resolver loads an index for
        # every version it inspects and often only reads the api surface.
        directory = self.build("lazy")
        index = HookIndex.load(directory)
        (directory / STRUCTURAL_FILENAME).unlink()
        self.assertEqual(index.decode_path, str(self.decode))
        self.assertEqual(index.descriptors_with_all_literals(REELS_LITERALS), ("LX/05t2;",))

    def test_the_path_cache_is_read_once(self):
        directory = self.build("cached")
        index = HookIndex.load(directory)
        self.assertEqual(index.class_count(), len(ROWS))
        (directory / STRUCTURAL_FILENAME).unlink()
        # Answers still come, so nothing re-opened the file.
        self.assertEqual(index.path_for("LX/05t2;"), "smali_classes3/LX/05t2.smali")
        self.assertTrue(index.has("LX/04Pn;"))
        self.assertEqual(index.class_count(), len(ROWS))

    def test_loading_rows_also_satisfies_every_path_query(self):
        """This is the coupling worth pinning: rows fill the path cache as a side
        effect, so a path query after a row query must never re-read the file."""
        directory = self.build("rowsfirst")
        index = HookIndex.load(directory)
        self.assertIsNotNone(index.row_for("LX/05t2;"))
        (directory / STRUCTURAL_FILENAME).unlink()
        self.assertEqual(index.path_for("LX/05t2;"), "smali_classes3/LX/05t2.smali")
        self.assertTrue(index.has("Lcom/instagram/app/InstagramAppShell;"))
        self.assertEqual(index.class_count(), len(ROWS))

    def test_a_path_query_does_not_satisfy_a_row_query(self):
        # The reverse coupling does not hold, and should not: `_load_paths`
        # deliberately keeps only the paths so the full rows stay unparsed.
        directory = self.build("pathsfirst")
        index = HookIndex.load(directory)
        self.assertEqual(index.path_for("LX/05t2;"), "smali_classes3/LX/05t2.smali")
        (directory / STRUCTURAL_FILENAME).unlink()
        # And an unreadable structural file reports as an unusable index rather
        # than a bare OSError, so `resolve.main`'s handler still catches it.
        with self.assertRaises(IndexUnusable):
            index.row_for("LX/05t2;")

    def test_two_readers_over_one_index_do_not_share_state(self):
        directory = self.build("shared")
        first = HookIndex.load(directory)
        second = HookIndex.load(directory)
        first.class_count()
        self.assertEqual(second.class_count(), len(ROWS))
        self.assertEqual(second.path_for("LX/05t2;"), "smali_classes3/LX/05t2.smali")


class MutationTests(IndexTestCase):
    """Each guard, re-attacked from the direction a broken implementation takes.

    A positive test proves the guard exists. These prove it bites: every one
    constructs the input that a specific plausible mutation would wave through,
    and asserts the exact outcome that mutation could not produce.
    """

    def test_a_cross_decode_index_is_refused_rather_than_answered(self):
        """Mutation: `assert_matches` compares nothing (or only warns).

        `LX/05t2;` exists in both indexes and points at a different class in
        each, so the 430 index answers a 439 question confidently and wrongly.
        The mutant returns a path; the guard raises.
        """
        decode_430 = self.decode
        decode_439 = (self.tmp / "stock-439").resolve()
        decode_439.mkdir()

        rows_439 = [dict(ROWS[0], path="smali_classes11/LX/05t2.smali", tree="smali_classes11")]
        index_430 = self.build("index-430")
        index_439 = write_index(
            self.tmp / "index-439", decode=str(decode_439), rows=rows_439
        )

        # Both indexes know the descriptor, and they disagree about it.
        self.assertEqual(
            HookIndex.load(index_430).path_for("LX/05t2;"), "smali_classes3/LX/05t2.smali"
        )
        self.assertEqual(
            HookIndex.load(index_439).path_for("LX/05t2;"), "smali_classes11/LX/05t2.smali"
        )

        # A mutant that compared nothing would hand back the 430 path here.
        with self.assertRaises(IndexUnusable) as caught:
            HookIndex.for_decode(index_430, decode_439)
        message = str(caught.exception)
        self.assertIn(str(decode_430), message)
        self.assertIn(str(decode_439), message)

        # And the guard is not simply refusing everything: the right pairings work.
        self.assertEqual(
            HookIndex.for_decode(index_430, decode_430).path_for("LX/05t2;"),
            "smali_classes3/LX/05t2.smali",
        )
        self.assertEqual(
            HookIndex.for_decode(index_439, decode_439).path_for("LX/05t2;"),
            "smali_classes11/LX/05t2.smali",
        )

    def test_a_union_would_keep_decoys_that_the_intersection_drops(self):
        """Mutation: `common | bucket` instead of `common & bucket`.

        The fixture is built so the two answers cannot coincide: three classes
        carry at least one clips literal, exactly one carries all three. A union
        returns all three, and host search would then read two unrelated classes
        and report an ambiguous host it should never have seen.
        """
        index = self.load()
        buckets = [set(index.descriptors_with_literal(literal)) for literal in REELS_LITERALS]
        union = set().union(*buckets)
        intersection = set(buckets[0]).intersection(*buckets[1:])

        self.assertEqual(union, {"LX/04Pn;", "LX/05t2;", "LX/0aOK;"})
        self.assertEqual(intersection, {"LX/05t2;"})
        self.assertNotEqual(union, intersection)  # the fixture discriminates

        result = index.descriptors_with_all_literals(REELS_LITERALS)
        self.assertEqual(result, ("LX/05t2;",))
        self.assertEqual(set(result), intersection)
        self.assertNotEqual(set(result), union)
        for decoy in ("LX/04Pn;", "LX/0aOK;"):
            with self.subTest(decoy=decoy):
                self.assertIn(decoy, union)
                self.assertNotIn(decoy, result)

    def test_a_union_would_also_survive_a_literal_that_is_absent_entirely(self):
        # The other half of the same mutation: under a union an unknown literal
        # contributes nothing and the result stays non-empty, so a dropped
        # endpoint would look like a successful match.
        index = self.load()
        literals = REELS_LITERALS + ["clips/vanished/"]
        self.assertEqual(index.descriptors_with_literal("clips/vanished/"), ())
        self.assertEqual(index.descriptors_with_all_literals(literals), ())
        # A union would have produced this instead:
        union = set().union(
            *(set(index.descriptors_with_literal(literal)) for literal in literals)
        )
        self.assertTrue(union)

    def test_an_unindexed_resource_type_cannot_be_mistaken_for_a_missing_one(self):
        """Mutation: `resource_id` returns `None` for an unindexed type.

        The two outcomes are otherwise indistinguishable at the call site -- both
        would be `None` -- so the mutant turns "this index cannot answer" into
        "the app does not have it". The fixture puts real string data in
        `resources` while leaving `string` out of `resource_types_indexed`: the
        answer is right there and the reader must still refuse it, because the
        table it came from is known to be incomplete.
        """
        index = HookIndex.load(
            self.build(
                "withstrings",
                resources=dict(RESOURCES, string={"app_name": "0x7f130001"}),
                header_overrides={"resource_types_indexed": ["drawable", "id", "layout"]},
            )
        )
        # An indexed type genuinely answers `None` for an unknown name...
        self.assertIsNone(index.resource_id("drawable", "no_such_drawable"))
        # ...so `None` for `string` would be silently identical to that.
        with self.assertRaises(IndexUnusable):
            index.resource_id("string", "app_name")
        # Even though the value is sitting in the surface file.
        with self.assertRaises(IndexUnusable):
            index.resource_id("string", "not_even_present")

    def test_the_structural_header_line_cannot_be_mistaken_for_a_class(self):
        """Mutation: drop the `readline()` that skips line 1.

        The header carries no `descriptor`, so a mutant using `row["descriptor"]`
        raises and a mutant using `.get` inserts a `None` key. Either way the
        class count moves, which is what this pins.
        """
        index = self.load()
        self.assertEqual(index.class_count(), len(ROWS))
        self.assertEqual(
            sorted(row["descriptor"] for row in ROWS),
            sorted(
                descriptor
                for descriptor in (row["descriptor"] for row in ROWS)
                if index.has(descriptor)
            ),
        )
        self.assertFalse(index.has("dfinsta.index.header"))


class RealIndexTests(unittest.TestCase):
    """The measured fact that justifies the co-location design.

    The real indexes are 70 MB each and gitignored, so this skips when they are
    absent. It reads only `api_surface.json` -- never the 63 MB structural file.
    """

    def test_the_three_reels_literals_identify_exactly_one_class_per_version(self):
        expected = {INDEX_430: "LX/05t2;", INDEX_439: "LX/04tC;"}
        missing = [
            str(directory)
            for directory in expected
            if not (directory / API_SURFACE_FILENAME).is_file()
        ]
        if missing:
            self.skipTest(f"real index not built: {', '.join(missing)}")

        found = {}
        for directory, descriptor in expected.items():
            with self.subTest(index=directory.name):
                index = HookIndex.load(directory)
                # Each literal on its own is ambiguous: analytics maps and
                # prefetch allowlists carry them too.
                for literal in REELS_LITERALS:
                    with self.subTest(literal=literal):
                        self.assertGreater(
                            len(index.descriptors_with_literal(literal)),
                            1,
                            f"{literal} is no longer ambiguous in {directory.name}; the "
                            "co-location test has stopped proving anything",
                        )
                result = index.descriptors_with_all_literals(REELS_LITERALS)
                self.assertEqual(result, (descriptor,))
                found[directory.name] = result[0]

        # The two versions name the same class differently, which is exactly why
        # a descriptor may never be carried across an index boundary.
        self.assertEqual(found, {"index-430": "LX/05t2;", "index-439": "LX/04tC;"})
        self.assertNotEqual(found["index-430"], found["index-439"])


class MalformedIndexTests(IndexTestCase):
    """Every way an index can be broken must surface as `IndexUnusable`.

    These began as characterisation tests for reported gaps and were rewritten
    when the gaps were closed. The reason they matter is one call site:
    `resolve.main` catches `IndexUnusable` around `for_decode` and exits 2, but
    the structural file is parsed lazily, *after* that try block. Anything that
    escapes as a different exception type therefore kills the Resolve stage with
    a traceback rather than a clean refusal.
    """

    def test_a_truncated_structural_file_is_reported_as_unusable(self):
        """The builder streams shards in with no atomic rename.

        An interrupted build leaves exactly this state, so it is the most likely
        real-world corruption rather than a hypothetical one.
        """
        directory = self.build(
            "badrow",
            structural_text=json.dumps(make_header(str(self.decode)))
            + "\n"
            + json.dumps(ROWS[0])
            + "\n"
            + '{"kind":"dfinsta.index.class","descrip\n',
        )
        index = HookIndex.load(directory)  # load only checks existence, by design
        with self.assertRaises(IndexUnusable) as caught:
            index.path_for("LX/05t2;")
        # The message must name the line, or a 181,000-row file is unsearchable.
        self.assertIn("structural.jsonl:3", str(caught.exception))

    def test_a_structural_row_missing_its_path_is_reported_as_unusable(self):
        directory = self.build(
            "nopath", rows=[{"kind": "dfinsta.index.class", "descriptor": "LX/05t2;"}]
        )
        index = HookIndex.load(directory)
        with self.assertRaises(IndexUnusable):
            index.class_count()

    def test_a_json_document_that_is_not_an_object_is_reported_as_unusable(self):
        # Valid JSON of the wrong shape is still malformed; without the isinstance
        # guard the first `.get` raises AttributeError past every handler.
        for filename in (HEADER_FILENAME, API_SURFACE_FILENAME):
            with self.subTest(filename=filename):
                directory = self.build(f"scalar-{filename}")
                (directory / filename).write_text("[]", encoding="utf-8")
                with self.assertRaises(IndexUnusable):
                    HookIndex.load(directory)

    def test_an_index_path_that_is_a_file_is_reported_as_unusable(self):
        # Pointing `--index` at a file rather than a directory is an easy CLI slip.
        target = self.tmp / "not-a-directory"
        target.write_text("x", encoding="utf-8")
        with self.assertRaises(IndexUnusable):
            HookIndex.load(target)

    def test_a_float_schema_version_is_rejected(self):
        # `1.0 == 1`, so a value comparison alone accepts a JSON float while
        # rejecting `0`, `2`, `"1"` and a missing key — laxer than it reads.
        with self.assertRaises(IndexUnusable):
            HookIndex.load(self.build("floatschema", header_overrides={"schema_version": 1.0}))

    def test_a_boolean_schema_version_is_rejected(self):
        # `True == 1` in Python, so bool needs excluding explicitly.
        with self.assertRaises(IndexUnusable):
            HookIndex.load(self.build("boolschema", header_overrides={"schema_version": True}))


class HeaderAccessorTests(IndexTestCase):
    def test_the_header_property_is_a_deep_copy(self):
        # A shallow copy still shares the nested `counts` dict, so a caller
        # editing a section would edit the reader's own header.
        index = self.load()
        original = index.header["counts"]["classes"]
        index.header["counts"]["classes"] = 999
        self.assertEqual(index.header["counts"]["classes"], original)

    def test_an_explicit_null_header_value_reads_as_the_empty_string(self):
        # `.get(key, "")` returns the default only for a MISSING key, so a JSON
        # null would otherwise hand a caller None from a `-> str` property.
        index = HookIndex.load(
            self.build("nulls", header_overrides={"decode_path": None, "content_hash": None})
        )
        self.assertEqual(index.decode_path, "")
        self.assertEqual(index.content_hash, "")
        # And the decode guard still fails closed in that state.
        with self.assertRaises(IndexUnusable):
            index.assert_matches(self.decode)


class KnownGapTests(IndexTestCase):
    """Characterisation tests for behaviour that is reported, not fixed.

    Each pins what the module does today. If one starts failing, the gap was
    closed and the test should be rewritten to assert the better behaviour.
    """

    def test_gap_h_a_duplicate_descriptor_silently_keeps_the_last_row(self):
        # The builder records duplicates under `anomalies` but the reader has no
        # opinion: `class_count` counts distinct descriptors, not rows.
        rows = [
            dict(ROWS[0], path="smali/first.smali"),
            dict(ROWS[0], path="smali/second.smali"),
        ]
        index = HookIndex.load(self.build("dupes", rows=rows))
        self.assertEqual(index.class_count(), 1)
        self.assertEqual(index.path_for("LX/05t2;"), "smali/second.smali")


if __name__ == "__main__":
    unittest.main()

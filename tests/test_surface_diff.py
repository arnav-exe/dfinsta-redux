"""Tests for the stage-3 API-surface diff.

The module under test exists because a class-level diff of two Instagram
versions reports "everything changed": obfuscated names are recycled, and 99.1%
of drawable ids are renumbered between 430 and 439. Almost every test here is
therefore built to make a *wrong layer* provably different from the right one,
rather than merely to exercise the right one — a fixture where names and ids
agree, or where descriptors happen not to churn, would pass under either
implementation and prove nothing.

Fixtures are synthetic three-file indexes in a temp directory, written by
`tests/test_hook_index.py`'s own `write_index` so both suites stay bound to the
one shape `tools/indexer/build_index.py` emits. The real 70 MB indexes are
gitignored, so only `RealIndexTests` touches them and it skips when they are
absent.

`MutationTests` re-attacks the guards from the direction a broken implementation
would take. The load-bearing one is the resource diff: the fixture there is
constructed so that diffing by id instead of by name changes the answer from
"nothing changed" to "90% of drawables changed", which is the miniature of the
measured 99.1%.
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from dfinsta_pipeline.hook_index import IndexUnusable
from dfinsta_pipeline.surface_diff import (
    BRANCH_ENDPOINT,
    BRANCH_INLINE,
    BRANCH_UNKNOWN,
    UNKNOWN_FAMILY,
    BlockedSurface,
    Candidate,
    CategoryDiff,
    SurfaceDiff,
    SurfaceDiffError,
    SurfaceSnapshot,
    classify_candidate,
    diff_surfaces,
    endpoint_family,
    is_endpoint_path,
    looks_feature_bearing,
    main,
    normalise_literal,
    package_prefix,
    summary_lines,
)
from tests.test_hook_index import write_index

ROOT = Path(__file__).resolve().parents[1]
INDEX_430 = ROOT / "work" / "index-430"
INDEX_439 = ROOT / "work" / "index-439"
REAL_MANIFEST = ROOT / "manifest" / "hooks.json"


# --------------------------------------------------------------------- fixture

# A miniature of the real 430 -> 439 move, built so that every wrong layer gives
# a different answer from the right one:
#
#   clips/discover/            2 classes -> 2 classes, EVERY descriptor renamed
#   feed/timeline/             1 class   -> 1 class,   descriptor renamed
#   shopping/onboard/          1 class   -> 3 classes  (the Shopping dissolution)
#   discover/topical_explore/  removed
#   clips/homecoming/          added
#
# So a descriptor-set comparison reports all four shared literals as changed,
# while the count comparison reports exactly the one that really moved.
BASELINE_PATHS = {
    "clips/discover/": ["LX/04Pn;", "LX/05t2;"],
    "feed/timeline/": ["LX/05t2;"],
    "shopping/onboard/": ["LX/0aOK;"],
    "discover/topical_explore/": ["LX/04Pn;", "LX/0aOK;"],
}
TARGET_PATHS = {
    "clips/discover/": ["LX/0Di2;", "LX/0DnT;"],
    "feed/timeline/": ["LX/0Di2;"],
    "shopping/onboard/": ["LX/04tC;", "LX/0Di2;", "LX/0DnT;"],
    "clips/homecoming/": ["LX/0Di2;"],
}

BASELINE_TYPES = [
    "Lcom/instagram/clips/ClipsViewerFragment;",
    "Lcom/instagram/feed/FeedFragment;",
    "Lcom/facebook/rsys/call/Foo;",
]
TARGET_TYPES = [
    "Lcom/instagram/clips/ClipsViewerFragment;",
    "Lcom/instagram/quicksnap/QuickSnapFragment;",
    "Lcom/instagram/quicksnap/QuickSnapFragment$1;",
    "Lcom/instagram/quicksnap/QuickSnapFragment$$ExternalSyntheticLambda0;",
    "Lcom/facebook/rsys/call/Foo;",
]

# `ic_reels` keeps its NAME and loses its id; `ic_shop` keeps both. Diffed by
# name that is one shared pair plus one add and one drop; diffed by id it is a
# near-total rewrite.
BASELINE_DRAWABLES = {
    "ic_reels": "0x7f080001",
    "ic_shop": "0x7f080002",
    "ic_gone": "0x7f080003",
}
TARGET_DRAWABLES = {
    "ic_reels": "0x7f0801ff",
    "ic_shop": "0x7f080002",
    "ic_new": "0x7f080300",
}

# Blocks the same families the real manifest does, written in the manifest's own
# spelling (`/feed/timeline/`) rather than the index's (`feed/timeline/`).
MANIFEST_HOOKS = [
    {
        "hook_id": "tigon_url_block",
        "semantic_deps": ["/feed/timeline/", "/api/v1/clips/homecoming/"],
        "status": "active",
    },
    {
        "hook_id": "set_app_context",
        "semantic_deps": ["Landroid/app/Application;->onCreate()V"],
        "status": "active",
    },
]


class SurfaceTestCase(unittest.TestCase):
    """Gives every test its own temp root, index builder and manifest writer."""

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)

    def build(
        self,
        name: str,
        *,
        api_paths: dict | None = None,
        stable_types: list | None = None,
        resources: dict | None = None,
        resource_types: tuple = ("drawable",),
    ) -> Path:
        """Write one synthetic index in the three-file shape the indexer emits."""
        decode = (self.tmp / name).resolve()
        decode.mkdir(parents=True, exist_ok=True)
        return write_index(
            self.tmp / f"index-{name}",
            decode=str(decode),
            header_overrides={"resource_types_indexed": list(resource_types)},
            api_paths=dict(api_paths or {}),
            # The value is the class's path; nothing in this stage reads it.
            stable_types={
                descriptor: "smali/x.smali" for descriptor in (stable_types or [])
            },
            resources=dict(resources or {}),
            rows=[],
        )

    def snapshot(self, name: str, **kwargs) -> SurfaceSnapshot:
        return SurfaceSnapshot.load(self.build(name, **kwargs))

    def baseline(self, **overrides) -> SurfaceSnapshot:
        return self.snapshot(
            overrides.pop("name", "stock-430"),
            api_paths=overrides.pop("api_paths", BASELINE_PATHS),
            stable_types=overrides.pop("stable_types", BASELINE_TYPES),
            resources=overrides.pop("resources", {"drawable": BASELINE_DRAWABLES}),
            **overrides,
        )

    def target(self, **overrides) -> SurfaceSnapshot:
        return self.snapshot(
            overrides.pop("name", "stock-439"),
            api_paths=overrides.pop("api_paths", TARGET_PATHS),
            stable_types=overrides.pop("stable_types", TARGET_TYPES),
            resources=overrides.pop("resources", {"drawable": TARGET_DRAWABLES}),
            **overrides,
        )

    def write_manifest(self, hooks=None, *, name: str = "hooks.json", schema: int = 1) -> Path:
        path = self.tmp / name
        path.write_text(
            json.dumps(
                {"schema_version": schema, "hooks": MANIFEST_HOOKS if hooks is None else hooks}
            ),
            encoding="utf-8",
        )
        return path

    def diff(self, *, blocked=None, **overrides) -> SurfaceDiff:
        return diff_surfaces(self.baseline(), self.target(), blocked)


# --------------------------------------------------------------------- families


class EndpointFamilyTests(unittest.TestCase):
    """Grouping is what turns N unrelated strings into a readable report.

    Without it a human reads 44 separate Reels deep links and 4 separate creator
    endpoints as 48 findings instead of two feature areas.
    """

    def test_the_first_named_segment_is_the_family(self):
        self.assertEqual(endpoint_family("clips/discover/"), "clips")
        self.assertEqual(endpoint_family("feed/timeline/"), "feed")
        self.assertEqual(endpoint_family("discover/topical_explore/"), "discover")
        self.assertEqual(endpoint_family("profile_ads/get_profile_ads/"), "profile_ads")

    def test_the_api_version_prefix_is_stripped(self):
        """Every endpoint carries `api/v1`, so it groups nothing.

        The manifest writes `/api/v1/clips/homecoming/` and the smali constant is
        `clips/homecoming/`; both must land in one family or the manifest's own
        blocked endpoints would look like a different area of the app.
        """
        self.assertEqual(endpoint_family("/api/v1/clips/homecoming/"), "clips")
        self.assertEqual(endpoint_family("api/v2/feed/timeline/"), "feed")
        self.assertEqual(
            endpoint_family("/api/v1/clips/homecoming/"), endpoint_family("clips/homecoming/")
        )

    def test_placeholder_segments_are_skipped(self):
        # `%s` and `{clip_id}` are formatting holes, not feature names.
        self.assertEqual(endpoint_family("video/refresh_resources/%s/"), "video")
        self.assertEqual(endpoint_family("text_feed/{post_id}/replies_in_ig/"), "text_feed")
        self.assertEqual(endpoint_family("{user_name}/reels/{clip_id}"), "reels")
        self.assertEqual(endpoint_family("1/graphqlsubscriptions"), "graphqlsubscriptions")

    def test_a_web_host_is_not_a_family_but_an_app_scheme_destination_is(self):
        """`www.instagram.com` describes no feature; `ig://clips_home` is entirely a feature name."""
        self.assertEqual(endpoint_family("https://www.instagram.com/reels/videos/{clip_id}"), "reels")
        self.assertEqual(endpoint_family("http://instagram.com/reel/{clip_id}"), "reel")
        self.assertEqual(endpoint_family("ig://clips_home"), "clips_home")
        self.assertEqual(endpoint_family("instagram://airwave_co_watching_join"), "airwave_co_watching_join")

    def test_related_endpoints_cluster_under_one_family(self):
        # The whole point: these five strings are one product surface.
        clips = {
            "clips/discover/",
            "clips/discover/stream/",
            "clips/homecoming/",
            "clips/discover/interest/stream/",
            "/api/v1/clips/homecoming/",
        }
        self.assertEqual({endpoint_family(literal) for literal in clips}, {"clips"})

    def test_a_type_descriptor_or_method_reference_has_no_family(self):
        """A manifest `semantic_deps` entry may be a method, not a path.

        Giving it a family would put an unrelated literal in the same bucket as
        a lifecycle hook and make it read as already blocked.
        """
        self.assertEqual(endpoint_family("Landroid/app/Application;->onCreate()V"), UNKNOWN_FAMILY)
        self.assertEqual(endpoint_family("Lcom/instagram/app/InstagramAppShell;"), UNKNOWN_FAMILY)

    def test_anything_that_is_not_a_path_is_unknown(self):
        for value in ("", "   ", "no-slashes", "/", "///", None, 42, "%s/{x}/"):
            with self.subTest(value=value):
                self.assertEqual(endpoint_family(value), UNKNOWN_FAMILY)


class EndpointPathTests(unittest.TestCase):
    """`is_endpoint_path` is the whole A/B split, so its errors have a cost.

    A false positive tells a human "one line in throwIfBlocked" about something
    that is not an endpoint. A false negative only says "look at this yourself".
    These pin the bias in the second direction.
    """

    def test_a_two_segment_trailing_slash_path_is_an_endpoint(self):
        for literal in (
            "clips/discover/",
            "feed/timeline/",
            "profile_ads/get_profile_ads/",
            "/media/preset/",
            "business/branded_content/update_branded_content_opt_in_status/",
        ):
            with self.subTest(literal=literal):
                self.assertTrue(is_endpoint_path(literal))

    def test_anything_under_api_vn_is_an_endpoint_even_without_two_more_segments(self):
        self.assertTrue(is_endpoint_path("api/v1/fan_club/categories_metadata/"))
        self.assertTrue(is_endpoint_path("/api/v1/clips/homecoming/"))
        self.assertTrue(is_endpoint_path("https://i.instagram.com/api/v1/feed/timeline/"))

    def test_app_scheme_deep_links_are_not_endpoints(self):
        """`ig://reels_home` opens a screen; it never becomes an outgoing request.

        Reporting it as branch A would promise a URL rule that has nothing to
        match against.
        """
        for literal in ("ig://reels_home", "instagram://open_poll_note_creation", "airwave://home"):
            with self.subTest(literal=literal):
                self.assertFalse(is_endpoint_path(literal))

    def test_web_urls_without_an_api_path_are_not_endpoints(self):
        for literal in (
            "https://www.instagram.com/reels_home",
            "https://help.instagram.com/1078739702690433",
            "http://schemas.android.com/apk/res/android",
        ):
            with self.subTest(literal=literal):
                self.assertFalse(is_endpoint_path(literal))

    def test_file_paths_and_cache_directories_are_not_endpoints(self):
        """The measured shape of the no-trailing-slash bucket is filesystem noise.

        `cache/...`, `/proc/...` and image assets outnumber real endpoints there,
        which is why the trailing slash is required of a bare relative path.
        """
        for literal in (
            "cache/plog_crash_resilience",
            "/mock/c2pa/fallback_source_media.jpg",
            "adb/magisk.img",
            "files/one_cache_path",
            "/proc/self/oom_score_adj",
        ):
            with self.subTest(literal=literal):
                self.assertFalse(is_endpoint_path(literal))

    def test_a_single_segment_path_is_not_an_endpoint(self):
        # `/dialog/` and `dogfood_feedback/` name an area, not a request path.
        self.assertFalse(is_endpoint_path("/dialog/"))
        self.assertFalse(is_endpoint_path("dogfood_feedback/"))

    def test_instagram_writes_both_spellings_and_only_the_slashed_one_is_the_request_path(self):
        # Both `feed/timeline` and `feed/timeline/` are in the real index; the
        # first is used as a name, the second is the path that is fetched.
        self.assertTrue(is_endpoint_path("feed/timeline/"))
        self.assertFalse(is_endpoint_path("feed/timeline"))


class NormaliseLiteralTests(unittest.TestCase):
    def test_manifest_and_index_spellings_of_one_path_compare_equal(self):
        """Without this the manifest's own blocked endpoints look absent.

        The manifest writes what the outgoing URI looks like (`/feed/timeline/`);
        the smali constant has no leading slash.
        """
        self.assertEqual(normalise_literal("/feed/timeline/"), normalise_literal("feed/timeline"))
        self.assertEqual(normalise_literal("  clips/discover/  "), "clips/discover")


# ---------------------------------------------------------------- stable types


class StableTypeGroupingTests(unittest.TestCase):
    def test_the_package_prefix_never_includes_the_class_name(self):
        """Otherwise every class becomes its own group and grouping does nothing."""
        self.assertEqual(package_prefix("Lcom/instagram/clips/intf/Foo;"), "com/instagram/clips")
        self.assertEqual(package_prefix("Lcom/instagram/Foo;"), "com/instagram")
        self.assertEqual(package_prefix("Lcom/facebook/rsys/call/Bar;"), "com/facebook/rsys")
        self.assertEqual(package_prefix("Lkotlin/Unit;"), "kotlin")

    def test_a_depth_can_be_chosen(self):
        self.assertEqual(package_prefix("Lcom/instagram/clips/intf/Foo;", depth=2), "com/instagram")

    def test_synthetic_and_inner_classes_are_not_feature_bearing(self):
        """They churn with every recompile and say nothing about what the app gained.

        1,569 stable types were added between 430 and 439; without this filter
        the "what is new" list is dominated by lambdas.
        """
        for descriptor in (
            "Lcom/instagram/quicksnap/QuickSnapFragment$1;",
            "Lcom/instagram/quicksnap/QuickSnapFragment$$ExternalSyntheticLambda0;",
            "Lcom/instagram/quicksnap/QuickSnapFragment$Companion;",
        ):
            with self.subTest(descriptor=descriptor):
                self.assertFalse(looks_feature_bearing(descriptor))

    def test_only_instagram_types_are_feature_bearing(self):
        # com/facebook and androidx are infrastructure the mod never targets.
        self.assertTrue(looks_feature_bearing("Lcom/instagram/quicksnap/QuickSnapFragment;"))
        self.assertFalse(looks_feature_bearing("Lcom/facebook/rsys/call/Foo;"))
        self.assertFalse(looks_feature_bearing("Landroidx/camera/camera2/Bar;"))
        self.assertFalse(looks_feature_bearing("LX/05t2;"))

    def test_build_glue_is_not_feature_bearing(self):
        self.assertFalse(looks_feature_bearing("Lcom/instagram/app/R;"))
        self.assertFalse(looks_feature_bearing("Lcom/instagram/app/BuildConfig;"))
        self.assertFalse(looks_feature_bearing(""))


# ------------------------------------------------------------------- snapshots


class SnapshotTests(SurfaceTestCase):
    def test_a_snapshot_loads_from_an_index_directory(self):
        snapshot = self.baseline()
        self.assertEqual(snapshot.label, "stock-430")
        self.assertEqual(snapshot.counts()["api_paths"], len(BASELINE_PATHS))
        self.assertEqual(snapshot.counts()["stable_types"], len(BASELINE_TYPES))
        self.assertEqual(snapshot.resource_types, ("drawable",))

    def test_a_broken_index_is_unusable_rather_than_empty(self):
        """Validation is delegated to HookIndex so it fails the same way everywhere.

        An index that silently loaded as empty would report the new version as
        having deleted its entire API surface.
        """
        directory = self.build("stock-430")
        (directory / "api_surface.json").write_text("{", encoding="utf-8")
        with self.assertRaises(IndexUnusable):
            SurfaceSnapshot.load(directory)
        with self.assertRaises(IndexUnusable):
            SurfaceSnapshot.load(self.tmp / "never-built")

    def test_class_count_is_per_version_and_literals_invert(self):
        snapshot = self.baseline()
        self.assertEqual(snapshot.class_count("clips/discover/"), 2)
        self.assertEqual(snapshot.class_count("nothing/here/"), 0)
        self.assertEqual(
            snapshot.literals_in("LX/05t2;"), frozenset({"clips/discover/", "feed/timeline/"})
        )
        self.assertEqual(snapshot.literals_in("LX/99zz;"), frozenset())

    def test_an_unindexed_resource_type_raises_rather_than_reading_as_empty(self):
        """Mirrors `HookIndex.resource_id`: "not indexed" is not "not present".

        At diff scale the difference is not subtle — an empty set would report
        every drawable in the other version as added or removed.
        """
        snapshot = self.baseline()
        with self.assertRaises(IndexUnusable) as caught:
            snapshot.resource_names("string")
        self.assertIn("not indexed", str(caught.exception))
        self.assertEqual(snapshot.resource_names("drawable"), frozenset(BASELINE_DRAWABLES))

    def test_resource_id_stability_measures_the_renumbering(self):
        """The 99.1% figure has to be recomputed, not quoted.

        Here one of the two shared names kept its id, so the answer is 0.5 and
        the diff itself still reports the names as unchanged.
        """
        self.assertEqual(self.baseline().resource_id_stability("drawable", self.target()), 0.5)
        self.assertIsNone(self.baseline().resource_id_stability("layout", self.target()))

    def test_the_snapshot_reports_only_identity_and_sizes(self):
        # Contents are the diff's job; the snapshot's dict must not smuggle a
        # per-version descriptor into a cross-version report.
        payload = json.dumps(self.baseline().to_dict())
        self.assertNotIn("LX/", payload)
        self.assertIn("stock-430", payload)


# ------------------------------------------------------------------- the diff


class ApiPathDiffTests(SurfaceTestCase):
    """The added/removed literal list is the primary signal of a new feature."""

    def setUp(self):
        super().setUp()
        self.result = self.diff()

    def test_added_and_removed_literals_are_reported(self):
        self.assertEqual(self.result.api_paths.added, ("clips/homecoming/",))
        self.assertEqual(self.result.api_paths.removed, ("discover/topical_explore/",))

    def test_a_literal_present_in_both_versions_is_neither_added_nor_removed(self):
        # Even though every class carrying it was renamed.
        for literal in ("clips/discover/", "feed/timeline/", "shopping/onboard/"):
            with self.subTest(literal=literal):
                self.assertNotIn(literal, self.result.api_paths.added)
                self.assertNotIn(literal, self.result.api_paths.removed)

    def test_the_lists_are_sorted_so_two_runs_produce_the_same_report(self):
        self.assertEqual(list(self.result.api_paths.added), sorted(self.result.api_paths.added))
        self.assertEqual(list(self.result.api_paths.removed), sorted(self.result.api_paths.removed))

    def test_counts_describe_both_versions(self):
        self.assertEqual(self.result.api_paths.baseline_count, 4)
        self.assertEqual(self.result.api_paths.target_count, 4)
        self.assertEqual(self.result.api_paths.shared_count, 3)


class StableTypeDiffTests(SurfaceTestCase):
    def setUp(self):
        super().setUp()
        self.result = self.diff()

    def test_packages_that_gained_or_lost_types_are_reported(self):
        moved = {delta.prefix: delta for delta in self.result.package_deltas}
        self.assertEqual(moved["com/instagram/quicksnap"].added, 3)
        self.assertEqual(moved["com/instagram/quicksnap"].removed, 0)
        self.assertEqual(moved["com/instagram/feed"].removed, 1)

    def test_a_package_that_did_not_move_is_not_reported(self):
        """The report is what CHANGED; unchanged packages are the other 17,000."""
        moved = {delta.prefix for delta in self.result.package_deltas}
        self.assertNotIn("com/instagram/clips", moved)
        self.assertNotIn("com/facebook/rsys", moved)

    def test_only_feature_bearing_added_types_are_singled_out(self):
        """Three types were added to one package; only one names a feature."""
        self.assertEqual(len(self.result.stable_types.added), 3)
        self.assertEqual(
            self.result.added_feature_types,
            ("Lcom/instagram/quicksnap/QuickSnapFragment;",),
        )

    def test_packages_are_ordered_by_churn(self):
        churn = [delta.churn for delta in self.result.package_deltas]
        self.assertEqual(churn, sorted(churn, reverse=True))


class ColocationTests(SurfaceTestCase):
    """The Shopping case: a literal that stays but stops being one blockable thing.

    An added/removed diff cannot see this at all -- the literal is on both sides.
    """

    def setUp(self):
        super().setUp()
        self.result = self.diff()

    def test_a_literal_that_spread_across_more_classes_is_reported(self):
        changes = {change.literal: change for change in self.result.colocation_changes}
        self.assertIn("shopping/onboard/", changes)
        change = changes["shopping/onboard/"]
        self.assertEqual((change.baseline_classes, change.target_classes), (1, 3))
        self.assertEqual(change.delta, 2)
        self.assertEqual(change.direction, "spread")

    def test_a_literal_that_concentrated_is_reported_in_the_other_direction(self):
        result = diff_surfaces(
            self.baseline(api_paths={"a/b/": ["LX/01;", "LX/02;", "LX/03;"]}),
            self.target(api_paths={"a/b/": ["LX/99;"]}),
        )
        self.assertEqual(len(result.colocation_changes), 1)
        change = result.colocation_changes[0]
        self.assertEqual(change.direction, "concentrated")
        self.assertEqual(change.delta, -2)

    def test_a_literal_whose_class_count_held_is_not_reported(self):
        """Even though every descriptor carrying it changed.

        `clips/discover/` moved from {LX/04Pn;, LX/05t2;} to {LX/0Di2;, LX/0DnT;}
        -- a set comparison calls that a change, and it is nothing but the
        obfuscator doing what it does every release.
        """
        reported = {change.literal for change in self.result.colocation_changes}
        self.assertNotIn("clips/discover/", reported)
        self.assertNotIn("feed/timeline/", reported)

    def test_only_literals_present_in_both_versions_are_considered(self):
        # An added literal is already in `api_paths.added`; reporting it again as
        # "co-location changed from 0" would double-count it.
        reported = {change.literal for change in self.result.colocation_changes}
        self.assertNotIn("clips/homecoming/", reported)
        self.assertNotIn("discover/topical_explore/", reported)

    def test_changes_carry_a_family_so_they_cluster_like_candidates_do(self):
        changes = {change.literal: change for change in self.result.colocation_changes}
        self.assertEqual(changes["shopping/onboard/"].family, "shopping")

    def test_the_biggest_movers_come_first(self):
        deltas = [abs(change.delta) for change in self.result.colocation_changes]
        self.assertEqual(deltas, sorted(deltas, reverse=True))


class ResourceDiffTests(SurfaceTestCase):
    """Resources are diffed BY NAME. 99.1% of shared drawable ids were renumbered."""

    def setUp(self):
        super().setUp()
        self.result = self.diff()

    def test_resources_are_diffed_by_name(self):
        drawable = self.result.resources["drawable"]
        self.assertEqual(drawable.added, ("ic_new",))
        self.assertEqual(drawable.removed, ("ic_gone",))
        self.assertEqual(drawable.shared_count, 2)

    def test_a_renumbered_name_is_not_a_change(self):
        """`ic_reels` kept its name and lost its id; the diff must say nothing.

        This is the single fact the whole resource design rests on: an anchor is
        pinned by name and the id is re-resolved per version.
        """
        drawable = self.result.resources["drawable"]
        self.assertNotIn("ic_reels", drawable.added)
        self.assertNotIn("ic_reels", drawable.removed)
        self.assertNotEqual(BASELINE_DRAWABLES["ic_reels"], TARGET_DRAWABLES["ic_reels"])

    def test_id_stability_is_reported_alongside_but_never_diffed(self):
        self.assertEqual(self.result.resource_id_stability["drawable"], 0.5)

    def test_a_type_indexed_on_only_one_side_is_skipped_not_diffed(self):
        """"Not indexed" would otherwise read as "the app deleted all of them".

        String ids are the real case: they are unresolvable under sparse
        resource encoding, so an index that stopped indexing a type must not
        make the next version look like it dropped the type.
        """
        result = diff_surfaces(
            self.baseline(resource_types=("drawable", "layout")),
            self.target(resource_types=("drawable",)),
        )
        self.assertIn("drawable", result.resources)
        self.assertNotIn("layout", result.resources)
        self.assertIn("layout", result.skipped_resource_types)
        # The reason has to name the version that cannot answer, or "skipped"
        # is unactionable in a report covering three resource types.
        self.assertIn("stock-439", result.skipped_resource_types["layout"])
        self.assertIn("not an empty one", result.skipped_resource_types["layout"])


class SurvivalRateTests(SurfaceTestCase):
    """The measured percentages must be recomputed by the pipeline, not quoted."""

    def test_survival_is_shared_over_baseline(self):
        result = self.diff()
        self.assertEqual(result.api_paths.survival_rate, 3 / 4)
        self.assertEqual(result.stable_types.survival_rate, 2 / 3)
        self.assertEqual(result.resources["drawable"].survival_rate, 2 / 3)

    def test_the_flat_view_names_every_category(self):
        rates = self.diff().survival_rates()
        self.assertEqual(
            set(rates), {"api_paths", "stable_types", "resources.drawable"}
        )

    def test_an_empty_baseline_has_no_survival_rate_rather_than_zero(self):
        """0/0 is neither a wipeout nor perfect survival.

        Reporting 0.0 would make a first-ever run, or a category the baseline
        never indexed, look like the app deleted the entire layer.
        """
        empty = CategoryDiff.between("api_paths", [], ["a/b/"])
        self.assertIsNone(empty.survival_rate)
        self.assertEqual(empty.added, ("a/b/",))

    def test_identical_input_survives_completely(self):
        self.assertEqual(CategoryDiff.between("x", ["a", "b"], ["a", "b"]).survival_rate, 1.0)

    def test_a_total_replacement_survives_not_at_all(self):
        self.assertEqual(CategoryDiff.between("x", ["a"], ["b"]).survival_rate, 0.0)


class EmptyDiffTests(SurfaceTestCase):
    def test_two_identical_indexes_produce_an_empty_diff(self):
        """A version compared with itself must report nothing, including no
        co-location noise -- the same-version case is how a run proves the
        differ is not inventing changes."""
        result = diff_surfaces(self.baseline(), self.baseline(name="stock-430-copy"))
        self.assertTrue(result.empty)
        self.assertEqual(result.api_paths.added, ())
        self.assertEqual(result.api_paths.removed, ())
        self.assertEqual(result.colocation_changes, ())
        self.assertEqual(result.package_deltas, ())
        self.assertEqual(result.candidates, ())
        self.assertEqual(result.api_paths.survival_rate, 1.0)

    def test_two_empty_indexes_produce_an_empty_diff_and_no_rates(self):
        result = diff_surfaces(
            self.snapshot("empty-a", api_paths={}, stable_types=[], resources={"drawable": {}}),
            self.snapshot("empty-b", api_paths={}, stable_types=[], resources={"drawable": {}}),
        )
        self.assertTrue(result.empty)
        self.assertIsNone(result.api_paths.survival_rate)
        self.assertIsNone(result.resource_id_stability["drawable"])
        self.assertEqual(result.candidates, ())

    def test_a_real_change_is_not_empty(self):
        # Proves `empty` is not simply always True.
        self.assertFalse(self.diff().empty)

    def test_a_category_that_could_not_be_compared_is_not_an_empty_diff(self):
        """"Nothing changed" and "nothing changed among what we could look at"
        are different claims, and a gate may only be skipped on the first."""
        result = diff_surfaces(
            self.baseline(
                resources={"drawable": BASELINE_DRAWABLES, "layout": {"a": "0x1"}},
                resource_types=("drawable", "layout"),
            ),
            self.baseline(
                name="stock-430-copy",
                resources={"drawable": BASELINE_DRAWABLES},
                resource_types=("drawable",),
            ),
        )
        self.assertEqual(result.api_paths.added, ())
        self.assertEqual(result.resources["drawable"].added, ())
        self.assertFalse(result.empty)


class InputTypeTests(SurfaceTestCase):
    def test_index_directories_and_snapshots_are_both_accepted(self):
        """The CLI has paths; later stages and tests have snapshots."""
        baseline_dir = self.build("stock-430", api_paths=BASELINE_PATHS)
        target_dir = self.build("stock-439", api_paths=TARGET_PATHS)
        from_paths = diff_surfaces(baseline_dir, target_dir)
        from_strings = diff_surfaces(str(baseline_dir), str(target_dir))
        from_snapshots = diff_surfaces(
            SurfaceSnapshot.load(baseline_dir), SurfaceSnapshot.load(target_dir)
        )
        self.assertEqual(from_paths.api_paths.added, from_strings.api_paths.added)
        self.assertEqual(from_paths.api_paths.added, from_snapshots.api_paths.added)

    def test_a_hook_index_is_refused_with_an_explanation(self):
        """It cannot enumerate a surface, so it would silently diff nothing."""
        from dfinsta_pipeline.hook_index import HookIndex

        index = HookIndex.load(self.build("stock-430", api_paths=BASELINE_PATHS))
        with self.assertRaises(TypeError) as caught:
            diff_surfaces(index, index)
        self.assertIn("SurfaceSnapshot", str(caught.exception))

    def test_anything_else_is_a_type_error(self):
        with self.assertRaises(TypeError):
            diff_surfaces(None, None)


# ---------------------------------------------------------------- the manifest


class BlockedSurfaceTests(SurfaceTestCase):
    """What DFInsta already blocks is read from the manifest, never hardcoded."""

    def test_families_come_from_semantic_deps(self):
        blocked = BlockedSurface.from_manifest(self.write_manifest())
        self.assertEqual(blocked.families, {"feed", "clips"})
        self.assertEqual(blocked.hooks_by_family["clips"], ("tigon_url_block",))

    def test_a_manifest_spelling_matches_the_index_spelling(self):
        blocked = BlockedSurface.from_manifest(self.write_manifest())
        self.assertTrue(blocked.matches("feed/timeline"))
        self.assertEqual(blocked.match_kind("feed/timeline/"), "literal")

    def test_a_different_endpoint_in_the_same_area_matches_by_family(self):
        """`clips/discover/interest/stream/` is not in the manifest and is still
        the surface the manifest is about; saying so is the point of families."""
        blocked = BlockedSurface.from_manifest(self.write_manifest())
        self.assertEqual(blocked.match_kind("clips/discover/interest/stream/"), "family")
        self.assertEqual(blocked.hooks_for("clips/discover/interest/stream/"), ("tigon_url_block",))

    def test_a_method_reference_dep_creates_no_family(self):
        """`set_app_context` depends on `Application;->onCreate()V`.

        Folding that into a family would make unrelated literals look blocked by
        the lifecycle hook.
        """
        blocked = BlockedSurface.from_manifest(self.write_manifest())
        self.assertNotIn(UNKNOWN_FAMILY, blocked.families)
        self.assertEqual(blocked.hooks_for("Landroid/app/Application;->onCreate()V"), ("set_app_context",))

    def test_an_inactive_hook_blocks_nothing_but_is_recorded(self):
        """A dropped hook is not coverage. Counting it would report a family as
        handled when nothing handles it any more."""
        blocked = BlockedSurface.from_manifest(
            self.write_manifest(
                [
                    {"hook_id": "old_shopping", "semantic_deps": ["/shopping/"], "status": "dropped@1.4.1"},
                    {"hook_id": "live", "semantic_deps": ["/feed/timeline/"]},
                ]
            )
        )
        self.assertEqual(blocked.families, {"feed"})
        self.assertEqual(blocked.inactive_hooks, ("old_shopping",))

    def test_a_hook_with_no_status_is_active(self):
        blocked = BlockedSurface.from_manifest(
            self.write_manifest([{"hook_id": "live", "semantic_deps": ["/feed/timeline/"]}])
        )
        self.assertEqual(blocked.families, {"feed"})

    def test_a_manifest_that_cannot_be_understood_is_an_error(self):
        for label, payload in (
            ("missing", None),
            ("malformed", "{not json"),
            ("scalar", "[]"),
            ("wrong schema", json.dumps({"schema_version": 99, "hooks": []})),
            ("no hooks", json.dumps({"schema_version": 1})),
            ("hook is a scalar", json.dumps({"schema_version": 1, "hooks": ["x"]})),
            # A bare string iterates one character at a time and would register
            # a family per letter instead of failing.
            (
                "deps are a string",
                json.dumps({"schema_version": 1, "hooks": [{"hook_id": "x", "semantic_deps": "/feed/"}]}),
            ),
        ):
            with self.subTest(label=label):
                path = self.tmp / f"bad-{label}.json"
                if payload is not None:
                    path.write_text(payload, encoding="utf-8")
                with self.assertRaises(SurfaceDiffError):
                    BlockedSurface.from_manifest(path)

    def test_the_real_manifest_reads_if_it_is_present(self):
        """Pins the reader against the file it actually consumes.

        A lighter read than `hook_manifest.load_manifest` is only defensible if
        it still reads the real thing.
        """
        if not REAL_MANIFEST.is_file():  # pragma: no cover - repo layout
            self.skipTest("manifest/hooks.json is absent")
        blocked = BlockedSurface.from_manifest(REAL_MANIFEST)
        self.assertEqual(blocked.families, {"feed", "discover", "profile_ads", "clips", "delivery"})


class ClassifyCandidateTests(SurfaceTestCase):
    """Classification reads the manifest. It must not know any family by heart."""

    def test_it_reads_semantic_deps_rather_than_a_built_in_list(self):
        """A manifest that blocks an invented family must drive the answer, and
        a manifest that does NOT mention clips must not claim clips is covered.

        Both halves are needed: the first fails if the list is hardcoded, the
        second fails if a hardcoded list is merely being unioned in.
        """
        blocked = BlockedSurface.from_manifest(
            self.write_manifest([{"hook_id": "narwhal_block", "semantic_deps": ["/narwhal/tank/"]}])
        )
        result = diff_surfaces(
            self.baseline(api_paths={}),
            self.target(api_paths={"narwhal/tank/": ["LX/01;"], "clips/discover/": ["LX/02;"]}),
            blocked,
        )
        invented = classify_candidate("narwhal/tank/", result)
        self.assertTrue(invented.maps_to_blocked_family)
        self.assertEqual(invented.blocked_by, ("narwhal_block",))
        real = classify_candidate("clips/discover/", result)
        self.assertFalse(real.maps_to_blocked_family)
        self.assertEqual(real.blocked_by, ())

    def test_without_a_manifest_coverage_is_unknown_rather_than_false(self):
        """"No manifest was supplied" and "DFInsta blocks nothing here" are
        different answers, and only one of them is safe to act on."""
        candidate = classify_candidate("clips/homecoming/", self.diff())
        self.assertIsNone(candidate.maps_to_blocked_family)
        self.assertEqual(candidate.blocked_by, ())
        self.assertEqual(candidate.match_kind, "")

    def test_a_candidate_carries_its_family_and_how_many_classes_hold_it(self):
        result = self.diff()
        candidate = classify_candidate("shopping/onboard/", result)
        self.assertEqual(candidate.family, "shopping")
        self.assertEqual(candidate.classes, 3)

    def test_a_candidate_reports_a_class_count_and_never_a_descriptor(self):
        """Descriptors are recycled across versions, so a count travels and a
        name is a trap. Serialising one here is how it would escape."""
        payload = json.dumps(classify_candidate("shopping/onboard/", self.diff()).to_dict())
        self.assertNotIn("LX/", payload)
        self.assertIn('"classes": 3', payload)

    def test_every_added_literal_is_classified(self):
        result = self.diff()
        self.assertEqual([c.literal for c in result.candidates], list(result.api_paths.added))
        self.assertTrue(all(isinstance(c, Candidate) for c in result.candidates))

    def test_candidates_group_by_family(self):
        result = self.diff()
        self.assertEqual(set(result.candidates_by_family()), {"clips"})


class DeliveryBranchTests(SurfaceTestCase):
    """The branch states the COST of blocking, which is what a human decides on."""

    def blocked(self):
        return BlockedSurface.from_manifest(self.write_manifest())

    def test_branch_a_is_an_endpoint_of_its_own(self):
        result = diff_surfaces(
            self.baseline(api_paths={}),
            self.target(api_paths={"clips/homecoming/": ["LX/01;"]}),
            self.blocked(),
        )
        candidate = classify_candidate("clips/homecoming/", result)
        self.assertEqual(candidate.delivery_branch, BRANCH_ENDPOINT)
        self.assertIn("throwIfBlocked", candidate.rationale)
        self.assertEqual(candidate.rides_with, ())

    def test_branch_b_never_appears_without_an_already_blocked_path(self):
        """It rides inside another response: a fragile rewriter, not a URL rule.

        `stories_ads` is not a request path, and both classes that carry it also
        carry a path DFInsta already blocks.
        """
        result = diff_surfaces(
            self.baseline(api_paths={}),
            self.target(
                api_paths={
                    "ig/stories_ads": ["LX/01;", "LX/02;"],
                    "feed/timeline/": ["LX/01;"],
                    "clips/homecoming/": ["LX/02;"],
                }
            ),
            self.blocked(),
        )
        candidate = classify_candidate("ig/stories_ads", result)
        self.assertEqual(candidate.delivery_branch, BRANCH_INLINE)
        self.assertEqual(candidate.rides_with, ("clips/homecoming/", "feed/timeline/"))
        self.assertIn("rides inside", candidate.rationale)

    def test_branch_c_when_one_host_carries_no_blocked_path(self):
        """A partial match is not evidence of riding inside anything.

        The second class carries no blocked path at all, so the honest answer is
        "a human has to look", not "it is inline".
        """
        result = diff_surfaces(
            self.baseline(api_paths={}),
            self.target(
                api_paths={
                    "ig/stories_ads": ["LX/01;", "LX/02;"],
                    "feed/timeline/": ["LX/01;"],
                }
            ),
            self.blocked(),
        )
        candidate = classify_candidate("ig/stories_ads", result)
        self.assertEqual(candidate.delivery_branch, BRANCH_UNKNOWN)
        self.assertEqual(candidate.rides_with, ())

    def test_branch_c_without_a_manifest_because_nothing_could_be_compared(self):
        result = diff_surfaces(
            self.baseline(api_paths={}), self.target(api_paths={"ig/stories_ads": ["LX/01;"]})
        )
        candidate = classify_candidate("ig/stories_ads", result)
        self.assertEqual(candidate.delivery_branch, BRANCH_UNKNOWN)
        self.assertIn("no manifest", candidate.rationale)

    def test_branch_c_for_a_deep_link_that_is_not_a_request_at_all(self):
        result = diff_surfaces(
            self.baseline(api_paths={}),
            self.target(api_paths={"ig://clips_home": ["LX/01;"], "feed/timeline/": ["LX/01;"]}),
            self.blocked(),
        )
        # It shares its only class with a blocked path, so it lands on B -- the
        # honest reading of the evidence, and still not A.
        self.assertNotEqual(
            classify_candidate("ig://clips_home", result).delivery_branch, BRANCH_ENDPOINT
        )

    def test_a_literal_absent_from_the_target_is_unknown_not_inline(self):
        result = diff_surfaces(self.baseline(), self.target(), self.blocked())
        candidate = classify_candidate("discover/topical_explore/", result)
        # It is an endpoint by shape, so branch A still holds; a non-endpoint
        # that is absent has nothing to be co-located with.
        self.assertEqual(candidate.delivery_branch, BRANCH_ENDPOINT)
        self.assertEqual(classify_candidate("gone/thing", result).delivery_branch, BRANCH_UNKNOWN)
        self.assertEqual(classify_candidate("gone/thing", result).classes, 0)

    def test_branch_counts_add_up_to_the_candidate_list(self):
        result = diff_surfaces(
            self.baseline(api_paths={}),
            self.target(
                api_paths={
                    "clips/homecoming/": ["LX/01;"],
                    "ig/stories_ads": ["LX/01;"],
                    "cache/thing": ["LX/09;"],
                }
            ),
            self.blocked(),
        )
        counts = result.branch_counts()
        self.assertEqual(sum(counts.values()), len(result.candidates))
        self.assertEqual(counts[BRANCH_ENDPOINT], 1)
        self.assertEqual(counts[BRANCH_INLINE], 1)
        self.assertEqual(counts[BRANCH_UNKNOWN], 1)


# ---------------------------------------------------------------------- report


class ReportTests(SurfaceTestCase):
    def test_the_report_is_json_serialisable(self):
        result = diff_surfaces(
            self.baseline(), self.target(), BlockedSurface.from_manifest(self.write_manifest())
        )
        payload = json.loads(json.dumps(result.to_dict()))
        self.assertEqual(payload["kind"], "dfinsta.surface_diff")
        for key in (
            "baseline",
            "target",
            "survival_rates",
            "api_paths",
            "stable_types",
            "stable_type_packages",
            "added_feature_types",
            "resources",
            "resource_id_stability",
            "colocation_changes",
            "candidates",
            "blocked",
        ):
            with self.subTest(key=key):
                self.assertIn(key, payload)

    def test_the_report_names_both_versions(self):
        payload = diff_surfaces(self.baseline(), self.target()).to_dict()
        self.assertEqual(payload["baseline"]["label"], "stock-430")
        self.assertEqual(payload["target"]["label"], "stock-439")
        self.assertNotEqual(payload["baseline"]["content_hash"], "")

    def test_the_report_states_the_layer_it_was_taken_at(self):
        # The warning travels with the artifact, as the index header's does.
        self.assertIn("stable-string layer", diff_surfaces(self.baseline(), self.target()).to_dict()["note"])

    def test_the_summary_is_returned_as_lines_so_it_can_be_asserted_on(self):
        lines = summary_lines(diff_surfaces(self.baseline(), self.target()))
        text = "\n".join(lines)
        self.assertIn("stock-430 -> stock-439", text)
        self.assertIn("api paths", text)
        self.assertIn("evidence, not a verdict", text)

    def test_the_summary_says_when_coverage_was_not_assessed(self):
        text = "\n".join(summary_lines(diff_surfaces(self.baseline(), self.target())))
        self.assertIn("no manifest supplied", text)


class CliTests(SurfaceTestCase):
    def run_cli(self, argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_it_prints_a_summary_and_writes_the_report(self):
        baseline = self.build("stock-430", api_paths=BASELINE_PATHS, resources={"drawable": BASELINE_DRAWABLES})
        target = self.build("stock-439", api_paths=TARGET_PATHS, resources={"drawable": TARGET_DRAWABLES})
        report = self.tmp / "report.json"
        code, out, _ = self.run_cli(
            [str(baseline), str(target), "--manifest", str(self.write_manifest()), "--json", str(report)]
        )
        self.assertEqual(code, 0)
        self.assertIn("surface diff", out)
        payload = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(payload["api_paths"]["added"], ["clips/homecoming/"])
        self.assertEqual(payload["blocked"]["families"], ["clips", "feed"])

    def test_an_unusable_index_exits_two_rather_than_raising(self):
        """`resolve.py` uses the same code for the same reason: the stage must
        refuse cleanly, not die with a traceback the orchestrator cannot read."""
        code, _, err = self.run_cli([str(self.tmp / "nope"), str(self.tmp / "also-nope")])
        self.assertEqual(code, 2)
        self.assertIn("error:", err)

    def test_an_explicitly_named_manifest_that_is_missing_is_an_error(self):
        """A path the user typed must never be silently ignored.

        The repo default may legitimately be absent; a typo must not degrade to
        "coverage not assessed" and look like a successful run.
        """
        baseline = self.build("stock-430", api_paths=BASELINE_PATHS)
        target = self.build("stock-439", api_paths=TARGET_PATHS)
        code, _, err = self.run_cli(
            [str(baseline), str(target), "--manifest", str(self.tmp / "absent.json")]
        )
        self.assertEqual(code, 2)
        self.assertIn("error:", err)

    def test_an_absent_default_manifest_warns_and_still_runs(self):
        baseline = self.build("stock-430", api_paths=BASELINE_PATHS)
        target = self.build("stock-439", api_paths=TARGET_PATHS)
        previous = os.getcwd()
        os.chdir(self.tmp)  # no manifest/hooks.json here
        try:
            code, out, err = self.run_cli([str(baseline), str(target)])
        finally:
            os.chdir(previous)
        self.assertEqual(code, 0)
        self.assertIn("warning:", err)
        self.assertIn("no manifest supplied", out)


# ------------------------------------------------------------------- mutations


class MutationTests(SurfaceTestCase):
    """Each guard, re-attacked from the direction a broken implementation takes.

    A positive test proves the guard exists. These prove it bites: every one
    builds the input a specific plausible mutation would wave through, and
    asserts the outcome that mutation could not produce.
    """

    def test_an_id_keyed_resource_diff_would_report_almost_every_drawable_as_changed(self):
        """Mutation: diff resources by id (or by name->id pair) instead of by name.

        Measured on the real indexes: of 11,737 drawable names in both 430 and
        439, only 103 keep their hex id -- 99.1% are renumbered. This fixture is
        that in miniature: ten names, all present in both versions, nine of them
        renumbered. The name diff must report NOTHING changed; an id-keyed diff
        reports 90% of the drawables as added and removed.
        """
        baseline_ids = {f"ic_{index}": f"0x7f08{index:04x}" for index in range(10)}
        # Same names, new numbering, except ic_0 which happens to survive.
        target_ids = {
            name: (value if name == "ic_0" else f"0x7f09{index:04x}")
            for index, (name, value) in enumerate(baseline_ids.items())
        }
        result = diff_surfaces(
            self.baseline(resources={"drawable": baseline_ids}),
            self.target(resources={"drawable": target_ids}),
        )

        drawable = result.resources["drawable"]
        self.assertEqual(drawable.added, ())
        self.assertEqual(drawable.removed, ())
        self.assertEqual(drawable.survival_rate, 1.0)

        # The fixture discriminates: keyed by id, or by the (name, id) pair, the
        # answer is not merely different, it is a near-total rewrite.
        by_id_shared = set(baseline_ids.values()) & set(target_ids.values())
        self.assertEqual(len(by_id_shared), 1)
        self.assertEqual(len(by_id_shared) / len(baseline_ids), 0.1)
        by_pair_changed = [
            name for name in baseline_ids if baseline_ids[name] != target_ids[name]
        ]
        self.assertEqual(len(by_pair_changed), 9)
        # ...and the module still knows the ids moved; it just refuses to call
        # that a surface change.
        self.assertEqual(result.resource_id_stability["drawable"], 0.1)

    def test_comparing_descriptor_sets_would_flag_every_shared_literal(self):
        """Mutation: co-location compares which classes hold a literal, not how many.

        Obfuscated names churn every release -- `LX/05t2` even denotes a
        different class in each version -- so a set comparison reports all three
        shared literals as moved and buries the one that really did.
        """
        result = self.diff()
        reported = {change.literal for change in result.colocation_changes}
        self.assertEqual(reported, {"shopping/onboard/"})

        shared = set(BASELINE_PATHS) & set(TARGET_PATHS)
        by_set = {
            literal
            for literal in shared
            if set(BASELINE_PATHS[literal]) != set(TARGET_PATHS[literal])
        }
        self.assertEqual(by_set, shared)  # the mutant flags everything
        self.assertNotEqual(by_set, reported)
        # And the descriptors really are disjoint, which is the whole point.
        self.assertEqual(
            set(BASELINE_PATHS["clips/discover/"]) & set(TARGET_PATHS["clips/discover/"]), set()
        )

    def test_an_unindexed_resource_type_would_look_like_a_mass_deletion(self):
        """Mutation: treat "type not indexed here" as "type is empty here".

        The baseline indexed layouts and the target did not. Read as empty, that
        is 3 layouts deleted and a 0% survival rate reported for a category
        nobody measured.
        """
        layouts = {"action_bar_item": "0x1", "profile_header": "0x2", "reels_viewer": "0x3"}
        result = diff_surfaces(
            self.baseline(
                resources={"drawable": BASELINE_DRAWABLES, "layout": layouts},
                resource_types=("drawable", "layout"),
            ),
            self.target(resources={"drawable": TARGET_DRAWABLES}, resource_types=("drawable",)),
        )
        self.assertNotIn("layout", result.resources)
        self.assertNotIn("layout", result.survival_rates())
        self.assertIn("layout", result.skipped_resource_types)
        # The mutant would have produced this instead:
        self.assertEqual(CategoryDiff.between("layout", layouts, {}).removed, tuple(sorted(layouts)))

    def test_an_empty_baseline_reported_as_zero_percent_would_read_as_a_wipeout(self):
        """Mutation: `survival_rate` returns `shared / max(baseline, 1)`.

        A first run, or a category the baseline never indexed, would then be
        reported as 0% survival -- indistinguishable from the app deleting the
        entire layer, which is the one thing this stage exists to detect.
        """
        empty_baseline = CategoryDiff.between("api_paths", [], ["a/b/", "c/d/"])
        wipeout = CategoryDiff.between("api_paths", ["a/b/", "c/d/"], [])
        self.assertIsNone(empty_baseline.survival_rate)
        self.assertEqual(wipeout.survival_rate, 0.0)
        self.assertNotEqual(empty_baseline.survival_rate, wipeout.survival_rate)

    def test_a_candidate_cannot_be_its_own_evidence_of_riding_inline(self):
        """Mutation: drop the `other != literal` test when looking for co-residents.

        `feed/gizmo` is itself in a blocked family, so a mutant finds "a blocked
        path in this class" -- itself -- and reports B_inline with a rewriter's
        cost attached, for a literal that shares its class with nothing.
        """
        result = diff_surfaces(
            self.baseline(api_paths={}),
            self.target(api_paths={"feed/gizmo": ["LX/01;"]}),
            BlockedSurface.from_manifest(self.write_manifest()),
        )
        candidate = classify_candidate("feed/gizmo", result)
        self.assertTrue(candidate.maps_to_blocked_family)  # the family really does match
        self.assertEqual(candidate.delivery_branch, BRANCH_UNKNOWN)
        self.assertEqual(candidate.rides_with, ())

    def test_an_unfiltered_stable_type_diff_would_report_synthetic_churn(self):
        """Mutation: report every added stable type instead of the meaningful ones.

        Two of the three added types are an inner class and a synthetic lambda,
        which appear and vanish with every recompile.
        """
        result = self.diff()
        self.assertEqual(len(result.stable_types.added), 3)
        self.assertEqual(len(result.added_feature_types), 1)
        self.assertNotEqual(set(result.stable_types.added), set(result.added_feature_types))

    def test_no_obfuscated_descriptor_reaches_the_report(self):
        """Mutation: serialise the classes a literal lives in, not the count.

        Every literal in the fixture lives in an `LX/` class, so if any code path
        put a descriptor into the cross-version artifact it would show up here.
        A downstream stage that joined on one would get a confident wrong answer,
        because `LX/05t2` names a different class in each version.
        """
        result = diff_surfaces(
            self.baseline(), self.target(), BlockedSurface.from_manifest(self.write_manifest())
        )
        payload = json.dumps(result.to_dict())
        self.assertNotIn("LX/", payload)
        self.assertNotIn("smali", payload)
        # ...while the fixture certainly contains descriptors.
        self.assertIn("LX/05t2;", json.dumps(BASELINE_PATHS))
        self.assertNotIn("LX/", "\n".join(summary_lines(result)))

    def test_the_module_states_no_verdict_about_a_feature(self):
        """Mutation: someone adds `addictive` to the candidate schema.

        The judgement is a later stage behind a human gate; a field here would
        pre-empt the gate and make the evidence look decided.
        """
        result = diff_surfaces(self.baseline(), self.target())
        payload = json.dumps(result.to_dict()).lower()
        for word in ("addictive", "verdict", "recommendation"):
            with self.subTest(word=word):
                self.assertNotIn(word, payload)


# ---------------------------------------------------------------- real indexes


class RealIndexTests(unittest.TestCase):
    """The one test that touches `work/index-430` and `work/index-439`.

    They are 70 MB each and gitignored, so it skips when they are absent. It
    re-measures the facts the whole design rests on rather than trusting the
    numbers written in the docstrings, and it fails if a version bump ever makes
    them untrue.
    """

    def test_the_measured_430_to_439_diff_still_holds(self):
        missing = [
            str(directory)
            for directory in (INDEX_430, INDEX_439)
            if not (directory / "api_surface.json").is_file()
        ]
        if missing:
            self.skipTest(f"real index not built: {', '.join(missing)}")

        blocked = BlockedSurface.from_manifest(REAL_MANIFEST)
        result = diff_surfaces(INDEX_430, INDEX_439, blocked)

        with self.subTest("api paths survive a version bump"):
            rate = result.api_paths.survival_rate
            assert rate is not None
            self.assertGreater(
                rate,
                0.90,
                "API-path literals are the strongest cross-version signal; if they "
                "stopped surviving, feature discovery needs a different anchor",
            )
            self.assertLess(rate, 1.0, "an identical surface means the wrong pair of indexes")

        with self.subTest("stable types survive slightly less well"):
            types_rate = result.stable_types.survival_rate
            assert types_rate is not None
            self.assertGreater(types_rate, 0.85)
            self.assertLess(types_rate, rate)

        with self.subTest("resource names survive while their ids do not"):
            drawable = result.resources["drawable"].survival_rate
            assert drawable is not None
            self.assertGreater(drawable, 0.95)
            # 103 of 11,737 shared names keep their hex id.
            self.assertLess(
                result.resource_id_stability["drawable"],
                0.05,
                "if drawable ids had become stable, the name-keyed diff would "
                "still be correct -- but the reason for it would need rewriting",
            )

        with self.subTest("clips/discover/ is a clips endpoint DFInsta already blocks"):
            candidate = classify_candidate("clips/discover/", result)
            self.assertEqual(candidate.family, "clips")
            self.assertEqual(candidate.delivery_branch, BRANCH_ENDPOINT)
            self.assertTrue(candidate.maps_to_blocked_family)
            self.assertIn("replace_reels_discover_endpoint", candidate.blocked_by)
            self.assertGreater(candidate.classes, 1)

        with self.subTest("the report carries no cross-version trap"):
            payload = json.dumps(result.to_dict())
            self.assertNotIn("LX/", payload)

        with self.subTest("there is a real candidate list to triage"):
            self.assertTrue(result.candidates)
            self.assertGreaterEqual(result.branch_counts()[BRANCH_ENDPOINT], 1)


if __name__ == "__main__":
    unittest.main()

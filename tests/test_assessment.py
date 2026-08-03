"""Tests for stage 4a — turning a changed surface into a claim a human can check.

The module under test exists because a calibration experiment killed the obvious
design. Seven engagement signals were fixed from product mechanics first, then
measured; six were noise, and the composite scored the 40-literal *control* group
(1.18) between the labelled positives (1.43) and negatives (0.90). So there is no
addictiveness score anywhere in `assessment.py`, and these tests treat that as a
property to defend rather than an implementation detail: `NoScoreTests`
enumerates the module's public functions reflectively and asserts none of them
returns a number.

What replaced the score is the app's own bookkeeping — a class that enumerates
several endpoints DFInsta already blocks is Instagram declaring a group, and
whatever else that class lists are the candidates. Three things make that fragile
in ways a passing test suite could easily miss, so each gets its own class here:

`NormaliseTests`      — the manifest writes `/api/v1/clips/homecoming/` and the
                        index holds `clips/homecoming/`. A mismatch produces zero
                        gaps, which is indistinguishable from "no new features".
`IsBlockedTests`      — coverage is substring containment, not equality, and it is
                        not symmetric. An equality implementation produced two
                        false gaps out of six.
`CohesionTests`       — the guard that keeps generated global string pools out of
                        the report. This is the important one: the failure it
                        prevents is a report a human stops reading.

Fixtures are synthetic three-file indexes built in a temp directory, using
`write_index` from `test_hook_index` so the shape stays in one place. `Hook`
objects are constructed directly rather than loaded from `manifest/hooks.json`,
so editing the manifest cannot silently change what these tests claim.

`MutationTests` adds no coverage. It re-attacks four guards from the direction a
broken implementation would take, and each docstring says what would reach a
human at the gate if that guard were removed.

`RealIndexTests` is the only test that touches `work/index-430` and
`work/index-439`; it skips when they are absent. It pins the measured result that
justifies the whole technique — the same four gaps under two different obfuscated
descriptors.

`KnownGapTests` pins two behaviours that are reported rather than fixed, so a
future fix fails loudly instead of quietly changing what the report certifies.

The last four classes cover the layer that turns this stage's output into
something a gate can pin. `document` names the whole stage in one call;
`canonical_bytes` is the exact byte string whose digest a human ends up signing;
`candidate_ids` is the ONE decoder both the preparing side and the re-deriving
client must read that list through; and `policy_revision` reads the manifest
field `load_manifest` discards. Their tests are refusal-heavy on purpose: each of
those is a place where a permissive reading would let two derivations disagree
about what a human approved, and `feature_gate.validate_submission` never re-reads
the assessment blob, so nothing downstream would catch it.
"""

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from unittest import mock

from dfinsta_pipeline import assessment
from dfinsta_pipeline.assessment import (
    looks_like_uri_rule,
    DOCUMENT_SCHEMA_VERSION,
    MIN_COHESION,
    Assessment,
    AssessmentError,
    Evidence,
    Grouping,
    Judgement,
    Strength,
    Verdict,
    assess,
    assess_gap,
    blocked_endpoints,
    canonical_bytes,
    candidate_ids,
    coverage_gaps,
    document,
    find_groupings,
    is_blocked,
    normalise,
    policy_revision,
    report,
)
from dfinsta_pipeline.contracts import ID_PATTERN, canonical_json
from dfinsta_pipeline.feature_gate import CANDIDATE_ID_PATTERN, MAX_CANDIDATE_ID
from dfinsta_pipeline.hook_index import API_SURFACE_FILENAME, HookIndex
from dfinsta_pipeline.hook_manifest import Hook, HostFingerprint
from dfinsta_pipeline.runtime_identity import probe_call

from tests.test_hook_index import write_index


ROOT = Path(__file__).resolve().parents[1]
INDEX_430 = ROOT / "work" / "index-430"
INDEX_439 = ROOT / "work" / "index-439"
#: Tracked, so any test reading it runs everywhere rather than skipping.
REPO_MANIFEST = ROOT / "manifest" / "hooks.json"


# --------------------------------------------------------------------- fixtures

# The five endpoints DFInsta blocks that the real grouping also lists, spelled the
# way the *index* holds them.
KNOWN_MEMBERS = (
    "clips/discover/",
    "clips/homecoming/",
    "discover/topical_explore/",
    "feed/reels_tray/",
    "feed/timeline/",
)

# The four it lists that DFInsta does not block. These are the real answer on both
# 430 and 439, and `feed/timeline_stream/` is the one that an equality-based
# coverage check would have got right by accident while getting others wrong.
NOVEL_MEMBERS = (
    "feed/injected_reels_media/",
    "feed/reels_media/",
    "feed/reels_media_stream/",
    "feed/timeline_stream/",
)

CURATED_MEMBERS = tuple(sorted(KNOWN_MEMBERS + NOVEL_MEMBERS))

# Spelled the way the *manifest* writes them, which is deliberately not the way the
# index holds them: two leading slashes to strip, one `api/v1/` prefix, and
# `discover/topical_explore` with no trailing slash — the entry that only matches
# its literal under containment.
BLOCK_DEPS = (
    "/feed/timeline/",
    "/discover/topical_explore",
    "/api/v1/clips/homecoming/",
    "/feed/reels_tray/",
    "clips/discover/",
)

# A generated global string pool, taken verbatim from `LX/0000;` on Instagram 439:
# 51 literals of which exactly 3 are covered by the block list, for a cohesion of
# 0.06 against the curated grouping's 0.56. `/proc/self/status` and the help-centre
# URLs are what a report looks like when the cohesion guard is removed.
POOL_KNOWN = ("clips/discover/location/", "clips/discover/social/", "feed/timeline/")
POOL_JUNK = (
    "/graphql",
    "/proc/self/cmdline",
    "/proc/self/statm",
    "/proc/self/status",
    "/settings/sandbox/web/sandbox",
    "/sys/devices/system/cpu/",
    "/t_rtc_multi",
    "/user_values",
    "accounts/change_profile_picture/",
    "business/branded_content/update_whitelist_settings/",
    "feed/text_post_app_timeline/",
    "http://schemas.android.com/apk/res/android",
    "https://help.instagram.com/1310346208996329",
    "https://help.instagram.com/1731078377046291",
    "https://help.instagram.com/491565145294150",
    "https://help.instagram.com/contact/233964459562201",
    "https://instagram.com/",
    "https://privacycenter.instagram.com/privacy/genai",
    "https://privacycenter.instagram.com/privacy/genai/",
    "https://www.facebook.com/",
    "https://www.facebook.com/legal/ai-terms",
    "https://www.facebook.com/legal/br-ai-terms",
    "https://www.facebook.com/legal/eu-ai-terms",
    "https://www.facebook.com/legal/terms/ad_creative_generative_ai_terms",
    "https://www.facebook.com/legal/uk-ai-terms",
    "https://www.facebook.com/policies/other-policies/ais-terms",
    "https://www.facebook.com/privacy/genai",
    "https://www.facebook.com/privacy/guide/genai",
    "https://www.facebook.com/privacy/guide/generative-ai",
    "https://www.facebook.com/privacy/policy",
    "https://www.facebook.com/privacy/policy/",
    "https://www.instagram.com/p/",
    "instagram://ad_activity",
    "instagram://ads_payments",
    "instagram://direct-inbox",
    "instagram://edit_profile_links",
    "instagram://editprofile",
    "instagram://security_checkup",
    "instagram://teen_project",
    "instagram://ugc_persona_settings",
    "media/%s/comment_like/",
    "media/%s/comment_unlike/",
    "media/%s/comments/",
    "media/story_comment/fetch/",
    "msys://ae-media",
    "restrict_action/unrestrict/",
    "usertags/%s/feed/",
    "usertags/review/",
)
POOL_MEMBERS = tuple(sorted(POOL_KNOWN + POOL_JUNK))


def hook_for(*semantic_deps: str, hook_id: str = "hook.block", status: str = "active") -> Hook:
    """A well-formed hook that declares `semantic_deps` and nothing else of interest.

    Built here rather than read from `manifest/hooks.json` so that a manifest edit
    cannot change what any test in this file asserts.
    """
    return Hook(
        hook_id=hook_id,
        intent="block a continuous-content surface",
        tier="robust",
        strategy="tigon_url_block",
        semantic_deps=tuple(semantic_deps),
        hosts=(HostFingerprint("named", descriptor="LX/05jj;"),),
        anchor=('const-string v0, "placeholder"',),
        payload=(f"# {hook_id}",),
        marker=f"# {hook_id}",
        expected_marker_count=1,
        status=status,
    )


def blocking_hooks() -> list[Hook]:
    """The manifest's real block list, one hook per entry, spelled as the manifest does."""
    return [
        hook_for(dep, hook_id=f"hook.block.{index}")
        for index, dep in enumerate(BLOCK_DEPS)
    ]


def manifest_document(
    *, policy_revision: Any = "2026-08-01", deps: Sequence[str] = BLOCK_DEPS
) -> dict:
    """A manifest `load_manifest` accepts, blocking `deps` and nothing else.

    The same block list as `blocking_hooks`, expressed as the file the loader
    reads, because `policy_revision` and `assessment_record.record` take a *path*
    rather than a hook list. The payload carries `probe_call` because
    `assert_instrumented` refuses an active hook that cannot report its own
    execution, and building that line from the real helper rather than a literal
    keeps this fixture correct if the probe descriptor ever moves.
    """
    hooks = []
    for index, dep in enumerate(deps):
        hook_id = f"hook.block.{index}"
        marker = f"# {hook_id}"
        hooks.append(
            {
                "hook_id": hook_id,
                "intent": "block a continuous-content surface",
                "tier": "robust",
                "strategy": "tigon_url_block",
                "semantic_deps": [dep],
                "hosts": [{"kind": "named", "descriptor": "LX/05jj;"}],
                "anchor": ['const-string v0, "placeholder"'],
                "payload": [probe_call(hook_id), marker],
                "marker": marker,
                "expected_marker_count": 1,
            }
        )
    return {"schema_version": 1, "policy_revision": policy_revision, "hooks": hooks}


def write_manifest(path: Path, *, drop: tuple[str, ...] = (), **overrides: Any) -> Path:
    """Write `manifest_document` to `path`; `drop` removes top-level keys.

    `drop` is how "the manifest has no policy_revision" is expressed, which is a
    different document from one whose `policy_revision` is null.
    """
    data = manifest_document(**overrides)
    for key in drop:
        data.pop(key, None)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def surface_for(classes: Mapping[str, Iterable[str]]) -> dict[str, list[str]]:
    """Invert {descriptor: literals} into the {literal: descriptors} shape the index stores.

    The index's forward map is what the builder writes; `literals_in` inverts it
    back. Writing fixtures in the readable direction keeps a test's intent visible
    instead of scattering one class's members across a dozen literal entries.
    """
    api_paths: dict[str, list[str]] = {}
    for descriptor, literals in classes.items():
        for literal in literals:
            api_paths.setdefault(literal, []).append(descriptor)
    return {literal: sorted(holders) for literal, holders in sorted(api_paths.items())}


class AssessmentTestCase(unittest.TestCase):
    """A temp root plus a one-line way to turn {descriptor: literals} into an index."""

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        # `.resolve()` because the header records a resolved decode path and `/tmp`
        # is a symlink on some systems.
        self.decode = (self.tmp / "stock-430").resolve()
        self.decode.mkdir()
        self._built = 0

    def index_for(self, classes: Mapping[str, Iterable[str]], name: str = "") -> HookIndex:
        self._built += 1
        directory = write_index(
            self.tmp / (name or f"index-{self._built}"),
            decode=str(self.decode),
            api_paths=surface_for(classes),
        )
        return HookIndex.load(directory)

    def curated_index(self, descriptor: str = "LX/05jj;", name: str = "") -> HookIndex:
        """One class holding the nine members of the real declared group."""
        return self.index_for({descriptor: CURATED_MEMBERS}, name=name)

    def blocked(self) -> set[str]:
        return blocked_endpoints(blocking_hooks())

    def sole_grouping(self, groupings) -> Grouping:
        self.assertEqual(len(groupings), 1, f"expected one grouping, got {groupings}")
        return groupings[0]


# -------------------------------------------------------------------- normalise


class NormaliseTests(unittest.TestCase):
    """One endpoint, two spellings. If they never meet, the stage finds nothing.

    A manifest `semantic_deps` entry reads `/api/v1/clips/homecoming/` while the
    index holds `clips/homecoming/`. That is not a cosmetic difference: without
    normalisation no seed ever matches an indexed literal, so no class is ever a
    candidate, so no grouping is ever found and no gap is ever reported — a
    silence that looks exactly like "this version added nothing".
    """

    def test_a_leading_slash_is_removed(self):
        self.assertEqual(normalise("/feed/timeline/"), "feed/timeline/")

    def test_repeated_leading_slashes_are_all_removed(self):
        self.assertEqual(normalise("//feed/timeline/"), "feed/timeline/")

    def test_the_api_v1_prefix_is_removed(self):
        self.assertEqual(normalise("api/v1/clips/homecoming/"), "clips/homecoming/")

    def test_the_bare_api_prefix_is_removed(self):
        # Some manifest entries carry `api/` without the version segment.
        self.assertEqual(normalise("api/clips/homecoming/"), "clips/homecoming/")

    def test_a_slash_and_an_api_v1_prefix_together(self):
        # The spelling the manifest actually uses.
        self.assertEqual(normalise("/api/v1/clips/homecoming/"), "clips/homecoming/")

    def test_surrounding_whitespace_is_removed(self):
        for raw in ("  /api/v1/clips/homecoming/", "/api/v1/clips/homecoming/  ", "\n/api/v1/clips/homecoming/\t"):
            with self.subTest(raw=repr(raw)):
                self.assertEqual(normalise(raw), "clips/homecoming/")

    def test_the_empty_string_normalises_to_the_empty_string(self):
        self.assertEqual(normalise(""), "")

    def test_input_that_is_only_punctuation_or_a_prefix_normalises_to_nothing(self):
        """A rule that normalises to `""` would be contained in every literal.

        `""` is a substring of every string, so an empty rule surviving into the
        block set would mark every endpoint as already covered and the stage would
        report zero gaps forever. Callers rely on `normalise` producing a falsy
        value here so they can drop it.
        """
        for raw in ("", "   ", "/", "//", "/api/", "api/v1/", "  /api/v1/  "):
            with self.subTest(raw=repr(raw)):
                self.assertEqual(normalise(raw), "")

    def test_an_already_normalised_literal_is_unchanged(self):
        for literal in KNOWN_MEMBERS + NOVEL_MEMBERS:
            with self.subTest(literal=literal):
                self.assertEqual(normalise(literal), literal)

    def test_it_is_idempotent(self):
        # Both sides of a comparison may have been normalised a different number
        # of times; the answer must not depend on how many.
        for raw in ("/api/v1/clips/homecoming/", "clips/homecoming/", "  /feed/timeline/ "):
            with self.subTest(raw=repr(raw)):
                self.assertEqual(normalise(normalise(raw)), normalise(raw))

    def test_an_interior_api_segment_is_not_touched(self):
        # Only a *prefix* is a spelling difference; the same text mid-path is part
        # of the endpoint and stripping it would silently rewrite the rule.
        self.assertEqual(normalise("feed/api/v1/timeline/"), "feed/api/v1/timeline/")

    def test_the_manifest_spelling_and_the_index_spelling_meet(self):
        """The single fact the whole stage rests on, asserted directly."""
        for dep, indexed in (
            ("/api/v1/clips/homecoming/", "clips/homecoming/"),
            ("/feed/timeline/", "feed/timeline/"),
            ("/feed/reels_tray/", "feed/reels_tray/"),
        ):
            with self.subTest(dep=dep):
                self.assertEqual(normalise(dep), normalise(indexed))


class NormalisationStakesTests(AssessmentTestCase):
    """What a spelling mismatch costs, stated as an assertion rather than a comment."""

    def test_the_raw_manifest_spellings_match_nothing_in_the_index(self):
        """The premise: the manifest's strings are simply not in the index.

        Every seed is looked up in the index verbatim, so if the seeds arrive
        unnormalised the candidate set is empty before any grouping logic runs.
        """
        index = self.curated_index()
        for dep in BLOCK_DEPS:
            if dep == normalise(dep):
                continue  # already index-shaped; nothing to prove
            with self.subTest(dep=dep):
                self.assertEqual(index.descriptors_with_literal(dep), ())

    def test_normalised_seeds_find_the_grouping_that_raw_seeds_cannot(self):
        """Same index, same manifest, two spellings — four gaps or none.

        This is the whole risk of the mismatch: the failure is not an exception,
        it is a clean run that reports nothing.
        """
        index = self.curated_index()
        assessments, groupings = assess(index, blocking_hooks())
        self.assertEqual(len(groupings), 1)
        self.assertEqual([item.literal for item in assessments], list(NOVEL_MEMBERS))

        raw_seeds = {dep for hook in blocking_hooks() for dep in hook.semantic_deps}
        raw_candidates = {
            descriptor
            for seed in raw_seeds
            for descriptor in index.descriptors_with_literal(seed)
        }
        # `clips/discover/` is already index-shaped, so one seed still lands; it is
        # alone, which is below `min_seeds` and therefore finds nothing.
        self.assertLess(len(raw_candidates), 2)


# ------------------------------------------------------------------- is_blocked


class IsBlockedTests(unittest.TestCase):
    """Coverage is substring containment against the URI path, and it has a direction.

    `throwIfBlocked` applies `endsWith`/`contains` to the request's URI path, so a
    rule covers any endpoint whose path contains it. Equality was tried first and
    produced two false gaps out of six — endpoints reported as unprotected that are
    in fact blocked. At a gate that is worse than a miss: a human who checks one
    claim, finds it wrong, and discounts the rest has been actively misled.
    """

    RULES = frozenset(
        {
            "clips/discover",
            "discover/topical_explore",
            "feed/timeline/",
            "feed/reels_tray/",
        }
    )

    def test_a_rule_covers_a_literal_that_merely_contains_it(self):
        # `discover/topical_explore` (no trailing slash) covers the literal that
        # has one. Under equality this is a false gap.
        self.assertEqual(
            is_blocked("discover/topical_explore/", self.RULES), "discover/topical_explore"
        )

    def test_a_rule_covers_every_longer_path_beneath_it(self):
        # `clips/discover` covers `clips/discover/interest/stream/`, which is a real
        # 439 endpoint the app added under an already-blocked prefix.
        for literal in (
            "clips/discover/",
            "clips/discover/stream/",
            "clips/discover/interest/stream/",
            "clips/discover/location/",
        ):
            with self.subTest(literal=literal):
                self.assertEqual(is_blocked(literal, self.RULES), "clips/discover")

    def test_a_trailing_slash_in_the_rule_is_part_of_the_rule(self):
        """`feed/timeline/` does not cover `feed/timeline_stream/`, and that is the point.

        This is the endpoint that makes the four-gap result non-trivial: it looks
        adjacent to a blocked one and is not blocked. A containment check that
        ignored the trailing slash would swallow it and the stage would report
        three gaps instead of four.
        """
        self.assertIsNone(is_blocked("feed/timeline_stream/", self.RULES))

    def test_containment_is_not_symmetric(self):
        """The rule must be inside the literal, never the other way round.

        A symmetric check would let the *shorter* side win in either direction, so
        a rule for a specific sub-path would start covering its whole parent tree
        and blocked-ness would leak upward into endpoints nobody ruled on.
        """
        self.assertEqual(is_blocked("feed/timeline/extra/", {"feed/timeline/"}), "feed/timeline/")
        self.assertIsNone(is_blocked("feed/timeline/", {"feed/timeline/extra/"}))

    def test_it_returns_the_matching_rule_rather_than_a_bare_bool(self):
        """A report has to be able to cite *which* rule covers an endpoint.

        "Already blocked" with no rule named is unverifiable at the gate: a reader
        cannot check it without re-deriving the whole block set themselves.
        """
        result = is_blocked("clips/discover/stream/", {"clips/discover"})
        self.assertIsInstance(result, str)
        self.assertEqual(result, "clips/discover")
        self.assertIn(result, {"clips/discover"})

    def test_an_uncovered_literal_returns_none(self):
        for literal in NOVEL_MEMBERS:
            with self.subTest(literal=literal):
                self.assertIsNone(is_blocked(literal, self.RULES))

    def test_the_literal_side_is_normalised_before_comparing(self):
        # The candidate can arrive in either spelling; the rules are already
        # normalised by `blocked_endpoints`.
        self.assertEqual(
            is_blocked("/api/v1/feed/timeline/", {"feed/timeline/"}), "feed/timeline/"
        )

    def test_an_empty_rule_never_matches(self):
        """`"" in anything` is True, so an empty rule would block the entire app.

        Every gap would disappear and the report would be empty — the same silent
        failure mode as a normalisation mismatch, reached from the other side.
        """
        for literal in NOVEL_MEMBERS:
            with self.subTest(literal=literal):
                self.assertIsNone(is_blocked(literal, {""}))
        self.assertEqual(is_blocked("feed/timeline/", {"", "feed/timeline/"}), "feed/timeline/")

    def test_an_empty_literal_is_not_covered_by_anything(self):
        self.assertIsNone(is_blocked("", self.RULES))
        self.assertIsNone(is_blocked("/", self.RULES))

    def test_an_empty_rule_set_covers_nothing(self):
        self.assertIsNone(is_blocked("feed/timeline/", set()))


class BlockedEndpointTests(unittest.TestCase):
    """What DFInsta covers is read from the manifest, normalised, and never guessed."""

    def test_every_active_dep_becomes_a_normalised_rule(self):
        self.assertEqual(
            blocked_endpoints(blocking_hooks()),
            {
                "feed/timeline/",
                "discover/topical_explore",
                "clips/homecoming/",
                "feed/reels_tray/",
                "clips/discover/",
            },
        )

    def test_an_inactive_hook_contributes_nothing(self):
        """A retired hook is not protection. Counting it would hide a real gap.

        Reading `status` here rather than at the call site is what keeps a disabled
        hook from silently vouching for an endpoint nobody is blocking any more.
        """
        hooks = [
            hook_for("/feed/timeline/", hook_id="hook.on"),
            hook_for("/feed/reels_tray/", hook_id="hook.off", status="retired"),
        ]
        self.assertEqual(blocked_endpoints(hooks), {"feed/timeline/"})

    def test_a_dep_that_normalises_to_nothing_is_dropped(self):
        """An empty rule is contained in every literal, so it would block everything.

        A hook whose dep is `/` or `/api/` is a manifest typo, not a rule covering
        the entire app; letting it through would empty the report and look like a
        clean run.
        """
        hooks = [hook_for("/", "/api/", "  ", "/feed/timeline/", hook_id="hook.typo")]
        self.assertEqual(blocked_endpoints(hooks), {"feed/timeline/"})

    def test_a_hook_with_no_deps_contributes_nothing(self):
        self.assertEqual(blocked_endpoints([hook_for(hook_id="hook.ui")]), set())

    def test_no_hooks_at_all_is_an_empty_set(self):
        self.assertEqual(blocked_endpoints([]), set())

    def test_a_non_endpoint_dep_is_excluded_from_the_rules(self):
        """`set_app_context` declares a smali method reference, not a URI rule.

        Carrying it through was harmless only because nothing contains it. A
        short non-path dep that slipped in would match most of a group and
        quietly empty the report — indistinguishable from "nothing new was
        found", which is the failure mode this stage most has to avoid.
        """
        hooks = [hook_for("Landroid/app/Application;->onCreate()V", hook_id="hook.startup")]
        self.assertEqual(blocked_endpoints(hooks), set())
        hooks = [hook_for("/feed/timeline/", hook_id="hook.feed")]
        self.assertEqual(blocked_endpoints(hooks), {"feed/timeline/"})


# -------------------------------------------------------------------- groupings


class GroupingDiscoveryTests(AssessmentTestCase):
    """The detector names no class. It finds a group by content or not at all.

    `LX/05jj;` on 430 and `LX/03Ez;` on 439 are the same grouping under different
    obfuscated names. Any implementation that recognised a descriptor would work
    on exactly one version, which is the failure the whole pipeline exists to
    avoid — obfuscated names are recycled, not merely scrambled.
    """

    def test_a_class_enumerating_several_blocked_endpoints_is_found(self):
        groupings = find_groupings(self.curated_index(), self.blocked())
        grouping = self.sole_grouping(groupings)
        self.assertEqual(grouping.descriptor, "LX/05jj;")
        self.assertEqual(grouping.known, KNOWN_MEMBERS)
        self.assertEqual(grouping.novel, NOVEL_MEMBERS)
        self.assertEqual(grouping.size, 9)

    def test_the_same_group_is_found_under_two_different_descriptors(self):
        """Two fixtures, two obfuscated names, identical membership, one answer.

        This is the entire reason the technique survives a version bump. If it
        needed a descriptor, porting to 439 would mean re-deriving it by hand,
        which is the manual step this stage replaces.
        """
        found = {}
        for descriptor in ("LX/05jj;", "LX/03Ez;"):
            index = self.curated_index(descriptor, name=f"index-{descriptor[3:-1]}")
            grouping = self.sole_grouping(find_groupings(index, self.blocked()))
            found[descriptor] = grouping

        self.assertNotEqual(*found)  # the two fixtures really do differ
        first, second = found["LX/05jj;"], found["LX/03Ez;"]
        self.assertEqual(first.descriptor, "LX/05jj;")
        self.assertEqual(second.descriptor, "LX/03Ez;")
        self.assertEqual(first.known, second.known)
        self.assertEqual(first.novel, second.novel)
        self.assertEqual(first.cohesion, second.cohesion)

    def test_a_descriptor_of_any_shape_at_all_is_accepted(self):
        # Not merely "two obfuscated names": there is no name filter of any kind,
        # so a version that stopped obfuscating this class would still be handled.
        index = self.curated_index("Lcom/instagram/feed/ContinuousSurfaces;")
        grouping = self.sole_grouping(find_groupings(index, self.blocked()))
        self.assertEqual(grouping.descriptor, "Lcom/instagram/feed/ContinuousSurfaces;")
        self.assertEqual(grouping.novel, NOVEL_MEMBERS)

    def test_a_class_holding_no_blocked_endpoint_is_never_a_candidate(self):
        index = self.index_for(
            {"LX/0zzz;": ("accounts/login/", "accounts/logout/", "users/set_biography/")}
        )
        self.assertEqual(find_groupings(index, self.blocked()), [])

    def test_classes_are_ranked_by_how_much_they_are_recognised(self):
        """Order decides what a reader sees first and which grouping a gap is cited to.

        Sorted on (-known, -size, descriptor), so it is total and deterministic
        rather than dependent on set iteration order.
        """
        index = self.index_for(
            {
                "LX/0bbb;": KNOWN_MEMBERS[:3],
                "LX/05jj;": CURATED_MEMBERS,
                "LX/0aaa;": KNOWN_MEMBERS[:3],
            }
        )
        groupings = find_groupings(index, self.blocked())
        self.assertEqual(
            [item.descriptor for item in groupings], ["LX/05jj;", "LX/0aaa;", "LX/0bbb;"]
        )
        for _ in range(3):
            self.assertEqual(
                [item.descriptor for item in find_groupings(index, self.blocked())],
                ["LX/05jj;", "LX/0aaa;", "LX/0bbb;"],
            )

    def test_no_seeds_at_all_finds_nothing_rather_than_everything(self):
        """An empty requirement must not degrade into "every class qualifies".

        With no block list there is nothing to recognise a group by, so the honest
        answer is none — not the whole index.
        """
        index = self.curated_index()
        for seeds in ([], (), set(), iter(())):
            with self.subTest(seeds=type(seeds).__name__):
                self.assertEqual(find_groupings(index, seeds), [])

    def test_seeds_that_normalise_to_nothing_find_nothing(self):
        # The same guard from the other direction: `["/", "  "]` is not a block
        # list, and an empty rule would otherwise match every literal.
        self.assertEqual(find_groupings(self.curated_index(), ["/", "  ", ""]), [])

    def test_a_grouping_exposes_its_own_size(self):
        grouping = self.sole_grouping(find_groupings(self.curated_index(), self.blocked()))
        self.assertEqual(grouping.size, len(grouping.known) + len(grouping.novel))
        self.assertEqual(grouping.size, len(CURATED_MEMBERS))


class ThresholdTests(AssessmentTestCase):
    """`min_seeds` and `min_size`: how much bookkeeping counts as a declaration."""

    def test_one_shared_endpoint_is_not_a_grouping(self):
        """A single co-occurrence is a coincidence, not a declaration.

        Hundreds of classes mention one blocked endpoint — analytics maps, prefetch
        allowlists, error tables. Treating any of them as a group would report
        their unrelated neighbours as engagement surfaces.
        """
        index = self.index_for({"LX/0lone;": ("feed/timeline/", "feed/timeline_stream/")})
        self.assertEqual(find_groupings(index, self.blocked()), [])

    def test_lowering_min_seeds_admits_it_so_the_guard_is_a_filter_not_a_wall(self):
        # The complement of the test above: the class is otherwise perfectly
        # well-formed, so `min_seeds` is the only thing rejecting it.
        index = self.index_for({"LX/0lone;": ("feed/timeline/", "feed/timeline_stream/")})
        grouping = self.sole_grouping(find_groupings(index, self.blocked(), min_seeds=1))
        self.assertEqual(grouping.known, ("feed/timeline/",))
        self.assertEqual(grouping.novel, ("feed/timeline_stream/",))

    def test_two_shared_endpoints_are_enough_by_default(self):
        index = self.index_for({"LX/0pair;": ("feed/timeline/", "feed/reels_tray/")})
        grouping = self.sole_grouping(find_groupings(index, self.blocked()))
        self.assertEqual(grouping.known, ("feed/reels_tray/", "feed/timeline/"))

    def test_raising_min_seeds_rejects_a_group_that_no_longer_clears_it(self):
        index = self.index_for({"LX/0pair;": ("feed/timeline/", "feed/reels_tray/")})
        self.assertEqual(find_groupings(index, self.blocked(), min_seeds=3), [])

    def test_min_size_rejects_a_class_too_small_to_be_a_list(self):
        """Two literals is a pair, not an enumeration.

        `min_size` counts everything the class holds, so it can reject a small
        class even when every literal in it is recognised — which is the case a
        seed count alone cannot see.
        """
        index = self.index_for({"LX/0pair;": ("feed/timeline/", "feed/reels_tray/")})
        self.assertEqual(find_groupings(index, self.blocked(), min_seeds=1, min_size=3), [])
        self.assertEqual(
            len(find_groupings(index, self.blocked(), min_seeds=1, min_size=2)), 1
        )

    def test_min_size_counts_the_whole_class_not_just_the_recognised_part(self):
        # Two seeds and one unknown: `min_size=3` passes on the class's full
        # membership, which a threshold counting only known members would fail.
        index = self.index_for(
            {"LX/0mix;": ("feed/timeline/", "feed/reels_tray/", "feed/timeline_stream/")}
        )
        self.assertEqual(
            find_groupings(index, self.blocked(), min_size=3),
            [
                Grouping(
                    "LX/0mix;",
                    ("feed/reels_tray/", "feed/timeline/"),
                    ("feed/timeline_stream/",),
                )
            ],
        )
        self.assertEqual(find_groupings(index, self.blocked(), min_size=4), [])


class CohesionTests(AssessmentTestCase):
    """The string-pool guard — the most consequential threshold in the module.

    Instagram's generated global string pools hold everything: `/proc/self/status`,
    help-centre URLs, deep links, and incidentally a few blocked endpoints. They
    clear any seed-count threshold trivially. `docs/PORT_430_MAPPING.md` already
    warns never to patch those pools; this keeps them out of the report for the
    same reason, and the reason is a human one — a report padded with
    `/proc/self/status` and privacy-policy URLs is worse than no report, because
    the reader stops reading and the four real gaps go with it.
    """

    def curated_and_pool(self) -> HookIndex:
        return self.index_for({"LX/05jj;": CURATED_MEMBERS, "LX/0000;": POOL_MEMBERS})

    def test_the_curated_grouping_is_kept(self):
        groupings = find_groupings(self.curated_and_pool(), self.blocked())
        self.assertIn("LX/05jj;", [item.descriptor for item in groupings])

    def test_the_generated_string_pool_is_rejected(self):
        groupings = find_groupings(self.curated_and_pool(), self.blocked())
        self.assertNotIn("LX/0000;", [item.descriptor for item in groupings])
        self.assertEqual([item.descriptor for item in groupings], ["LX/05jj;"])

    def test_the_two_score_the_way_the_design_measured_them(self):
        # 5 known among 9 versus 3 among 51 — the separation the threshold sits in.
        curated = Grouping("LX/05jj;", KNOWN_MEMBERS, NOVEL_MEMBERS)
        self.assertEqual(curated.size, 9)
        self.assertAlmostEqual(curated.cohesion, 5 / 9, places=3)
        self.assertGreater(curated.cohesion, MIN_COHESION)

        pool_known = tuple(sorted(item for item in POOL_MEMBERS if is_blocked(item, self.blocked())))
        pool_novel = tuple(item for item in POOL_MEMBERS if item not in pool_known)
        pool = Grouping("LX/0000;", pool_known, pool_novel)
        self.assertEqual(pool.size, 51)
        self.assertEqual(len(pool.known), 3)
        self.assertAlmostEqual(pool.cohesion, 3 / 51, places=3)
        self.assertLess(pool.cohesion, MIN_COHESION)

    def test_the_pool_clears_the_seed_threshold_so_cohesion_is_what_rejects_it(self):
        """Proves the fixture discriminates: a seed count alone would admit the pool.

        This is why the guard is cohesion and not a higher `min_seeds` — a seed
        threshold is a magic number that fits today's group and would miss a
        smaller one tomorrow, while still letting a large enough pool through.
        """
        index = self.curated_and_pool()
        admitted = find_groupings(index, self.blocked(), min_cohesion=0.0)
        self.assertIn("LX/0000;", [item.descriptor for item in admitted])
        pool = next(item for item in admitted if item.descriptor == "LX/0000;")
        self.assertGreaterEqual(len(pool.known), 2)  # clears the default min_seeds
        self.assertGreaterEqual(pool.size, 2)  # and the default min_size

    def test_the_threshold_is_the_documented_one(self):
        self.assertEqual(MIN_COHESION, 0.4)

    def test_a_grouping_exactly_at_the_threshold_is_kept(self):
        # The comparison is `< min_cohesion`, so the boundary belongs to the group.
        # Pinned because an off-by-one here silently changes which classes qualify.
        members = ("feed/timeline/", "feed/reels_tray/") + NOVEL_MEMBERS[:3]
        index = self.index_for({"LX/0edge;": members})
        grouping = self.sole_grouping(find_groupings(index, self.blocked()))
        self.assertAlmostEqual(grouping.cohesion, 0.4, places=6)

    def test_just_below_the_threshold_is_rejected(self):
        members = ("feed/timeline/", "feed/reels_tray/") + NOVEL_MEMBERS
        index = self.index_for({"LX/0edge;": members})
        self.assertAlmostEqual(2 / 6, 0.333, places=3)
        self.assertEqual(find_groupings(index, self.blocked()), [])

    def test_cohesion_is_exposed_on_the_serialised_grouping(self):
        """A reader must be able to check the inference, not trust that a threshold ran.

        The grouping is an inference from co-location, and cohesion is the number
        that says how much of the class we recognised. Hiding it would leave a
        human at the gate with "trust me, it passed a filter".
        """
        grouping = self.sole_grouping(find_groupings(self.curated_and_pool(), self.blocked()))
        payload = grouping.to_dict()
        self.assertIn("cohesion", payload)
        self.assertEqual(payload["cohesion"], round(grouping.cohesion, 3))
        self.assertEqual(payload["cohesion"], 0.556)
        # And the raw material for re-deriving it by hand.
        self.assertEqual(payload["known"], list(KNOWN_MEMBERS))
        self.assertEqual(payload["novel"], list(NOVEL_MEMBERS))
        self.assertEqual(payload["size"], 9)
        self.assertEqual(payload["descriptor"], "LX/05jj;")

    def test_cohesion_of_an_empty_grouping_is_zero_rather_than_a_crash(self):
        # Not reachable through `find_groupings`, but `Grouping` is public and a
        # ZeroDivisionError inside a report renderer is the worst place to find one.
        self.assertEqual(Grouping("LX/0empty;", (), ()).cohesion, 0.0)
        self.assertEqual(Grouping("LX/0empty;", (), ()).to_dict()["cohesion"], 0.0)


# ------------------------------------------------------- measured versus judged


class MeasuredAndJudgedTests(AssessmentTestCase):
    """Facts and opinions are different types, and the report keeps them apart.

    The experiment is the reason. If a judgement could be appended to the evidence
    list, the distinction would survive exactly as long as the next person's
    attention, and the gate would stop being able to tell "the index says so" from
    "an agent thought so".
    """

    def sample(self) -> Assessment:
        index = self.curated_index()
        assessments, _ = assess(index, blocking_hooks())
        return assessments[0]

    def test_everything_in_measured_is_evidence(self):
        index = self.curated_index()
        assessments, _ = assess(index, blocking_hooks())
        self.assertTrue(assessments)
        for item in assessments:
            for entry in item.measured:
                with self.subTest(candidate=item.candidate_id, kind=getattr(entry, "kind", None)):
                    self.assertIsInstance(entry, Evidence)
                    self.assertNotIsInstance(entry, Judgement)

    def test_the_stage_produces_no_judgement_of_its_own(self):
        """The stage measures; a human or an agent judges, later and by name.

        A recommendation emitted by the same code that produced the evidence would
        be self-corroboration, which is the failure the evidence ledger is built
        around.
        """
        index = self.curated_index()
        assessments, _ = assess(index, blocking_hooks())
        for item in assessments:
            with self.subTest(candidate=item.candidate_id):
                self.assertIsNone(item.judgement)

    def test_a_judgement_must_name_who_made_it(self):
        for actor in ("", "   ", "\t\n"):
            with self.subTest(actor=repr(actor)):
                with self.assertRaises(ValueError) as caught:
                    Judgement(actor, Verdict.OFFER_TOGGLE, "reels injection is engagement bait")
                self.assertIn("who made it", str(caught.exception))

    def test_a_judgement_must_give_its_reasoning(self):
        """A bare recommendation is indistinguishable from a guess at the gate.

        The whole value of the judged half is that a reader can disagree with the
        reading without distrusting the facts — which requires the reading to be
        written down.
        """
        for reasoning in ("", "   ", "\n"):
            with self.subTest(reasoning=repr(reasoning)):
                with self.assertRaises(ValueError) as caught:
                    Judgement("agent:assessor-1", Verdict.BLOCK, reasoning)
                self.assertIn("indistinguishable from a guess", str(caught.exception))

    def test_a_well_formed_judgement_is_accepted(self):
        judgement = Judgement(
            "agent:assessor-1",
            Verdict.OFFER_TOGGLE,
            "injected reels are a continuous surface; the product rule is a switch",
            unresolved=("no delivery-branch evidence for this endpoint",),
        )
        self.assertEqual(judgement.recommendation, Verdict.OFFER_TOGGLE)
        self.assertEqual(judgement.unresolved, ("no delivery-branch evidence for this endpoint",))

    def test_an_admitted_gap_survives_serialisation(self):
        # `unresolved` is where a reader looks to decide how much the
        # recommendation is worth, so it must not be dropped on the way out.
        judgement = Judgement("sam@dfinsta", Verdict.DEFER, "needs a device check", ("cost of blocking",))
        self.assertEqual(judgement.to_dict()["unresolved"], ["cost of blocking"])

    def test_to_dict_keeps_the_two_halves_in_separate_keys(self):
        judgement = Judgement("sam@dfinsta", Verdict.OFFER_TOGGLE, "engagement surface")
        item = Assessment(
            self.sample().candidate_id,
            self.sample().literal,
            self.sample().measured,
            judgement=judgement,
        )
        payload = item.to_dict()
        self.assertEqual(set(payload), {
            "candidate_id", "literal", "strongest_evidence", "measured", "judgement",
        })
        self.assertEqual(payload["judgement"], judgement.to_dict())
        for entry in payload["measured"]:
            with self.subTest(kind=entry["kind"]):
                self.assertEqual(set(entry), {"kind", "strength", "summary", "detail"})
                self.assertNotIn("recommendation", entry)
                self.assertNotIn("reasoning", entry)
                self.assertNotIn("actor", entry)

    def test_an_unjudged_candidate_serialises_its_judgement_as_null(self):
        # Explicitly null rather than absent: a consumer must be able to see that
        # nobody has ruled on this candidate, which is the state that blocks a run.
        payload = self.sample().to_dict()
        self.assertIn("judgement", payload)
        self.assertIsNone(payload["judgement"])

    def test_strongest_reports_the_highest_level_present_and_never_a_total(self):
        """Strength is recorded per item and never summed.

        A composite of mostly-weak signals reads as authority it has not earned,
        which is precisely what the calibration experiment measured happening.
        """
        weak = Evidence("prefetch_density", Strength.WEAK, "0.38 against a 0.17 baseline")
        medium = Evidence("co_location_change", Strength.MEDIUM, "moved class")
        strong = Evidence("coverage_gap", Strength.STRONG, "no active hook blocks it")
        self.assertIs(Assessment("c", "l", (weak, medium, strong)).strongest, Strength.STRONG)
        self.assertIs(Assessment("c", "l", (weak, medium)).strongest, Strength.MEDIUM)
        self.assertIs(Assessment("c", "l", (weak, weak, weak)).strongest, Strength.WEAK)
        self.assertIsNone(Assessment("c", "l", ()).strongest)

    def test_a_candidate_is_summarised_by_a_label_not_a_number(self):
        # `strongest_evidence` is the only per-candidate summary in the document.
        # A number there would be a ranking, and a ranking is a score by another name.
        payload = self.sample().to_dict()
        self.assertEqual(payload["strongest_evidence"], "strong")
        self.assertNotIsInstance(payload["strongest_evidence"], (int, float))


class NoScoreTests(AssessmentTestCase):
    """No function in this module returns an addictiveness score or a ranking.

    The calibration experiment is the whole reason: six of seven a-priori
    engagement signals were noise, and the composite placed a 40-literal random
    control (1.18) between the labelled positives (1.43) and negatives (0.90). A
    number built from that would read as authority it has not earned, and worse,
    would be trivially sortable — turning "here are four endpoints, check them"
    into "here is the top of a ranking", which is a claim nothing supports.
    """

    #: Every public function the module exposes. Pinned as a set so that adding a
    #: new one is a deliberate act that must come with a decision about whether it
    #: is allowed to return a number.
    EXPECTED_PUBLIC_FUNCTIONS = frozenset(
        {
            "assess",
            "assess_gap",
            "blocked_endpoints",
            "coverage_gaps",
            "find_groupings",
            "is_blocked",
            "looks_like_uri_rule",
            "normalise",
            "report",
            # The bytes the gate pins, and the readers of the two files it needs.
            # `document` is `report(*assess(...))` under one name; `canonical_bytes`
            # is the exact byte string whose digest a human signs; `candidate_ids`
            # is the one decoder both derivations must read that list through;
            # `policy_revision` reads the manifest field `load_manifest` discards.
            # None of them may return a number, for the same reason as the rest.
            "canonical_bytes",
            "candidate_ids",
            "document",
            "policy_revision",
        }
    )

    #: Pinned for the same reason as the functions, one layer up: `AssessmentError`
    #: is what every refusal in the decoder raises, so a caller catches it by name.
    #: A new public class appearing here without a decision is how a second error
    #: type, or a second document shape, arrives unnoticed.
    EXPECTED_PUBLIC_CLASSES = frozenset(
        {
            "Assessment",
            "AssessmentError",
            "Evidence",
            "Grouping",
            "Judgement",
            "Strength",
            "Verdict",
        }
    )

    def public_functions(self) -> dict:
        return {
            name: member
            for name, member in inspect.getmembers(assessment, inspect.isfunction)
            if member.__module__ == assessment.__name__ and not name.startswith("_")
        }

    def public_classes(self) -> dict:
        return {
            name: member
            for name, member in inspect.getmembers(assessment, inspect.isclass)
            if member.__module__ == assessment.__name__ and not name.startswith("_")
        }

    def test_the_public_surface_is_exactly_what_this_test_checks(self):
        self.assertEqual(frozenset(self.public_functions()), self.EXPECTED_PUBLIC_FUNCTIONS)

    def test_the_public_class_surface_is_exactly_what_this_test_checks(self):
        self.assertEqual(frozenset(self.public_classes()), self.EXPECTED_PUBLIC_CLASSES)
        # And the one every reader of a document has to catch is a ValueError, so
        # an `except ValueError` around a strict decode keeps working.
        self.assertTrue(issubclass(AssessmentError, ValueError))

    def test_no_public_function_declares_a_numeric_return(self):
        for name, function in sorted(self.public_functions().items()):
            with self.subTest(function=name):
                annotation = inspect.signature(function).return_annotation
                self.assertIsNot(annotation, inspect.Signature.empty, f"{name} is unannotated")
                # `from __future__ import annotations` makes these strings.
                self.assertNotIn("float", str(annotation))
                self.assertNotEqual(str(annotation).strip(), "int")

    def test_no_public_function_actually_returns_a_number(self):
        """The annotations could lie; these are the real return values.

        Every public function is called with fixture arguments and its result
        checked. A scoring function added later fails the surface test above, so
        the two together close the loop.
        """
        index = self.curated_index()
        hooks = blocking_hooks()
        blocked = self.blocked()
        grouping = Grouping("LX/05jj;", KNOWN_MEMBERS, NOVEL_MEMBERS)
        manifest = write_manifest(self.tmp / "hooks.json")
        calls = {
            "normalise": lambda: normalise("/api/v1/clips/homecoming/"),
            "blocked_endpoints": lambda: blocked_endpoints(hooks),
            "is_blocked": lambda: is_blocked("clips/discover/stream/", blocked),
            "find_groupings": lambda: find_groupings(index, blocked),
            "coverage_gaps": lambda: coverage_gaps([grouping], blocked),
            "assess_gap": lambda: assess_gap("feed/reels_media/", grouping),
            "assess": lambda: assess(index, hooks),
            "report": lambda: report(*assess(index, hooks)),
            "looks_like_uri_rule": lambda: looks_like_uri_rule("/feed/timeline/"),
            "document": lambda: document(index, hooks),
            "canonical_bytes": lambda: canonical_bytes(document(index, hooks)),
            "candidate_ids": lambda: candidate_ids(document(index, hooks)),
            "policy_revision": lambda: policy_revision(manifest),
        }
        self.assertEqual(frozenset(calls), self.EXPECTED_PUBLIC_FUNCTIONS)
        for name, call in sorted(calls.items()):
            with self.subTest(function=name):
                result = call()
                # `bool` is excluded deliberately: a predicate answering yes/no
                # is not a score. What this forbids is a magnitude — a number
                # standing in for how addictive something is — because six of the
                # seven signals measured were noise and a composite would read as
                # authority it has not earned. `bool` subclasses `int`, so the
                # order of these checks matters.
                self.assertNotIsInstance(result, complex)
                if not isinstance(result, bool):
                    self.assertNotIsInstance(result, (int, float))

    def test_the_report_ranks_nothing(self):
        """Candidates come out in discovery order, carrying no rank of their own.

        Sorted output would imply an ordering the evidence does not support: all
        four gaps are the same claim, differing only in which endpoint they name.
        """
        index = self.curated_index()
        document = report(*assess(index, blocking_hooks()))
        for candidate in document["candidates"]:
            with self.subTest(candidate=candidate["candidate_id"]):
                self.assertNotIn("score", candidate)
                self.assertNotIn("rank", candidate)
                self.assertNotIn("priority", candidate)

    def test_the_document_says_out_loud_that_no_score_is_computed(self):
        # The constraint travels with the document. A downstream consumer reading
        # it later has no other way to know a number was deliberately withheld.
        note = report([], [])["note"]
        self.assertIn("No addictiveness score", note)
        self.assertIn("six of", note.lower())


# ------------------------------------------------------------------- end to end


class AssessTests(AssessmentTestCase):
    """The whole stage on a synthetic fixture: gaps, no gaps, and no grouping at all."""

    def test_a_grouping_with_novel_members_yields_one_assessment_per_gap(self):
        assessments, groupings = assess(self.curated_index(), blocking_hooks())
        self.assertEqual([item.literal for item in assessments], list(NOVEL_MEMBERS))
        self.assertEqual(len(groupings), 1)

    def test_each_gap_carries_both_strong_measured_facts(self):
        """The two claims a reader can re-derive: the app groups it, and we miss it.

        Both are STRONG because both are checkable against the decode without
        trusting anything this stage inferred.
        """
        assessments, _ = assess(self.curated_index(), blocking_hooks())
        for item in assessments:
            with self.subTest(candidate=item.candidate_id):
                kinds = [entry.kind for entry in item.measured]
                self.assertEqual(kinds, ["app_declared_grouping", "coverage_gap"])
                for entry in item.measured:
                    self.assertIs(entry.strength, Strength.STRONG)
                self.assertIs(item.strongest, Strength.STRONG)

    def test_the_evidence_names_the_grouping_so_the_inference_can_be_checked(self):
        assessments, _ = assess(self.curated_index(), blocking_hooks())
        detail = assessments[0].measured[0].detail
        self.assertEqual(detail["descriptor"], "LX/05jj;")
        self.assertEqual(detail["group_size"], 9)
        self.assertEqual(detail["known_members"], list(KNOWN_MEMBERS))
        self.assertIn("LX/05jj;", assessments[0].measured[0].summary)

    def test_the_candidate_id_is_derived_from_the_endpoint(self):
        # Stable across runs and versions, because the endpoint string is the one
        # part of a candidate that does not move when Instagram re-obfuscates.
        assessments, _ = assess(self.curated_index(), blocking_hooks())
        for item in assessments:
            with self.subTest(candidate=item.candidate_id):
                self.assertEqual(item.candidate_id, f"gap:{item.literal}")

    def test_a_fully_covered_grouping_yields_no_assessments(self):
        """Nothing to report is a valid, correct outcome and must be reported as such."""
        index = self.index_for({"LX/05jj;": KNOWN_MEMBERS})
        assessments, groupings = assess(index, blocking_hooks())
        self.assertEqual(assessments, [])
        self.assertEqual(len(groupings), 1)
        self.assertEqual(groupings[0].novel, ())
        self.assertEqual(groupings[0].cohesion, 1.0)

    def test_no_grouping_at_all_is_an_empty_list_rather_than_an_error(self):
        """It must degrade by reporting nothing, never by inventing a grouping.

        If a future version stops enumerating these endpoints together, the honest
        answer is "no group found" — the same principle as the co-location host
        search, which empties rather than falling back to a partial match.
        """
        index = self.index_for({"LX/0zzz;": ("accounts/login/", "users/set_biography/")})
        assessments, groupings = assess(index, blocking_hooks())
        self.assertEqual(assessments, [])
        self.assertEqual(groupings, [])

    def test_an_index_with_no_literals_at_all_is_handled(self):
        index = self.index_for({})
        self.assertEqual(assess(index, blocking_hooks()), ([], []))

    def test_a_manifest_with_no_deps_finds_nothing_rather_than_everything(self):
        index = self.curated_index()
        self.assertEqual(assess(index, [hook_for(hook_id="hook.ui")]), ([], []))
        self.assertEqual(assess(index, []), ([], []))

    def test_a_gap_listed_by_two_groupings_is_reported_once(self):
        """A candidate is an endpoint, not an (endpoint, class) pair.

        Two classes enumerating the same endpoint is one gap seen twice; reporting
        it twice would inflate the count a human uses to judge how much changed.
        """
        index = self.index_for(
            {
                "LX/0aaa;": KNOWN_MEMBERS + ("feed/reels_media/",),
                "LX/0bbb;": KNOWN_MEMBERS + ("feed/reels_media/",),
            }
        )
        assessments, groupings = assess(index, blocking_hooks())
        self.assertEqual(len(groupings), 2)
        self.assertEqual([item.literal for item in assessments], ["feed/reels_media/"])
        # Attributed deterministically to the first grouping in ranked order.
        self.assertEqual(assessments[0].measured[0].detail["descriptor"], "LX/0aaa;")

    def test_the_groupings_come_back_alongside_the_candidates(self):
        """A reader at the gate needs the grouping to judge whether the inference holds.

        Handing back only the candidates would make the report unfalsifiable: four
        endpoint names with no way to see what they were grouped with.
        """
        assessments, groupings = assess(self.curated_index(), blocking_hooks())
        self.assertTrue(assessments)
        self.assertEqual(groupings[0].known, KNOWN_MEMBERS)
        self.assertEqual(groupings[0].novel, NOVEL_MEMBERS)

    def test_a_group_below_the_seed_threshold_produces_no_candidates(self):
        index = self.index_for({"LX/0lone;": ("feed/timeline/", "feed/timeline_stream/")})
        self.assertEqual(assess(index, blocking_hooks()), ([], []))
        # And relaxing the threshold is what admits it, so nothing else is at play.
        assessments, groupings = assess(index, blocking_hooks(), min_seeds=1)
        self.assertEqual([item.literal for item in assessments], ["feed/timeline_stream/"])
        self.assertEqual(len(groupings), 1)

    def test_the_result_is_stable_across_repeated_runs(self):
        # Groupings are found by walking sets; an unstable order would make two
        # runs of the same stage produce different documents and different hashes.
        index = self.index_for({"LX/05jj;": CURATED_MEMBERS, "LX/0aaa;": KNOWN_MEMBERS})
        first = report(*assess(index, blocking_hooks()))
        for _ in range(4):
            self.assertEqual(report(*assess(index, blocking_hooks())), first)


class ReportTests(AssessmentTestCase):
    """The document that goes to CAS and is hash-pinned into the gate.

    It has to survive `json.dumps` byte-for-byte identically on both sides of that
    pin, and its counts have to describe its own contents — a reader who trusts the
    summary must not be reading a different document from the one they were shown.
    """

    def document(self) -> dict:
        return report(*assess(self.curated_index(), blocking_hooks()))

    def test_the_report_is_json_serialisable(self):
        text = json.dumps(self.document(), sort_keys=True)
        self.assertEqual(json.loads(text), self.document())

    def test_the_counts_match_the_contents(self):
        document = self.document()
        self.assertEqual(document["counts"]["candidates"], len(document["candidates"]))
        self.assertEqual(document["counts"]["groupings"], len(document["groupings"]))
        self.assertEqual(document["counts"]["candidates"], 4)
        self.assertEqual(document["counts"]["groupings"], 1)

    def test_the_judged_count_counts_only_judged_candidates(self):
        assessments, groupings = assess(self.curated_index(), blocking_hooks())
        self.assertEqual(report(assessments, groupings)["counts"]["judged"], 0)

        judgement = Judgement("sam@dfinsta", Verdict.OFFER_TOGGLE, "continuous surface")
        judged = [
            Assessment(item.candidate_id, item.literal, item.measured, judgement)
            if index == 0
            else item
            for index, item in enumerate(assessments)
        ]
        document = report(judged, groupings)
        self.assertEqual(document["counts"]["judged"], 1)
        self.assertEqual(document["counts"]["candidates"], 4)

    def test_an_empty_run_still_produces_a_well_formed_document(self):
        # "Nothing found" must be a report, not an absence of one: the gate reads a
        # document either way and a missing one is indistinguishable from a crash.
        document = report([], [])
        self.assertEqual(document["counts"], {"groupings": 0, "candidates": 0, "judged": 0})
        self.assertEqual(document["candidates"], [])
        self.assertEqual(document["groupings"], [])
        self.assertEqual(document["schema_version"], 1)
        self.assertTrue(json.dumps(document))

    def test_every_candidate_can_be_traced_to_a_grouping_in_the_same_document(self):
        # A citation pointing outside the document is not checkable.
        document = self.document()
        descriptors = {item["descriptor"] for item in document["groupings"]}
        for candidate in document["candidates"]:
            with self.subTest(candidate=candidate["candidate_id"]):
                cited = candidate["measured"][0]["detail"]["descriptor"]
                self.assertIn(cited, descriptors)


# -------------------------------------------------------- the bytes a gate pins


class DocumentTests(AssessmentTestCase):
    """`document` is a wrapper, and must stay one.

    `assess` returns two values and `report` combines them in a particular order.
    Every caller wanting the document had to remember to do both, which is the
    kind of invariant that survives on attention until it does not — so the
    wrapper exists, and these assert it wraps rather than reimplements.
    """

    def test_document_equals_report_of_assess(self):
        index = self.curated_index()
        hooks = blocking_hooks()
        self.assertEqual(document(index, hooks), report(*assess(index, hooks)))
        # Byte-level too: this is the object whose digest a human signs, so
        # "equal dicts" is not a strong enough statement about it.
        self.assertEqual(
            canonical_bytes(document(index, hooks)),
            canonical_bytes(report(*assess(index, hooks))),
        )

    def test_document_passes_min_seeds_through_rather_than_fixing_it(self):
        """A wrapper that dropped the argument would silently find fewer groups.

        With `min_seeds=1` a two-literal class becomes a grouping and yields a
        candidate; with the default it does not. If `document` hardcoded the
        default, the two sides of this would agree and the parameter would be
        decoration.
        """
        index = self.index_for({"LX/0lone;": ("feed/timeline/", "feed/timeline_stream/")})
        hooks = blocking_hooks()
        self.assertEqual(
            document(index, hooks, min_seeds=1), report(*assess(index, hooks, min_seeds=1))
        )
        self.assertEqual(document(index, hooks, min_seeds=1)["counts"]["candidates"], 1)
        self.assertEqual(document(index, hooks)["counts"]["candidates"], 0)


class CanonicalBytesTests(AssessmentTestCase):
    """The exact byte string that goes to CAS and whose digest the gate pins.

    Two Activities derive this independently and neither may trust the other's
    copy, so "the same document" is not enough: the bytes have to be the same
    bytes, every time, from the same input.
    """

    def test_canonical_bytes_are_identical_across_repeated_calls(self):
        payload = document(self.curated_index(), blocking_hooks())
        first = canonical_bytes(payload)
        self.assertIsInstance(first, bytes)
        for _ in range(4):
            self.assertEqual(canonical_bytes(payload), first)
        # And recomputing the document from the same input gives the same bytes,
        # which is the property that lets the recording side recompute rather
        # than adopt a caller's copy.
        self.assertEqual(
            canonical_bytes(document(self.curated_index(), blocking_hooks())), first
        )

    def test_canonical_bytes_are_the_canonical_json_encoding(self):
        payload = document(self.curated_index(), blocking_hooks())
        self.assertEqual(canonical_bytes(payload), canonical_json(payload).encode("utf-8"))
        # Not `json.dumps` in some other shape: the digest is over these bytes and
        # nothing else, so an indented or key-ordered variant is a different blob.
        self.assertNotEqual(
            canonical_bytes(payload), json.dumps(payload, indent=2).encode("utf-8")
        )


class CandidateIdsTests(AssessmentTestCase):
    """The one decoder, and every way it must refuse rather than guess.

    Both the side preparing the gate and the client re-deriving its subject read
    the candidate list through this function. A second decoder, or one that took
    the list from a caller, lets the two derivations diverge for a reason the
    human never touched — and `feature_gate.validate_submission` never re-reads
    the assessment blob, so nothing downstream would catch it. Every rule below
    is therefore a refusal, not a filter.
    """

    def real_document(self) -> dict:
        return document(self.curated_index(), blocking_hooks())

    def with_candidates(self, candidates) -> dict:
        """A real document with its candidates array replaced.

        Real surroundings on purpose: a refusal has to come from the candidate
        under test rather than from a hand-built document being malformed
        elsewhere.
        """
        payload = self.real_document()
        payload["candidates"] = candidates
        return payload

    def test_a_real_document_reads_back_its_four_candidates(self):
        # The positive control the refusals hang off: this decoder is capable of
        # succeeding, so a refusal below is about the input and not about the
        # function rejecting everything.
        names = candidate_ids(self.real_document())
        self.assertEqual(names, tuple(f"gap:{literal}" for literal in NOVEL_MEMBERS))
        for name in names:
            with self.subTest(candidate=name):
                self.assertTrue(CANDIDATE_ID_PATTERN.fullmatch(name), name)

    def test_the_documents_own_order_is_preserved_and_never_sorted(self):
        """`FeatureGateRequestV1` compares the list positionally.

        Both derivations read the same CAS document, so the document's order is
        the authority. Re-sorting here would make two orders hash alike and
        quietly discard the order a human was shown.
        """
        unsorted = (
            "gap:feed/timeline_stream/",
            "gap:clips/discover/",
            "gap:aaa/",
            "gap:feed/reels_media/",
        )
        self.assertNotEqual(unsorted, tuple(sorted(unsorted)))  # the fixture bites
        payload = self.with_candidates([{"candidate_id": name} for name in unsorted])
        self.assertEqual(candidate_ids(payload), unsorted)
        self.assertNotEqual(candidate_ids(payload), tuple(sorted(unsorted)))

    def test_a_document_that_is_not_a_mapping_is_refused(self):
        for value in ([], (), "candidates", 1, None, [{"candidate_id": "gap:a/"}]):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(AssessmentError):
                    candidate_ids(value)

    def test_a_document_of_another_schema_version_is_refused(self):
        for version in (2, 0, "1", None):
            with self.subTest(schema_version=version):
                payload = self.real_document()
                payload["schema_version"] = version
                with self.assertRaises(AssessmentError):
                    candidate_ids(payload)
        missing = self.real_document()
        del missing["schema_version"]
        with self.assertRaises(AssessmentError):
            candidate_ids(missing)
        # The version this decoder does read is the one the writer stamps.
        self.assertEqual(self.real_document()["schema_version"], DOCUMENT_SCHEMA_VERSION)

    def test_a_document_with_no_candidates_array_is_refused(self):
        missing = self.real_document()
        del missing["candidates"]
        with self.assertRaises(AssessmentError):
            candidate_ids(missing)
        for value in ({"gap:a/": 1}, "gap:a/", 4, None):
            with self.subTest(candidates=type(value).__name__):
                with self.assertRaises(AssessmentError):
                    candidate_ids(self.with_candidates(value))

    def test_a_candidate_that_is_not_a_mapping_is_refused(self):
        for entry in ("gap:feed/reels_media/", ["gap:feed/reels_media/"], None, 7):
            with self.subTest(candidate=type(entry).__name__):
                with self.assertRaises(AssessmentError):
                    candidate_ids(self.with_candidates([entry]))

    def test_a_candidate_id_that_is_not_a_string_is_refused(self):
        for value in (1, None, True, ["gap:a/"], {"id": "gap:a/"}):
            with self.subTest(candidate_id=type(value).__name__):
                with self.assertRaises(AssessmentError):
                    candidate_ids(self.with_candidates([{"candidate_id": value}]))
        with self.assertRaises(AssessmentError):
            candidate_ids(self.with_candidates([{"literal": "feed/reels_media/"}]))

    def test_a_candidate_id_the_gate_pattern_rejects_is_refused(self):
        """Refused here as well as at the gate, so the failure is named where read.

        Each of these is ambiguity no producer emits — a leading slash, a doubled
        slash, a second `:`, an empty segment — and admitting one would hand
        `FeatureGateRequestV1` an id it refuses, one layer from where it was made.
        """
        for value in ("/leading/slash/", "gap:double//slash/", "two:colons:here", "", " "):
            with self.subTest(candidate_id=value):
                self.assertIsNone(CANDIDATE_ID_PATTERN.fullmatch(value))
                with self.assertRaises(AssessmentError):
                    candidate_ids(self.with_candidates([{"candidate_id": value}]))

    def test_a_candidate_id_longer_than_the_gate_allows_is_refused(self):
        """Length is a separate rule from shape, so it needs a separate case.

        This id is perfectly well formed — it fails only for being too long. A
        version of the decoder that dropped the length check would wave it
        through while the pattern check kept passing.
        """
        long_id = "gap:" + "a" * MAX_CANDIDATE_ID
        self.assertGreater(len(long_id), MAX_CANDIDATE_ID)
        self.assertIsNotNone(CANDIDATE_ID_PATTERN.fullmatch(long_id))
        with self.assertRaises(AssessmentError):
            candidate_ids(self.with_candidates([{"candidate_id": long_id}]))
        # And the same id one character inside the bound is accepted, so the
        # refusal above is about the length rather than about the shape.
        inside = "gap:" + "a" * (MAX_CANDIDATE_ID - len("gap:"))
        self.assertEqual(len(inside), MAX_CANDIDATE_ID)
        self.assertEqual(
            candidate_ids(self.with_candidates([{"candidate_id": inside}])), (inside,)
        )

    def test_an_empty_candidates_array_is_refused(self):
        """A gate over nothing is a human approving nothing.

        Completeness would hold vacuously and the run would proceed on an empty
        ruling. Stage 4 finding no grouping is a report, not a gate — and
        `report([], [])` is a perfectly valid document, which is exactly why the
        refusal has to live in the reader.
        """
        with self.assertRaises(AssessmentError):
            candidate_ids(self.with_candidates([]))
        with self.assertRaises(AssessmentError):
            candidate_ids(report([], []))

    def test_duplicate_candidate_ids_are_refused(self):
        """One candidate ruled on twice is two rulings that may disagree.

        `FeatureGateRequestV1` refuses duplicates too, so admitting them here
        would only move the failure somewhere it reads as corruption.
        """
        names = ["gap:feed/reels_media/", "gap:feed/timeline_stream/", "gap:feed/reels_media/"]
        with self.assertRaises(AssessmentError) as caught:
            candidate_ids(self.with_candidates([{"candidate_id": n} for n in names]))
        self.assertIn("gap:feed/reels_media/", str(caught.exception))
        # The same list without the repeat is accepted, so the refusal is about
        # the duplication and nothing else.
        self.assertEqual(
            candidate_ids(self.with_candidates([{"candidate_id": n} for n in names[:2]])),
            tuple(names[:2]),
        )


class PolicyRevisionTests(AssessmentTestCase):
    """The manifest field `load_manifest` reads and throws away.

    It is a required field of the gate request and one of the four dimensions
    `decisions.py` makes a decision's reusability hang on, so a value that exists
    on disk and reaches nothing is a wire that looks connected.
    """

    def test_the_revision_is_read_from_the_manifest(self):
        path = write_manifest(self.tmp / "hooks.json", policy_revision="2026-08-01")
        self.assertEqual(policy_revision(path), "2026-08-01")
        # Read from the file rather than defaulted: a different file says
        # something different.
        other = write_manifest(self.tmp / "other.json", policy_revision="2025-12-31")
        self.assertEqual(policy_revision(other), "2025-12-31")
        self.assertEqual(policy_revision(str(other)), "2025-12-31")

    def test_a_manifest_with_no_policy_revision_is_refused(self):
        path = write_manifest(self.tmp / "hooks.json", drop=("policy_revision",))
        self.assertNotIn("policy_revision", json.loads(path.read_text(encoding="utf-8")))
        with self.assertRaises(AssessmentError):
            policy_revision(path)

    def test_a_non_string_policy_revision_is_refused(self):
        for value in (None, 20260801, 2026.08, True, ["2026-08-01"], {"v": "2026-08-01"}):
            with self.subTest(policy_revision=type(value).__name__):
                path = write_manifest(self.tmp / "hooks.json", policy_revision=value)
                with self.assertRaises(AssessmentError):
                    policy_revision(path)

    def test_a_policy_revision_that_is_not_an_identifier_is_refused(self):
        """`FeatureAssessmentGateV1` validates it with `ID_PATTERN`.

        A value that fails there fails after the document has been built and
        recorded, which is the wrong place to find out.
        """
        for value in ("", "2026 08 01", "-2026-08-01", "2026/08/01", "a" * 129):
            with self.subTest(policy_revision=value):
                self.assertIsNone(ID_PATTERN.fullmatch(value))
                path = write_manifest(self.tmp / "hooks.json", policy_revision=value)
                with self.assertRaises(AssessmentError):
                    policy_revision(path)

    def test_the_repo_manifest_carries_a_revision_this_reader_accepts(self):
        """The one test here that reads `manifest/hooks.json` itself.

        Tracked, so it cannot skip. Every other fixture in this file is synthetic
        precisely so a manifest edit cannot change what a test claims — but that
        also means nothing would notice the real manifest and this reader drifting
        apart until a gate refused a run. This notices.
        """
        self.assertTrue(REPO_MANIFEST.is_file(), REPO_MANIFEST)
        revision = policy_revision(REPO_MANIFEST)
        self.assertIsInstance(revision, str)
        self.assertIsNotNone(ID_PATTERN.fullmatch(revision), revision)
        self.assertEqual(
            revision, json.loads(REPO_MANIFEST.read_text(encoding="utf-8"))["policy_revision"]
        )


# ------------------------------------------------------------------- mutations


class MutationTests(AssessmentTestCase):
    """Each guard, re-attacked from the direction a broken implementation takes.

    A positive test proves a guard exists. These prove it bites: each builds the
    input a specific plausible mutation would wave through, and asserts the
    outcome that mutation could not produce. Every docstring says what would reach
    a human at the gate if the guard were gone.
    """

    def test_without_the_cohesion_check_the_string_pool_reappears(self):
        """Mutation: drop the `min_cohesion` filter from `find_groupings`.

        In production the report then fills with `/proc/self/status`,
        `https://help.instagram.com/...` and Facebook privacy-policy URLs, because
        a generated global string pool holds a few blocked endpoints among fifty
        unrelated strings. `docs/PORT_430_MAPPING.md` already warns never to patch
        those pools. The cost is not a wrong patch — it is a human who opens the
        report, sees junk, and stops reading before reaching the four real gaps.
        """
        index = self.index_for({"LX/05jj;": CURATED_MEMBERS, "LX/0000;": POOL_MEMBERS})
        blocked = self.blocked()

        mutant = find_groupings(index, blocked, min_cohesion=0.0)
        mutant_gaps = [literal for literal, _ in coverage_gaps(mutant, blocked)]
        self.assertIn("LX/0000;", [item.descriptor for item in mutant])
        for junk in ("/proc/self/status", "https://www.facebook.com/privacy/policy", "instagram://editprofile"):
            with self.subTest(junk=junk):
                self.assertIn(junk, mutant_gaps)
        self.assertGreater(len(mutant_gaps), 40)

        assessments, groupings = assess(index, blocking_hooks())
        self.assertEqual([item.descriptor for item in groupings], ["LX/05jj;"])
        self.assertEqual([item.literal for item in assessments], list(NOVEL_MEMBERS))
        for junk in POOL_JUNK:
            with self.subTest(junk=junk):
                self.assertNotIn(junk, [item.literal for item in assessments])

    def test_equality_instead_of_containment_reports_blocked_endpoints_as_gaps(self):
        """Mutation: `rule == target` instead of `rule in target` in `is_blocked`.

        This was the first implementation and it produced two false gaps out of
        six. In production a human checks the first claim — "we do not block
        `discover/topical_explore/`" — finds a hook that plainly does, and
        discounts the other four. The report is then worse than useless: it has
        spent the reader's trust on a claim it got wrong.
        """
        blocked = self.blocked()
        misread = [
            ("discover/topical_explore/", "discover/topical_explore"),
            ("clips/discover/stream/", "clips/discover/"),
            ("clips/discover/interest/stream/", "clips/discover/"),
        ]
        for literal, rule in misread:
            with self.subTest(literal=literal):
                self.assertNotIn(literal, blocked)  # equality finds nothing
                self.assertEqual(is_blocked(literal, blocked), rule)  # containment does

        # And it is not merely permissive: the genuine gap stays a gap.
        self.assertNotIn("feed/timeline_stream/", blocked)
        self.assertIsNone(is_blocked("feed/timeline_stream/", blocked))

    def test_a_set_membership_split_puts_a_covered_endpoint_in_novel(self):
        """Mutation: `l in wanted` for the known/novel split, containment for the gap check.

        `find_groupings` and `coverage_gaps` must apply the SAME rule. Under the
        mutation `discover/topical_explore/` — which the hook `/discover/
        topical_explore` plainly covers — lands in `Grouping.novel`, the public
        list of "endpoints this class lists that we do not block". In production
        anything reading that list reports it as a gap, and the document
        contradicts itself: the grouping shows five unblocked members while the
        candidate list carries four. Cohesion drops with it, which is the input to
        the string-pool guard.
        """
        blocked = self.blocked()
        members = set(CURATED_MEMBERS)
        real = self.sole_grouping(find_groupings(self.curated_index(), blocked))

        mutant_known = tuple(sorted(item for item in members if item in blocked))
        mutant_novel = tuple(sorted(item for item in members if item not in blocked))
        mutant = Grouping("LX/05jj;", mutant_known, mutant_novel)

        self.assertIn("discover/topical_explore/", mutant.novel)
        self.assertIn("discover/topical_explore/", real.known)
        self.assertNotIn("discover/topical_explore/", real.novel)

        # The endpoint the mutant offers as unblocked is blocked, by a named rule.
        self.assertEqual(
            is_blocked("discover/topical_explore/", blocked), "discover/topical_explore"
        )
        # The two halves of the mutant document disagree; the real one agrees.
        self.assertNotEqual(
            set(mutant.novel), {literal for literal, _ in coverage_gaps([mutant], blocked)}
        )
        self.assertEqual(
            set(real.novel), {literal for literal, _ in coverage_gaps([real], blocked)}
        )
        # And the guard's own input moves.
        self.assertLess(mutant.cohesion, real.cohesion)

    def test_without_normalise_the_stage_finds_nothing_at_all(self):
        """Mutation: `normalise` becomes the identity function.

        The manifest writes `/api/v1/clips/homecoming/`, the index holds
        `clips/homecoming/`, and nothing raises. In production this reports zero
        groupings and zero gaps on a version that added four unblocked continuous
        surfaces — a clean, confident, empty run that is indistinguishable from
        "nothing changed", which is the worst way for this stage to break.
        """
        index = self.curated_index()
        assessments, groupings = assess(index, blocking_hooks())
        self.assertEqual(len(assessments), 4)
        self.assertEqual(len(groupings), 1)

        with mock.patch.object(assessment, "normalise", lambda literal: literal):
            mutant_assessments, mutant_groupings = assess(index, blocking_hooks())
        self.assertEqual(mutant_assessments, [])
        self.assertEqual(mutant_groupings, [])
        # Nothing failed, which is exactly the problem.
        self.assertEqual(
            report(mutant_assessments, mutant_groupings)["counts"],
            {"groupings": 0, "candidates": 0, "judged": 0},
        )


# ------------------------------------------------------------------ real index


class RealIndexTests(unittest.TestCase):
    """The measured result on both real versions, which is the evidence for the design.

    The claim the whole stage rests on is that a declared grouping is found by
    content and therefore survives a version bump. That is only true if it holds on
    two real, independently obfuscated indexes — so this reads them and pins both
    the four endpoints and the two different descriptors carrying them.

    The indexes are 70 MB each and gitignored, so this skips when they are absent.
    It reads only `api_surface.json`, never the 63 MB structural file.
    """

    EXPECTED_GAPS = [
        "feed/injected_reels_media/",
        "feed/reels_media/",
        "feed/reels_media_stream/",
        "feed/timeline_stream/",
    ]
    EXPECTED_DESCRIPTORS = {"index-430": "LX/05jj;", "index-439": "LX/03Ez;"}

    def test_both_versions_yield_the_same_four_gaps_under_different_descriptors(self):
        missing = [
            str(directory)
            for directory in (INDEX_430, INDEX_439)
            if not (directory / API_SURFACE_FILENAME).is_file()
        ]
        if missing:
            self.skipTest(f"real index not built: {', '.join(missing)}")

        hooks = blocking_hooks()
        found = {}
        for directory in (INDEX_430, INDEX_439):
            with self.subTest(index=directory.name):
                index = HookIndex.load(directory)
                assessments, groupings = assess(index, hooks)

                self.assertEqual(
                    sorted(item.literal for item in assessments),
                    self.EXPECTED_GAPS,
                    f"{directory.name} no longer yields the four known gaps",
                )
                carrying = {
                    item.measured[0].detail["descriptor"] for item in assessments
                }
                self.assertEqual(len(carrying), 1, "the four gaps came from several classes")
                descriptor = carrying.pop()
                self.assertEqual(descriptor, self.EXPECTED_DESCRIPTORS[directory.name])

                grouping = next(
                    item for item in groupings if item.descriptor == descriptor
                )
                # The same nine-member curated list on both versions, found without
                # the descriptor being known in advance.
                self.assertEqual(grouping.size, 9)
                self.assertEqual(len(grouping.known), 5)
                self.assertGreater(grouping.cohesion, MIN_COHESION)
                found[directory.name] = descriptor

        self.assertEqual(found, self.EXPECTED_DESCRIPTORS)
        # The two versions name the same class differently. That is what makes the
        # cross-version agreement evidence rather than a coincidence, and it is why
        # a descriptor may never be carried across an index boundary.
        self.assertNotEqual(found["index-430"], found["index-439"])


# ------------------------------------------------------------------ known gaps


class KnownGapTests(AssessmentTestCase):
    """Characterisation tests for behaviour that is reported, not fixed.

    Each pins what the module does today. If one starts failing, the gap was
    closed and the test should be rewritten to assert the better behaviour.
    """

    def test_the_cited_rule_is_deterministic_and_most_specific(self):
        """The citation used to depend on PYTHONHASHSEED.

        `blocked_endpoints` returns a set, and `is_blocked` took the first match,
        so when several rules genuinely cover one endpoint — `clips/discover/`
        and `clips/discover/stream/` both cover the latter — the rule the report
        cited moved between runs on identical input. The boolean never wavered,
        but citing the rule is the stated reason for returning it rather than a
        bool, and the gate requires two independent derivations to agree
        byte-for-byte.

        Longest-then-lexicographic is also the more useful answer: the most
        specific rule is the one that explains why the endpoint counts as
        covered.
        """
        rules = ["clips/discover/", "clips/discover/stream/"]
        for ordering in (rules, list(reversed(rules)), set(rules)):
            with self.subTest(ordering=type(ordering).__name__):
                self.assertEqual(
                    is_blocked("clips/discover/stream/", ordering), "clips/discover/stream/"
                )

    def test_a_judgement_placed_in_measured_is_refused(self):
        """It used to be accepted, and the failure was silent.

        The module docstring claims the measured/judged split is enforced by the
        types. At runtime a frozen dataclass validated nothing, so a `Judgement`
        appended after a STRONG `Evidence` slipped past `strongest`'s
        short-circuit and was serialised into the **measured** array while the
        `judgement` key stayed null — an opinion presented as a measurement, in a
        document that still validated and whose counts still agreed.
        """
        evidence = Evidence("k", Strength.STRONG, "measured thing")
        judgement = Judgement("agent-a", Verdict.BLOCK, "because it looks addictive")
        with self.assertRaises(TypeError) as caught:
            Assessment("c", "lit", (evidence, judgement))
        self.assertIn("measured evidence must be Evidence", str(caught.exception))
        # And the legitimate shape is untouched.
        assessment = Assessment("c", "lit", (evidence,), judgement)
        self.assertEqual(assessment.to_dict()["judgement"]["actor"], "agent-a")
        self.assertEqual(len(assessment.to_dict()["measured"]), 1)


if __name__ == "__main__":
    unittest.main()

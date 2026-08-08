"""The url-block guard is generated, and the generator must be provable.

The whole point of generating `throwIfBlocked` is that a human decision recorded
in the manifest and the smali that ships can no longer disagree. That only holds
if the generator is right, and "right" here has a target: on 2026-08-08 a
five-rule, seven-path version of this method was built, signed, installed on the
owner's phone and **measured firing**. `fixtures/throw_if_blocked_device_proved.smali`
is that exact method.

So the first test is not a fixture comparison against text this module wrote — it
is a comparison against smali that was observed working on a device. Every other
test here is about refusing, because a generator that quietly emits a guard it
guessed at is worse than no generator: the request keeps flowing and the manifest
says it does not.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dfinsta_pipeline.guards import (
    BLOCK_MESSAGE,
    OBSERVE_CLASS_PATH,
    OBSERVE_DESCRIPTOR,
    OBSERVE_TAG,
    watch_from_manifest,
    watched_literals,
    write_observe_class,
    GuardError,
    Literal,
    Rule,
    apply_to_source,
    normalise,
    read_method,
    render_method,
    rules_from_manifest,
    slug,
)

REPOSITORY = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "throw_if_blocked_device_proved.smali"
REAL_SOURCE = REPOSITORY / "dfinsta_source_439/newCode/com/dfinstagram/hooks.smali"
REAL_MANIFEST = REPOSITORY / "manifest/hooks.json"

#: The five rules the device-proved method implements, written out by hand. This
#: is deliberately NOT read from the manifest: the point is to check the renderer
#: against a known-good target, and sourcing both sides from the same file would
#: only prove the manifest round-trips.
DEVICE_PROVED_RULES = (
    Rule((Literal("/feed/timeline/", "endswith"), Literal("/feed/timeline_stream/", "contains")),
         ("disable_feed",)),
    Rule((Literal("/discover/topical_explore", "contains"),), ("disable_explore",)),
    Rule((Literal("/api/v1/clips/homecoming/", "endswith"), Literal("/clips/discover", "contains")),
         ("disable_reels",)),
    Rule((Literal("/feed/reels_tray/", "endswith"),), ("disable_stories",)),
    Rule((Literal("/profile_ads/get_profile_ads/", "endswith"),), ("disable_adds",)),
)


class ReproducesTheDeviceProvedMethodTests(unittest.TestCase):
    def test_generated_output_is_the_method_that_ran_on_the_phone(self):
        """Instruction for instruction, against smali measured firing on a device.

        Compared through `normalise`, so a label rename is not a failure and a
        changed instruction is. Comparing raw text would make every cosmetic
        difference a diff line, which is how a real one gets missed.
        """
        proved = normalise(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(proved, normalise(render_method(DEVICE_PROVED_RULES)))
        # Not vacuous: the fixture is a real method, not an empty file.
        self.assertGreater(len(proved), 60)
        self.assertIn(f'const-string v1, "{BLOCK_MESSAGE}"', proved)

    def test_a_changed_instruction_is_caught_but_a_renamed_label_is_not(self):
        """The control for the comparison above, in both directions."""
        rules = list(DEVICE_PROVED_RULES)
        base = normalise(render_method(rules))

        renamed = render_method(rules).replace("cond_rule_", "cond_zzz_")
        self.assertNotEqual(render_method(rules), renamed)
        self.assertEqual(base, normalise(renamed), "a rename must not read as a change")

        rules[3] = Rule((Literal("/feed/reels_tray/", "contains"),), ("disable_stories",))
        self.assertNotEqual(base, normalise(render_method(rules)),
                            "endsWith -> contains must read as a change")

    def test_the_shipped_source_matches_the_shipped_manifest(self):
        """The invariant `guards --check` enforces, asserted here so a hand edit fails CI."""
        if not REAL_SOURCE.is_file() or not REAL_MANIFEST.is_file():
            self.skipTest("DFInsta source or manifest not present")
        rules = rules_from_manifest(REAL_MANIFEST)
        self.assertEqual(
            normalise(read_method(REAL_SOURCE)),
            normalise(render_method(rules)),
            "hooks.smali no longer matches manifest url_block_rules; run "
            "`python -m dfinsta_pipeline.guards` to regenerate",
        )

    def test_the_other_method_in_the_file_is_not_touched(self):
        """`hooks.smali` also holds `replaceReelsEndpoint`, which is a different hook."""
        if not REAL_SOURCE.is_file():
            self.skipTest("DFInsta source not present")
        self.assertIn("replaceReelsEndpoint", REAL_SOURCE.read_text(encoding="utf-8"))


class ShapeTests(unittest.TestCase):
    def test_two_toggles_render_two_independent_checks(self):
        """The any-of form, which no rule needed until 2026-08-08."""
        out = render_method((Rule((Literal("/x/", "contains"),), ("disable_feed", "disable_reels")),))
        self.assertEqual(2, out.count("getBoolTrueEz"))
        self.assertEqual(2, out.count("if-nez v2, :cond_block"))
        # One path test, not two: the literal is checked once and each toggle
        # gets its own chance to block.
        self.assertEqual(1, out.count('const-string v1, "/x/"'))

    def test_a_single_literal_rule_emits_no_short_circuit_label(self):
        """An unused label assembles fine and reads as a missing branch."""
        out = render_method((Rule((Literal("/x/"),), ("disable_feed",)),))
        self.assertNotIn("_toggle", out)

    def test_the_note_becomes_a_comment_and_not_behaviour(self):
        rules = (Rule((Literal("/x/"),), ("disable_feed",), note="why this is contains"),)
        out = render_method(rules)
        self.assertIn("# why this is contains", out)
        plain = render_method((Rule((Literal("/x/"),), ("disable_feed",)),))
        self.assertEqual(normalise(plain), normalise(out), "a note must not change behaviour")

    def test_register_count_does_not_grow_with_rules(self):
        """v0/v1/v2 are reused by every rule, so a guard can never change .locals."""
        for count in (1, 5, 40):
            rules = tuple(
                Rule((Literal(f"/p{i}/"),), ("disable_feed",)) for i in range(count)
            )
            self.assertIn(".locals 3", render_method(rules))


class RefusalTests(unittest.TestCase):
    def test_no_rules_is_refused_rather_than_emitted(self):
        with self.assertRaises(GuardError) as caught:
            render_method(())
        self.assertIn("silently block nothing", str(caught.exception))

    def test_a_rule_with_no_toggle_is_refused(self):
        with self.assertRaises(GuardError) as caught:
            Rule((Literal("/x/"),), ())
        self.assertIn("block unconditionally", str(caught.exception))

    def test_an_unknown_match_kind_is_refused(self):
        with self.assertRaises(GuardError) as caught:
            Literal("/x/", "startswith")
        self.assertIn("contains, endswith", str(caught.exception))

    def test_a_key_that_is_not_a_preference_is_refused(self):
        with self.assertRaises(GuardError):
            Rule((Literal("/x/"),), ("feed",))

    def test_an_empty_literal_is_refused_because_it_matches_everything(self):
        with self.assertRaises(GuardError) as caught:
            Literal("   ")
        self.assertIn("matches every request", str(caught.exception))

    def test_a_repeated_toggle_is_refused(self):
        with self.assertRaises(GuardError):
            Rule((Literal("/x/"),), ("disable_feed", "disable_feed"))

    def test_an_earlier_rule_that_swallows_a_later_one_is_refused(self):
        """Order is behaviour: the first match decides, so the later toggle dies.

        This is not hypothetical — `/feed/timeline/` tested with `contains` would
        swallow `/feed/timeline_stream/`, and they share a toggle today only by
        luck.
        """
        with self.assertRaises(GuardError) as caught:
            render_method((
                Rule((Literal("/feed/timeline", "contains"),), ("disable_feed",)),
                Rule((Literal("/feed/timeline_stream/", "contains"),), ("disable_reels",)),
            ))
        message = str(caught.exception)
        self.assertIn("would never", message)
        self.assertIn("/feed/timeline_stream/", message)

    def test_shadowing_under_the_SAME_toggle_is_allowed(self):
        """The control. The outcome is identical, so refusing would cry wolf."""
        out = render_method((
            Rule((Literal("/feed/timeline", "contains"),), ("disable_feed",)),
            Rule((Literal("/feed/timeline_stream/", "contains"),), ("disable_feed",)),
        ))
        self.assertIn("/feed/timeline_stream/", out)

    def test_endswith_does_not_falsely_report_swallowing(self):
        """`endsWith` only swallows a later `endsWith` with the same tail.

        Without this the four rules added on 2026-08-08 would have been refused
        for shadowing that cannot happen.
        """
        out = render_method((
            Rule((Literal("/feed/timeline/", "endswith"),), ("disable_feed",)),
            Rule((Literal("/feed/text_post_app_timeline/", "contains"),), ("disable_reels",)),
        ))
        self.assertIn("/feed/text_post_app_timeline/", out)


class SourceAndManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.tmp = Path(self.directory.name)

    def source(self, body: str) -> Path:
        path = self.tmp / "hooks.smali"
        path.write_text(body, encoding="utf-8")
        return path

    def test_applying_twice_changes_nothing_the_second_time(self):
        path = self.source(
            ".class public final Lcom/dfinstagram/hooks;\n\n"
            + FIXTURE.read_text(encoding="utf-8")
            + "\n"
        )
        rules = (Rule((Literal("/x/"),), ("disable_feed",)),)
        self.assertTrue(apply_to_source(path, rules))
        self.assertFalse(apply_to_source(path, rules), "generation must be idempotent")

    def test_two_methods_of_the_same_name_are_refused_not_half_generated(self):
        body = ".class public final Lcom/dfinstagram/hooks;\n" + (
            FIXTURE.read_text(encoding="utf-8") + "\n"
        ) * 2
        with self.assertRaises(GuardError) as caught:
            apply_to_source(self.source(body), (Rule((Literal("/x/"),), ("disable_feed",)),))
        self.assertIn("leave the other untouched", str(caught.exception))

    def test_a_manifest_with_no_rules_is_refused_rather_than_read_as_empty(self):
        path = self.tmp / "m.json"
        path.write_text(json.dumps({"hooks": [{"hook_id": "tigon_url_block"}]}), encoding="utf-8")
        with self.assertRaises(GuardError) as caught:
            rules_from_manifest(path)
        self.assertIn("not the same as a hook that blocks nothing", str(caught.exception))

    def test_an_absent_hook_is_refused(self):
        path = self.tmp / "m.json"
        path.write_text(json.dumps({"hooks": []}), encoding="utf-8")
        with self.assertRaises(GuardError):
            rules_from_manifest(path)


class DiagnosticTests(unittest.TestCase):
    """The build that answers "is this rule ever reached?" — and never ships."""

    def test_each_rule_gets_its_own_message_containing_the_canonical_string(self):
        rules = rules_from_manifest(REAL_MANIFEST) if REAL_MANIFEST.is_file() else DEVICE_PROVED_RULES
        out = render_method(rules, diagnostic=True)
        self.assertEqual(len(rules), out.count("DIAG-"))
        # The superset property: every diagnostic message still contains the
        # canonical string, so existing greps and canonical counts keep working.
        self.assertEqual(len(rules), out.count(BLOCK_MESSAGE))
        for rule in rules:
            self.assertIn(f"{BLOCK_MESSAGE} [DIAG-{slug(rule)}]", out)

    def test_diagnostic_and_shipped_are_different_methods(self):
        """So one can never be mistaken for the other by the equivalence check."""
        rules = DEVICE_PROVED_RULES
        self.assertNotEqual(
            normalise(render_method(rules)), normalise(render_method(rules, diagnostic=True))
        )

    def test_the_shipped_form_has_exactly_one_throw(self):
        """The owner's 2026-08-08 decision, asserted rather than trusted to review."""
        out = render_method(DEVICE_PROVED_RULES)
        self.assertEqual(1, out.count("throw v0"))
        self.assertEqual(1, out.count("new-instance v0, Ljava/io/IOException;"))
        self.assertNotIn("DIAG", out)


if __name__ == "__main__":
    unittest.main()


def _rules_span(method: str) -> tuple[str, ...]:
    """The block rules only, with the preamble and any observation pass removed.

    Needed because `normalise` numbers labels by first appearance, so an
    observation pass shifts every label after it — a renumbering, not a change.
    Cutting both methods at the point the rules begin and normalising each span
    independently compares the rules themselves, jump targets included.
    """
    marker = ":cond_observed_"
    if marker in method:
        cut = method.rindex(marker)
        cut = method.index("\n", cut) + 1
    else:
        cut = method.rindex("if-eqz v0, :cond_return")
        cut = method.index("\n", cut) + 1
    return normalise(method[cut:])


class ObserveModeTests(unittest.TestCase):
    """A measurement build learns what the app asks for without changing it.

    The pipeline ruled `block` on six endpoints in one sitting; one fires zero
    times and one is not a request path at all. Observation exists so the human
    at the gate sees behaviour instead of a string found in a class.
    """

    def test_an_observing_build_blocks_exactly_what_a_shipped_one_blocks(self):
        """The property everything else rests on, as a subsequence check.

        If observation could alter a single instruction of the guard, then every
        measurement taken with it would be a measurement of a different app —
        and the numbers would be quoted about the shipped one.
        """
        rules = DEVICE_PROVED_RULES
        shipped = render_method(rules)
        observing = render_method(rules, observe=watched_literals(rules))
        self.assertEqual(
            _rules_span(shipped),
            _rules_span(observing),
            "observation changed the guard's own rules",
        )
        # Not vacuous, in both directions: the spans are real, and the whole
        # methods genuinely differ.
        self.assertGreater(len(_rules_span(shipped)), 40)
        self.assertNotEqual(normalise(shipped), normalise(observing))

    def test_the_span_comparison_would_catch_a_changed_rule(self):
        """The control for the test above, which is otherwise unfalsifiable."""
        rules = list(DEVICE_PROVED_RULES)
        shipped = render_method(rules)
        rules[3] = Rule((Literal("/feed/reels_tray/", "contains"),), ("disable_stories",))
        tampered = render_method(rules, observe=watched_literals(tuple(rules)))
        self.assertNotEqual(_rules_span(shipped), _rules_span(tampered))

    def test_observation_runs_before_any_rule_can_throw(self):
        """Order is behaviour: a blocked path throws, and a throw ends the method.

        Observing after the rules would silently report zero for exactly the
        paths that are working — the ones killed before they could be counted.
        """
        out = render_method(DEVICE_PROVED_RULES, observe=("/feed/timeline/",))
        self.assertLess(
            out.index(f"{OBSERVE_DESCRIPTOR}->seen"),
            out.index("if-nez v2, :cond_block"),
        )

    def test_every_watched_path_is_reported_exactly_once(self):
        watched = ("/a/", "/b/", "/c/")
        out = render_method(DEVICE_PROVED_RULES, observe=watched)
        self.assertEqual(len(watched), out.count(f"{OBSERVE_DESCRIPTOR}->seen"))
        for literal in watched:
            self.assertIn(f'const-string v1, "{literal}"', out)

    def test_watched_literals_is_blocked_paths_then_extras_without_duplicates(self):
        watched = watched_literals(DEVICE_PROVED_RULES, ("/new/", "/feed/timeline/"))
        self.assertEqual("/feed/timeline/", watched[0])
        self.assertIn("/new/", watched)
        self.assertEqual(len(set(watched)), len(watched), "a path must not be watched twice")
        # A blocked path repeated as an extra must not appear twice — that would
        # double every count for it and make one endpoint look busier than it is.
        self.assertEqual(1, watched.count("/feed/timeline/"))

    def test_the_shipped_form_carries_no_observation_at_all(self):
        """No class reference, no log tag. A shipped APK must not announce itself."""
        out = render_method(DEVICE_PROVED_RULES)
        self.assertNotIn(OBSERVE_DESCRIPTOR, out)
        self.assertNotIn(OBSERVE_TAG, out)

    def test_the_observe_class_logs_and_never_throws(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        written = write_observe_class(Path(tmp.name))
        self.assertEqual(Path(tmp.name) / OBSERVE_CLASS_PATH, written)
        body = written.read_text(encoding="utf-8")
        self.assertIn(f'const-string v0, "{OBSERVE_TAG}"', body)
        self.assertIn("Landroid/util/Log;->i(", body)
        # It must never throw: observation that changed what the app receives
        # would not be observation. Asserted over CODE only — the class's own
        # comment explains why it never throws, and a rule this blunt should
        # keep its bluntness and have the prose moved out of its way rather than
        # be softened into one that could miss a real `throw`.
        code = "\n".join(
            line for line in body.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertNotIn("throw", code)
        self.assertNotIn("IOException", code)
        self.assertIn("IOException", body, "the reason must still be written down")

    def test_an_absent_watch_list_is_coherent_but_an_absent_hook_is_not(self):
        """Empty means "watch only what is blocked"; a missing hook is a mistake."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "m.json"
        path.write_text(json.dumps({"hooks": [{"hook_id": "tigon_url_block"}]}), encoding="utf-8")
        self.assertEqual((), watch_from_manifest(path))

        path.write_text(json.dumps({"hooks": []}), encoding="utf-8")
        with self.assertRaises(GuardError):
            watch_from_manifest(path)

    def test_the_shipped_manifest_watches_the_endpoint_it_needs_to_disprove(self):
        """`delivery/background_prefetch` is watched precisely because it should never appear.

        It was ruled `block` on 2026-08-08 and is a no-op logger's marker name,
        not a request path. Watching it turns that from a reading of one call
        site into evidence: never observed, while its neighbours are observed
        constantly, is a fact a withdrawal can cite.
        """
        if not REAL_MANIFEST.is_file():
            self.skipTest("manifest not present")
        self.assertIn("delivery/background_prefetch", watch_from_manifest(REAL_MANIFEST))

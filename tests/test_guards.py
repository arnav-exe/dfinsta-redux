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
    BLOCKED_DIRECTIVE,
    BLOCKED_METHOD,
    REPORTS,
    REPORTS_LINE,
    REPORTS_MARK,
    OBSERVE_CLASS_PATH,
    OBSERVE_DESCRIPTOR,
    OBSERVE_TAG,
    render_observe_class,
    toggles_of,
    watch_from_manifest,
    watched_literals,
    write_observe_class,
    GuardError,
    Literal,
    Rule,
    apply_to_source,
    decide,
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


#: Every state with exactly one toggle on, plus all-off and all-on. The
#: single-toggle states are the ones the exploration protocol actually walks, and
#: all-on is where two rules under different toggles could interfere.
def _states(rules) -> tuple[dict, ...]:
    keys = toggles_of(rules)
    return (
        {key: False for key in keys},
        {key: True for key in keys},
        *({key: key == one for key in keys} for one in keys),
    )


#: Request paths a decision is compared over: every literal the rules test, each
#: one under a realistic `/api/v1` prefix and bare, plus near-misses that must NOT
#: match and a path no rule mentions. Near-misses are the point — a generator that
#: turned every `endsWith` into a `contains` would pass a test that only tried
#: paths which are supposed to block.
def _paths(rules) -> tuple[str, ...]:
    out: list[str] = ["/api/v1/users/1/info/", "/", ""]
    for rule in rules:
        for literal in rule.literals:
            out += [
                literal.text,
                f"/api/v1{literal.text}",
                f"{literal.text}extra/",
                f"/prefixed{literal.text}",
                literal.text.rstrip("/"),
                literal.text.upper(),
            ]
    return tuple(dict.fromkeys(out))


def _behaviour(method: str, rules) -> dict:
    """What a rendered method decides, over every path and every state."""
    return {
        (path, tuple(sorted(state.items()))): decide(method, path, state).blocked
        for path in _paths(rules)
        for state in _states(rules)
    }


class ObserveModeTests(unittest.TestCase):
    """A measurement build learns what the app asks for without changing it.

    The pipeline ruled `block` on six endpoints in one sitting; one fires zero
    times and one is not a request path at all. Observation exists so the human
    at the gate sees behaviour instead of a string found in a class.
    """

    def test_an_observing_build_blocks_exactly_what_a_shipped_one_blocks(self):
        """The property everything else rests on, **run** rather than read.

        If observation could alter what the guard decides, then every measurement
        taken with it would be a measurement of a different app — and the numbers
        would be quoted about the shipped one.

        This used to compare the two methods' instructions. It cannot any more:
        an observing build carries a fourth register and a call the shipped one
        does not, so their text differs by construction and a text comparison
        would only be asserting that a change nobody made was not made. So both
        are executed against every path under every toggle state, which is what
        the phone would do and what the claim actually says.
        """
        rules = DEVICE_PROVED_RULES
        shipped = _behaviour(render_method(rules), rules)
        observing = _behaviour(render_method(rules, observe=watched_literals(rules)), rules)
        self.assertEqual(shipped, observing, "observation changed what the guard blocks")
        # Not vacuous in either direction: the comparison is over a real matrix in
        # which both answers occur, and the two methods genuinely do differ.
        self.assertGreater(len(shipped), 200)
        self.assertEqual({True, False}, set(shipped.values()))
        self.assertNotEqual(
            normalise(render_method(rules)),
            normalise(render_method(rules, observe=watched_literals(rules))),
        )

    def test_the_comparison_would_catch_a_changed_rule(self):
        """The control for the test above, which is otherwise unfalsifiable.

        `endsWith` to `contains` on one literal is the smallest change that alters
        behaviour without altering the instruction count, and it is exactly the
        mistake `MATCHES` exists to make impossible by hand.
        """
        rules = list(DEVICE_PROVED_RULES)
        shipped = _behaviour(render_method(tuple(rules)), tuple(rules))
        rules[3] = Rule((Literal("/feed/reels_tray/", "contains"),), ("disable_stories",))
        tampered = _behaviour(
            render_method(tuple(rules), observe=watched_literals(tuple(rules))), tuple(rules)
        )
        self.assertNotEqual(shipped, tampered)

    def test_dropping_a_rule_entirely_is_caught_too(self):
        """The other direction of the same control: fewer rules, same shape."""
        rules = DEVICE_PROVED_RULES
        shipped = _behaviour(render_method(rules), rules)
        fewer = _behaviour(render_method(rules[:-1], observe=watched_literals(rules)), rules)
        self.assertNotEqual(shipped, fewer)

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

    def test_the_observe_class_reports_the_toggle_state_it_was_built_with(self):
        """The line that makes a capture interpretable, read from the device.

        A measurement taken with the blocks on cannot answer "is this endpoint
        ever requested": blocking `/feed/timeline/` leaves no timeline response
        for Reels to be injected into, so the child never fires whatever
        Instagram would do. Measured 2026-08-08 — `/feed/injected_reels_media/`
        observed 0 times with blocks on and 3 with them off. So the build states
        which blocks were active, and it is the build rather than the operator
        because an operator-typed answer is a formality, not a safety property.
        """
        body = render_observe_class(("disable_feed", "disable_reels"))
        self.assertIn('const-string v2, "!toggles', body)
        self.assertEqual(2, body.count("->one(Ljava/lang/StringBuilder;"))
        for toggle in ("disable_feed", "disable_reels"):
            self.assertIn(f'const-string v2, "{toggle}"', body)
        # Emitted on EVERY call, deliberately. A once-per-process flag lost the
        # line entirely on the first real session: `logcat -c` runs immediately
        # before walking the app, Instagram's process is usually already alive,
        # so the single line went into the buffer that was then cleared and the
        # flag stayed set. The capture had 22 path lines and no toggle state, and
        # nothing said so. Any capture with a path line must carry the state.
        self.assertNotIn("sget-boolean", body)
        self.assertNotIn("cond_done", body)

    def test_an_observing_build_states_its_toggles_before_any_path(self):
        """Order again: a path line before the toggle line could not be attributed."""
        out = render_method(DEVICE_PROVED_RULES, observe=("/feed/timeline/",))
        self.assertLess(out.index("->state()V"), out.index(f"{OBSERVE_DESCRIPTOR}->seen"))

    def test_an_observe_class_with_no_toggles_is_refused(self):
        """It could not state what was active, so its captures answer nothing."""
        with self.assertRaises(GuardError) as caught:
            render_observe_class(())
        self.assertIn("cannot state its toggle state", str(caught.exception))

    def test_the_observe_class_logs_and_never_throws(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        written = write_observe_class(Path(tmp.name), ("disable_feed",))
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


class RecordsItsOwnRefusalsTests(unittest.TestCase):
    """A measurement build says which path it refused, instead of asking Instagram.

    The block signal used to be `java.io.IOException: Blocked by DFInsta setting`
    grepped out of logcat, which is there only because Instagram catches our
    exception and files it into its own error event. Across eight sessions on two
    Instagram versions and two walks, `/discover/topical_explore` was refused
    seven times and reported once, and six times and reported none, while
    `/feed/timeline/` reported 20/20 and 23/23 in the very same captures. The
    event is Instagram's to emit; the decision is ours, and so is recording it.
    """

    def test_the_refusal_names_the_literal_that_matched_not_the_rule(self):
        """The reason a register is spent on this at all.

        `/api/v1/clips/homecoming/` and `/clips/discover` are ONE rule under
        `disable_reels`, and `/clips/discover` is the exact path that read ERASED
        under one walk protocol and unaffected under another. Naming the rule
        would merge the contested path into its neighbour and lose the question.
        """
        rules = DEVICE_PROVED_RULES
        observing = render_method(rules, observe=watched_literals(rules))
        reels = {key: key == "disable_reels" for key in toggles_of(rules)}
        homecoming = decide(observing, "/api/v1/clips/homecoming/", reels)
        discover = decide(observing, "/api/v1/clips/discover/stream/", reels)
        self.assertTrue(homecoming.blocked and discover.blocked)
        self.assertEqual("/api/v1/clips/homecoming/", homecoming.recorded)
        self.assertEqual("/clips/discover", discover.recorded)

    def test_every_literal_can_be_named_by_a_refusal_it_caused(self):
        """Not one rule, all of them — and the name must be one that matches.

        A property rather than a re-derivation of the emission: whatever literal
        comes back, the path really does match it under that literal's own kind.
        A generator that recorded a fixed string, or the wrong rule's, fails here.
        """
        rules = DEVICE_PROVED_RULES
        observing = render_method(rules, observe=watched_literals(rules))
        every = {key: True for key in toggles_of(rules)}
        by_text = {
            literal.text: literal for rule in rules for literal in rule.literals
        }
        for text in by_text:
            path = f"/api/v1{text}"
            with self.subTest(path=path):
                outcome = decide(observing, path, every)
                self.assertTrue(outcome.blocked)
                self.assertIn(outcome.recorded, by_text)
                named = by_text[outcome.recorded]
                self.assertTrue(
                    named.text in path
                    if named.match == "contains"
                    else path.endswith(named.text),
                    f"{outcome.recorded!r} was recorded for {path!r}, which does not "
                    f"match it under {named.match}",
                )

    def test_nothing_is_recorded_when_nothing_is_refused(self):
        """A path that is watched, requested and allowed records no refusal.

        This is the zero the whole store is compared against, so it has to be a
        measured zero rather than a line nobody emitted for another reason.
        """
        rules = DEVICE_PROVED_RULES
        observing = render_method(rules, observe=watched_literals(rules))
        allowed = decide(
            observing, "/api/v1/feed/timeline/", {key: False for key in toggles_of(rules)}
        )
        self.assertFalse(allowed.blocked)
        self.assertIsNone(allowed.recorded)
        self.assertEqual(("/feed/timeline/",), allowed.observed)

    def test_a_shipped_build_records_nothing_and_names_nothing(self):
        """It must carry neither the call nor the directive nor the class."""
        out = render_method(DEVICE_PROVED_RULES)
        self.assertNotIn(BLOCKED_DIRECTIVE, out)
        self.assertNotIn(BLOCKED_METHOD, out)
        self.assertNotIn(OBSERVE_DESCRIPTOR, out)
        every = {key: True for key in toggles_of(DEVICE_PROVED_RULES)}
        refused = decide(out, "/api/v1/feed/timeline/", every)
        self.assertTrue(refused.blocked)
        self.assertIsNone(refused.recorded)

    def test_the_message_thrown_stays_one_string_whichever_rule_fired(self):
        """Owner decision, 2026-08-08, and the new signal is what makes it free.

        Instagram files our IOException into its own error event, which names
        `logview_group_by`, so a per-rule vocabulary in the message would tell
        Meta which rules a modified client carries. Rule identity now goes to our
        own log instead, where Instagram has no reason to look — so an observing
        build attributes every refusal AND throws the same string a shipped one
        does.
        """
        rules = DEVICE_PROVED_RULES
        observing = render_method(rules, observe=watched_literals(rules))
        every = {key: True for key in toggles_of(rules)}
        thrown = {
            decide(observing, f"/api/v1{literal.text}", every).message
            for rule in rules
            for literal in rule.literals
        }
        self.assertEqual({BLOCK_MESSAGE}, thrown)
        self.assertEqual(1, observing.count("throw v0"))

    def test_an_observing_build_declares_the_register_it_records_through(self):
        """`.locals` is part of the method, and smali will not assemble a lie.

        The shipped register contract is three — path, literal-or-key, boolean —
        and observation takes a fourth to carry the matched literal from the test
        that matched it down to the throw. Confined to a build that never ships,
        and stated here so a future edit that adds v3 to the shipped form fails a
        test rather than an apktool run.
        """
        rules = DEVICE_PROVED_RULES
        self.assertIn("    .locals 3\n", render_method(rules))
        self.assertIn("    .locals 4\n", render_method(rules, observe=watched_literals(rules)))

    def test_the_observe_class_states_that_it_can_report_refusals(self):
        """Without this line a capture's zero is unreadable.

        A capture with no `!blocked` line is either a state where nothing was
        refused or a build that could not have said, and every session recorded
        before 2026-08-13 is the second. A reader that could not tell them apart
        would read 48 committed sessions as proof that nothing ever blocked —
        which is the absent-versus-empty conflation this project has shipped in
        five modules.
        """
        body = render_observe_class(("disable_feed",))
        self.assertIn(f'const-string v2, "{REPORTS_LINE}"', body)
        for name in REPORTS:
            self.assertIn(f"{REPORTS_MARK}{name}", REPORTS_LINE)
        # Carried ON the toggle line, which `state()` emits on every checked
        # request — 625 times in a three-round session. A line of its own would
        # have grown every committed capture by 55% to repeat one constant, and it
        # cannot be emitted once per process: `logcat -c` runs immediately before
        # every walk, which is what lost the toggle line itself the first time.
        self.assertNotIn("sget-boolean", body)
        self.assertEqual(1, body.count("!toggles"))
        self.assertIn(f".method public static {BLOCKED_METHOD}(Ljava/lang/String;)V", body)

    def test_a_capability_mark_can_never_be_read_as_a_preference_key(self):
        """Why the two token shapes can share one line without a delimiter.

        `Rule` already refuses any toggle that does not start with `disable_`, and
        a preference key is `[A-Za-z_][A-Za-z0-9_]*` besides. A `+` cannot begin
        either, so splitting the line on token shape is exact rather than a
        convention two modules have to remember.
        """
        for name in REPORTS:
            marked = f"{REPORTS_MARK}{name}"
            self.assertFalse(marked[0].isalnum() and marked[0] != "_")
            with self.assertRaises(GuardError):
                Rule((Literal("/x/"),), (marked,))

    def test_the_recorded_line_is_the_directive_and_the_literal(self):
        """The whole method, instruction for instruction — arguments included.

        Nothing executes the observe class: `decide` covers `throwIfBlocked` and
        this class is only ever assembled and installed. So a register mistake here
        survives every other check and is found on the phone. `{v0, v0}` instead of
        `{v0, p0}` assembles, runs, and logs `!blocked !blocked ` for every refusal
        — which `observation.parse` then rejects as padded, throwing away a
        completed walk at record time rather than at build time.
        """
        body = render_observe_class(("disable_feed",))
        method = body[body.index(f".method public static {BLOCKED_METHOD}("):]
        method = method[: method.index(".end method") + len(".end method")]
        self.assertEqual(
            [
                f".method public static {BLOCKED_METHOD}(Ljava/lang/String;)V",
                "    .locals 1",
                f'    const-string v0, "{BLOCKED_DIRECTIVE} "',
                "    invoke-virtual {v0, p0}, Ljava/lang/String;->concat"
                "(Ljava/lang/String;)Ljava/lang/String;",
                "    move-result-object v0",
                f"    invoke-static {{v0}}, {OBSERVE_DESCRIPTOR}->seen(Ljava/lang/String;)V",
                "    return-void",
                ".end method",
            ],
            [line for line in method.splitlines() if line.strip()],
        )

    def test_the_observe_class_reads_the_argument_it_was_given(self):
        """Stated as a property too, so a future rewrite of the method above fails.

        The literal reaching `blocked` must be the one that comes back out. Pinned
        against the emitting side because there is no other side: this class has no
        interpreter and no test that runs it.
        """
        body = render_observe_class(("disable_feed",))
        for name, argument in ((BLOCKED_METHOD, "p0"), ("seen", "p0")):
            with self.subTest(method=name):
                span = body[body.index(f".method public static {name}("):]
                span = span[: span.index(".end method")]
                self.assertIn(
                    argument,
                    span,
                    f"{name} never reads its argument, so every line it logs is a "
                    "constant",
                )


#: Every key the device-proved rules read, all off. Spelled once: `decide`
#: refuses a state that leaves a key out, and four copies of the same dict is
#: four places for one of them to drift.
ALL_OFF_STATE = {key: False for key in toggles_of(DEVICE_PROVED_RULES)}


class DecideTests(unittest.TestCase):
    """The interpreter is load-bearing, so it has to fail loudly on what it cannot do.

    Every equivalence claim about the guard now runs through `decide`. An
    interpreter that silently skipped an instruction would report the decision the
    *rest* of the method makes, and two methods that differ only in the skipped
    instruction would compare equal — the comparison would pass by being blind.
    """

    def test_it_refuses_an_instruction_it_does_not_know(self):
        """And the refusal must come from the *dispatch*, not from the line regex.

        An earlier version of this test injected `rem-int/lit8 v2, v2, 0x3`, whose
        `/` the instruction pattern did not admit — so the line was rejected as
        unreadable and the unknown-opcode branch was never reached. Deleting that
        branch entirely left this test green. `nop` and `move` are the shape that
        matters: plain opcodes a future generator could plausibly emit, which
        would otherwise be silently skipped while `decide` reported the decision
        the rest of the method makes.
        """
        for opcode, line in (("nop", "    nop"), ("move", "    move v2, v1")):
            with self.subTest(opcode=opcode):
                method = render_method(DEVICE_PROVED_RULES).replace(
                    "    return-void", f"{line}\n    return-void", 1
                )
                with self.assertRaises(GuardError) as caught:
                    decide(method, "/api/v1/users/1/info/", ALL_OFF_STATE)
                self.assertIn(
                    "does not know the instruction",
                    str(caught.exception),
                    "rejected by the line pattern instead of the opcode dispatch",
                )
                self.assertIn(opcode, str(caught.exception))

    def test_the_opcode_dispatch_is_reachable_at_all(self):
        """The positive control for the test above.

        A line pattern narrow enough to reject every unknown opcode would pass it
        while making the dispatch unreachable, so an opcode with the awkward
        characters — `/` and digits — must read fine and be refused by name.
        """
        method = render_method(DEVICE_PROVED_RULES).replace(
            "    return-void", "    rem-int/lit8 v2, v2, 0x3\n    return-void", 1
        )
        with self.assertRaises(GuardError) as caught:
            decide(method, "/api/v1/users/1/info/", ALL_OFF_STATE)
        self.assertIn("does not know the instruction", str(caught.exception))
        self.assertIn("rem-int/lit8", str(caught.exception))

    def test_it_refuses_a_move_result_that_follows_no_invoke(self):
        """Dalvik rejects the class; a model that moved a stale value would not.

        Without this `decide` reports a decision for a method that could never
        load — the "reports blocked for something that would not throw" case, and
        the one that would make the equivalence claim meaningless.
        """
        method = render_method(DEVICE_PROVED_RULES).replace(
            "    invoke-virtual {v0, v1}, Ljava/lang/String;->endsWith"
            "(Ljava/lang/String;)Z\n\n",
            "",
            1,
        )
        with self.assertRaises(GuardError) as caught:
            decide(method, "/api/v1/feed/timeline/", ALL_OFF_STATE)
        self.assertIn("move-result", str(caught.exception))

    def test_it_refuses_a_label_nobody_defined_even_on_the_branch_not_taken(self):
        """Checked before anything runs, not when the jump happens.

        A method with a dangling target does not assemble at all, so deciding it
        on the strength of a path that avoids the jump is modelling a class that
        could not exist.
        """
        method = render_method(DEVICE_PROVED_RULES).replace(
            "if-nez v2, :cond_block", "if-nez v2, :cond_nowhere", 1
        )
        with self.assertRaises(GuardError) as caught:
            decide(method, "/api/v1/users/1/info/", ALL_OFF_STATE)
        self.assertIn("cond_nowhere", str(caught.exception))

    def test_an_empty_string_is_a_reference_and_a_null_is_not(self):
        """`if-eqz` on an object register asks "is this null", not "is this falsey".

        Asserted on `_zero` directly, and deliberately: no path through the
        device-proved rules distinguishes the two treatments, because a `""` path
        matches no literal either way. `replaceReelsEndpoint` leaves exactly `""`
        behind when `disable_reels` is on, so a model that returned early on it
        would say the guard never saw an erased request — right answer, wrong
        mechanism, and telling a block from an erasure is the whole job.
        """
        from dfinsta_pipeline.guards import _zero

        self.assertFalse(_zero(""), "an empty string is a live reference")
        self.assertFalse(_zero("/feed/timeline/"))
        self.assertTrue(_zero(None), "and null is the only null")
        self.assertTrue(_zero(0))
        self.assertFalse(_zero(1))
        self.assertTrue(_zero(False))

    def test_a_shipped_method_decides_nothing_differently_after_all_that(self):
        """The controls above inject damage; this proves the undamaged form runs."""
        self.assertTrue(
            decide(render_method(DEVICE_PROVED_RULES), "/api/v1/feed/timeline/",
                   {**ALL_OFF_STATE, "disable_feed": True}).blocked
        )
        self.assertFalse(
            decide(render_method(DEVICE_PROVED_RULES), "/api/v1/feed/timeline/",
                   ALL_OFF_STATE).blocked
        )

    def test_it_refuses_a_register_the_method_never_declared(self):
        """`.locals 3` with a v3 in it does not assemble; this says so first."""
        method = render_method(DEVICE_PROVED_RULES).replace(
            '    const-string v1, "/feed/timeline/"',
            '    const-string v3, "/feed/timeline/"',
            1,
        )
        with self.assertRaises(GuardError) as caught:
            decide(method, "/api/v1/feed/timeline/", {**ALL_OFF_STATE, "disable_feed": True})
        self.assertIn(".locals 3", str(caught.exception))

    def test_it_refuses_a_state_that_leaves_a_key_out(self):
        """Off and never-mentioned are different answers, and the arms are single-toggle."""
        with self.assertRaises(GuardError) as caught:
            decide(render_method(DEVICE_PROVED_RULES), "/api/v1/feed/timeline/", {})
        self.assertIn("disable_feed", str(caught.exception))

    def test_it_refuses_a_method_that_never_terminates(self):
        method = render_method(DEVICE_PROVED_RULES).replace(
            "    :cond_return\n    return-void",
            "    :cond_return\n    goto :cond_return",
            1,
        )
        with self.assertRaises(GuardError) as caught:
            decide(method, "/api/v1/users/1/info/", ALL_OFF_STATE)
        self.assertIn("did not terminate", str(caught.exception))

    def test_it_reads_the_diagnostic_form_too(self):
        """Which is what makes the diagnostic build checkable rather than trusted."""
        rules = DEVICE_PROVED_RULES
        every = {key: True for key in toggles_of(rules)}
        outcome = decide(render_method(rules, diagnostic=True), "/api/v1/feed/reels_tray/", every)
        self.assertTrue(outcome.blocked)
        self.assertIn("DIAG-", outcome.message)
        self.assertIn(BLOCK_MESSAGE, outcome.message)

    def test_an_empty_path_is_a_reference_and_not_a_null(self):
        """`replaceReelsEndpoint` leaves `""` behind, and `if-eqz` must not take it.

        A guard that treated an empty path as null would return early and every
        erased request would read as "the guard never saw it" for the wrong
        reason — right answer, wrong mechanism, and the two are the whole point of
        telling a block from an erasure.
        """
        rules = DEVICE_PROVED_RULES
        every = {key: True for key in toggles_of(rules)}
        self.assertEqual((), decide(render_method(rules), "", every).observed)
        self.assertFalse(decide(render_method(rules), None, every).blocked)

    def test_a_null_uri_and_a_null_path_are_two_branches(self):
        """The guard tests both, so a model that conflated them covered one.

        `p0` is the URI and `v0` is `getPath()`. Modelling p0 *as* the path made
        `path=None` take the first `if-eqz` and left the second unreachable, so a
        matrix over paths looked like it exercised a branch it never reached.
        """
        rules = DEVICE_PROVED_RULES
        observing = render_method(rules, observe=watched_literals(rules))
        every = {key: True for key in toggles_of(rules)}
        no_uri = decide(observing, "/api/v1/feed/timeline/", every, uri=False)
        self.assertFalse(no_uri.blocked)
        self.assertEqual((), no_uri.observed, "a null URI returns before observing")
        no_path = decide(observing, None, every)
        self.assertFalse(no_path.blocked)
        self.assertEqual((), no_path.observed, "and so does a null path")
        # The control: with both present the very same call blocks.
        self.assertTrue(decide(observing, "/api/v1/feed/timeline/", every).blocked)

"""Asking for a new anchor, and refusing to believe the answer without counting.

The module exists for one situation: the host resolved and its anchor did not.
442 was that — a literal moved out of its class into a shared string table, so
the `const-string` the anchor pinned stopped existing while the class, the method
and the register receiving the value all stayed put.

**The property under most protection here is that agreement is not acceptance.**
`proposer.collect` accepts what k agents agree on, which is right for a class
descriptor and wrong for an anchor: two correct anchors can differ in every
capture name and in how much context they carry, and `ask-the-agent-only-what-
varies` records k-of-n failing for exactly this reason when the question was a
patch rather than a class. So k is attempts, not votes, and a candidate is
accepted because counting says it is selective — never because a second agent
said the same thing.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dfinsta_pipeline.hook_manifest import Hook, HostFingerprint, ManifestError
from dfinsta_pipeline.reanchor import (
    ACCEPTED,
    NOT_MY_SITE,
    BAD_FORM,
    NOT_COMPILED,
    NOT_IN_HOST,
    NOT_SELECTIVE,
    Candidate,
    apply_variant,
    as_variant,
    check,
    collect,
    parse,
    prompt,
)

MARKER = "# dfinsta_probe_marker"

HOOK = Hook(
    hook_id="probe_hook",
    intent="blank the path when the toggle is on",
    tier="robust",
    strategy="endpoint_replace",
    semantic_deps=("needle/",),
    hosts=(HostFingerprint("by_literal", literal="needle/", note="n"),),
    anchor=('const-string <r:reg>, "needle/"',),
    payload=(f"    {MARKER}", "    invoke-static {<r>}, LH;->f(Ljava/lang/String;)V"),
    marker=MARKER,
    expected_marker_count=1,
    mode="replace",
    constraints=("the five-line form is what disambiguates",),
    intent_constraints=("the request must not go out",),
)

#: The host as the new version has it: the value arrives from a call, not a
#: constant, exactly as 442's pooled fetch does.
HOST_SOURCE = "\n".join([
    ".class public LX/8Ec;",
    ".super Ljava/lang/Object;",
    "",
    ".method public final A08()V",
    "    .locals 4",
    "",
    "    invoke-static {v3}, Lcom/example/Helper;->A00()Ljava/lang/String;",
    "",
    "    move-result-object v8",
    "",
    "    return-void",
    ".end method",
    "",
])

GOOD_ANCHOR = (
    "invoke-static {<c:reg>}, Lcom/example/Helper;->A00()Ljava/lang/String;",
    "move-result-object <r:reg>",
)
GOOD_PAYLOAD = (f"    {MARKER}", "    invoke-static {<r>}, LH;->f(Ljava/lang/String;)V")


def candidate(name="proposer-0", anchor=GOOD_ANCHOR, payload=GOOD_PAYLOAD, mode="insert_after"):
    return Candidate(proposer=name, anchor=anchor, payload=payload, mode=mode)


class CheckingTests(unittest.TestCase):
    """Everything here is arithmetic. No agent is asked anything."""

    def test_a_selective_candidate_is_accepted(self) -> None:
        checked = check(HOOK, candidate(), HOST_SOURCE, {"441": 0, "442": 1})
        self.assertEqual(ACCEPTED, checked.outcome, checked.reason)
        self.assertEqual(1, checked.in_host)

    def test_matching_several_classes_anywhere_is_refused(self) -> None:
        """Even though it matches once in the host. It has to serve as a
        fingerprint on the version that needs it."""
        checked = check(HOOK, candidate(), HOST_SOURCE, {"441": 4, "442": 1})
        self.assertEqual(NOT_SELECTIVE, checked.outcome)
        self.assertIn("441", checked.reason)

    def test_matching_the_host_twice_is_refused(self) -> None:
        """The patch site is one place, and `expected_anchor_count` is fixed at 1."""
        twice = HOST_SOURCE + HOST_SOURCE.split(".class public LX/8Ec;")[1]
        checked = check(HOOK, candidate(), twice, {"442": 1})
        self.assertEqual(NOT_IN_HOST, checked.outcome)
        self.assertEqual(2, checked.in_host)

    def test_matching_the_host_not_at_all_is_refused(self) -> None:
        absent = ('const-string <r:reg>, "not in this class"',)
        checked = check(HOOK, candidate(anchor=absent), HOST_SOURCE, {"442": 1})
        self.assertEqual(NOT_IN_HOST, checked.outcome)
        self.assertEqual(0, checked.in_host)

    def test_a_pattern_that_does_not_compile_is_refused_by_name(self) -> None:
        checked = check(HOOK, candidate(anchor=("move-result-object <r>",)), HOST_SOURCE, {})
        self.assertEqual(NOT_COMPILED, checked.outcome)
        self.assertIn("kind", checked.reason)

    def test_a_payload_using_a_capture_the_anchor_does_not_bind_is_refused(self) -> None:
        checked = check(
            HOOK,
            candidate(payload=(f"    {MARKER}", "    invoke-static {<zz>}, LH;->f(…)V")),
            HOST_SOURCE,
            {"442": 1},
        )
        self.assertEqual(BAD_FORM, checked.outcome)
        self.assertIn("zz", checked.reason)

    def test_a_payload_that_does_not_write_the_marker_is_refused(self) -> None:
        checked = check(
            HOOK,
            candidate(payload=("    invoke-static {<r>}, LH;->f(Ljava/lang/String;)V",)),
            HOST_SOURCE,
            {"442": 1},
        )
        self.assertEqual(BAD_FORM, checked.outcome)

    def test_an_unknown_mode_is_refused(self) -> None:
        checked = check(HOOK, candidate(mode="sideways"), HOST_SOURCE, {"442": 1})
        self.assertEqual(BAD_FORM, checked.outcome)

    def test_the_existing_forms_are_left_alone(self) -> None:
        """Checking a candidate must not disturb the hook it is checked against."""
        before = HOOK.forms
        check(HOOK, candidate(), HOST_SOURCE, {"442": 1})
        self.assertEqual(before, HOOK.forms)


class TwoBarsTests(unittest.TestCase):
    """App-wide selectivity is the bar for a FINGERPRINT, not for an anchor.

    Measured over four versions, only three of the eight shipped forms match one
    class in a whole decode. `tigon_url_block`'s matches seven, on every version,
    and always has — it works because a `named` fingerprint picks the class
    first. Demanding the strict bar everywhere asks for something harder than the
    job needs and rejects repairs that would work.
    """

    def test_the_strict_bar_is_the_default(self) -> None:
        """Because the case this exists for — 442 — had lost its host search
        along with the literal, so the new anchor had to do both jobs."""
        checked = check(HOOK, candidate(), HOST_SOURCE, {"441": 4, "442": 1})
        self.assertEqual(NOT_SELECTIVE, checked.outcome)

    def test_within_host_only_accepts_what_the_strict_bar_refuses(self) -> None:
        checked = check(
            HOOK, candidate(), HOST_SOURCE, {"441": 4, "442": 1}, fingerprint=False
        )
        self.assertEqual(ACCEPTED, checked.outcome)

    def test_the_relaxed_bar_still_requires_once_in_the_host(self) -> None:
        """It relaxes which classes may match, never how many sites in this one."""
        twice = HOST_SOURCE + HOST_SOURCE.split(".class public LX/8Ec;")[1]
        checked = check(HOOK, candidate(), twice, {"442": 1}, fingerprint=False)
        self.assertEqual(NOT_IN_HOST, checked.outcome)

    def test_the_relaxed_acceptance_says_what_it_still_depends_on(self) -> None:
        """A variant accepted this way is useless without a host fingerprint that
        resolves, and the record has to say so or the next reader will not know."""
        checked = check(
            HOOK, candidate(), HOST_SOURCE, {"441": 4, "442": 1}, fingerprint=False
        )
        self.assertIn("host fingerprint", checked.reason)


class AcceptanceIsNotAgreementTests(unittest.TestCase):
    def run_with(self, answers, counts=None):
        counts = counts or {"441": 0, "442": 1}
        proposers = {
            f"proposer-{index}": (lambda _prompt, reply=reply: reply)
            for index, reply in enumerate(answers)
        }
        return collect(
            HOOK, "LX/8Ec;", HOST_SOURCE, "442", proposers, lambda anchor: counts
        )

    def answer(self, anchor=GOOD_ANCHOR, payload=GOOD_PAYLOAD, mode="insert_after"):
        return json.dumps({"anchor": list(anchor), "payload": list(payload), "mode": mode})

    def test_one_proposer_is_enough_when_counting_accepts_it(self) -> None:
        """No second opinion is sought, because there is nothing for it to add."""
        run = self.run_with([self.answer()])
        self.assertEqual(1, len(run.accepted))
        self.assertIsNotNone(run.winner)

    def test_two_proposers_agreeing_on_a_bad_anchor_are_both_refused(self) -> None:
        """The case agreement gets wrong. Both said the same thing; it matches
        four classes on an older version, so both are rejected."""
        run = self.run_with([self.answer(), self.answer()], counts={"441": 4, "442": 1})
        self.assertEqual((), run.accepted)
        self.assertTrue(all(item.outcome == NOT_SELECTIVE for item in run.checked))

    def test_disagreeing_proposers_can_both_be_accepted(self) -> None:
        """Two different anchors that both pin the site are both correct."""
        longer = (
            "invoke-static {<c:reg>}, Lcom/example/Helper;->A00()Ljava/lang/String;",
            "move-result-object <r:reg>",
        )
        run = self.run_with([self.answer(), self.answer(anchor=longer)])
        self.assertEqual(2, len(run.accepted))

    def test_the_shortest_accepted_answer_wins(self) -> None:
        """A tie-break, not a judgement: fewer lines is less to break next
        version, and every survivor already passed the same counting."""
        long_anchor = (
            "invoke-static {<c:reg>}, Lcom/example/Helper;->A00()Ljava/lang/String;",
            "move-result-object <r:reg>",
        )
        short = ("move-result-object <r:reg>",)
        run = self.run_with([self.answer(anchor=long_anchor), self.answer(anchor=short)])
        self.assertEqual(2, len(run.accepted))
        assert run.winner is not None
        self.assertEqual(short, run.winner.candidate.anchor)

    def test_a_proposer_that_fails_is_recorded_and_dropped(self) -> None:
        def boom(_prompt):
            raise RuntimeError("the runtime fell over")

        run = collect(
            HOOK, "LX/8Ec;", HOST_SOURCE, "442",
            {"good": lambda _p: self.answer(), "bad": boom},
            lambda anchor: {"442": 1},
        )
        self.assertEqual(1, len(run.checked))
        self.assertEqual(1, len(run.failures))
        self.assertIn("the runtime fell over", run.failures[0])

    def test_every_answer_is_recorded_not_only_the_winner(self) -> None:
        """A run that kept only the winner could not say afterwards whether the
        others were near-misses or nonsense — which is the difference between
        "write a better prompt" and "there is nothing to find here"."""
        absent = ('const-string <r:reg>, "not in this class"',)
        run = self.run_with([self.answer(), self.answer(anchor=absent)])
        self.assertEqual(2, len(run.checked))
        self.assertEqual({ACCEPTED, NOT_IN_HOST}, {item.outcome for item in run.checked})


class ParsingTests(unittest.TestCase):
    def test_the_last_object_wins(self) -> None:
        """An agent that revises itself leaves the draft behind, and the draft is
        the answer it decided against."""
        text = (
            'first thoughts {"anchor": ["nop"], "payload": ["x"], "mode": "replace"} '
            'on reflection {"anchor": ["move-result-object <r:reg>"], '
            '"payload": ["y"], "mode": "insert_after"}'
        )
        self.assertEqual(("move-result-object <r:reg>",), parse("p", text).anchor)

    def test_an_unknown_field_is_refused(self) -> None:
        with self.assertRaises(Exception):
            parse("p", json.dumps({"anchor": ["nop"], "payload": ["x"],
                                   "mode": "replace", "descriptor": "LX/8Ec;"}))

    def test_a_missing_field_is_refused(self) -> None:
        with self.assertRaises(Exception):
            parse("p", json.dumps({"anchor": ["nop"], "payload": ["x"]}))

    def test_an_empty_anchor_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            parse("p", json.dumps({"anchor": [], "payload": ["x"], "mode": "replace"}))

    def test_evidence_is_optional(self) -> None:
        answer = parse("p", json.dumps(
            {"anchor": ["nop"], "payload": ["x"], "mode": "replace"}
        ))
        self.assertEqual((), answer.evidence)


class PromptTests(unittest.TestCase):
    def test_it_withholds_what_last_version_s_patch_looked_like(self) -> None:
        """`constraints` describes the answer's SHAPE, and the shape is precisely
        what changed. `intent_constraints` says what the patch must achieve and
        is safe."""
        text = prompt(HOOK, "LX/8Ec;", HOST_SOURCE, "442")
        self.assertNotIn("the five-line form is what disambiguates", text)
        self.assertIn("the request must not go out", text)

    def test_it_hands_over_the_class_rather_than_asking_for_it(self) -> None:
        text = prompt(HOOK, "LX/8Ec;", HOST_SOURCE, "442")
        self.assertIn("LX/8Ec;", text)
        self.assertIn("invoke-static {v3}, Lcom/example/Helper;->A00()", text)
        self.assertIn("You do not need to find the class", text)

    def test_it_says_the_answer_will_be_counted_not_read(self) -> None:
        text = prompt(HOOK, "LX/8Ec;", HOST_SOURCE, "442")
        self.assertIn("counted, not read", text)
        self.assertIn("exactly once", text)

    def test_it_warns_off_the_two_things_that_rot(self) -> None:
        text = prompt(HOOK, "LX/8Ec;", HOST_SOURCE, "442")
        self.assertIn("obfuscator cannot touch", text)
        self.assertIn("renumber every release", text)

    def test_it_carries_the_marker_and_the_count(self) -> None:
        text = prompt(HOOK, "LX/8Ec;", HOST_SOURCE, "442")
        self.assertIn(MARKER, text)


class ApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.manifest = Path(self.directory.name) / "hooks.json"
        self.manifest.write_text(
            json.dumps({"hooks": [{"hook_id": "probe_hook", "anchor": ["nop"]}]}, indent=2),
            encoding="utf-8",
        )
        self.variant = as_variant(
            check(HOOK, candidate(), HOST_SOURCE, {"442": 1}), "because 442"
        )

    def test_it_appends_a_variant(self) -> None:
        apply_variant(self.manifest, "probe_hook", self.variant)
        data = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(1, len(data["hooks"][0]["variants"]))
        self.assertEqual("because 442", data["hooks"][0]["variants"][0]["note"])

    def test_the_same_anchor_twice_is_refused(self) -> None:
        """A re-run is not a change, and two forms that both match is exactly
        what the resolver refuses — on a hook that was working before this ran."""
        apply_variant(self.manifest, "probe_hook", self.variant)
        with self.assertRaises(ValueError) as caught:
            apply_variant(self.manifest, "probe_hook", self.variant)
        self.assertIn("already carries", str(caught.exception))

    def test_an_unknown_hook_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            apply_variant(self.manifest, "no_such_hook", self.variant)

    def test_a_rejected_candidate_cannot_become_a_variant(self) -> None:
        absent = ('const-string <r:reg>, "not in this class"',)
        rejected = check(HOOK, candidate(anchor=absent), HOST_SOURCE, {"442": 1})
        with self.assertRaises(ValueError):
            as_variant(rejected, "no")


class ItRefusesRatherThanGuessesTests(unittest.TestCase):
    """The module boundary: what it will not decide on its own."""

    def test_a_hook_with_no_forms_left_to_add_is_still_constructible(self) -> None:
        """Sanity: adding a variant to a hook that already has one is allowed —
        a version can change shape twice."""
        with_one = Hook(
            hook_id=HOOK.hook_id, intent=HOOK.intent, tier=HOOK.tier,
            strategy=HOOK.strategy, semantic_deps=HOOK.semantic_deps, hosts=HOOK.hosts,
            anchor=HOOK.anchor, payload=HOOK.payload, marker=HOOK.marker,
            expected_marker_count=HOOK.expected_marker_count, mode=HOOK.mode,
            variants=(candidate().form(),),
        )
        checked = check(with_one, candidate(anchor=("move-result-object <r:reg>",)),
                        HOST_SOURCE, {"442": 1})
        self.assertEqual(ACCEPTED, checked.outcome, checked.reason)

    def test_a_hook_needing_supplied_captures_is_refused_by_the_manifest(self) -> None:
        """Variants and supplied captures together are unsupported, and the
        refusal comes from `Hook` rather than from a rule restated here."""
        from dfinsta_pipeline.hook_manifest import CaptureSupply

        supplied = Hook(
            hook_id=HOOK.hook_id, intent=HOOK.intent, tier=HOOK.tier,
            strategy=HOOK.strategy, semantic_deps=HOOK.semantic_deps, hosts=HOOK.hosts,
            anchor=HOOK.anchor,
            payload=(f"    {MARKER}", "    invoke-static {<r>}, LH;->f(<g>)V"),
            marker=HOOK.marker, expected_marker_count=1, mode=HOOK.mode,
            supplied_captures=(
                CaptureSupply.from_dict(
                    {"provides": [{"name": "g", "kind": "type"}], "suppliers": ["agent"]}
                ),
            ),
        )
        checked = check(supplied, candidate(), HOST_SOURCE, {"442": 1})
        self.assertEqual(BAD_FORM, checked.outcome)
        self.assertIn("supplied_captures", checked.reason)


class CliTests(unittest.TestCase):
    """Reading the artefact the failure produces, and finding the class in it."""

    def setUp(self) -> None:
        import importlib.util
        import sys

        spec = importlib.util.spec_from_file_location(
            "reanchor_cli", Path(__file__).resolve().parents[1] / "tools" / "reanchor.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["reanchor_cli"] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop("reanchor_cli", None)
        self.cli = module
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def report(self, resolutions):
        return {"decode": str(self.root / "decode"), "resolutions": resolutions}

    def test_the_single_candidate_is_the_host(self) -> None:
        """The hook did not resolve, so `descriptor` is empty and the class is
        whatever its host search returned."""
        host, decode = self.cli.host_from(
            self.report([{
                "hook_id": "h", "descriptor": None,
                "searches": [{"candidates": ["LX/8Ec;"]}],
            }]),
            "h",
        )
        self.assertEqual("LX/8Ec;", host)
        self.assertEqual(self.root / "decode", decode)

    def test_a_resolved_descriptor_wins_over_the_search(self) -> None:
        host, _ = self.cli.host_from(
            self.report([{
                "hook_id": "h", "descriptor": "LX/Real;",
                "searches": [{"candidates": ["LX/Other;"]}],
            }]),
            "h",
        )
        self.assertEqual("LX/Real;", host)

    def test_two_candidate_hosts_are_refused_rather_than_picked_from(self) -> None:
        """This repairs an anchor inside a known class. A hook with two possible
        hosts has a different problem, and `--discover-hosts` is that one."""
        with self.assertRaises(SystemExit) as caught:
            self.cli.host_from(
                self.report([{
                    "hook_id": "h", "descriptor": None,
                    "searches": [{"candidates": ["LX/A;", "LX/B;"]}],
                }]),
                "h",
            )
        self.assertIn("2 candidate host(s)", str(caught.exception))

    def test_a_hook_not_in_the_report_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            self.cli.host_from(self.report([]), "missing")

    def test_a_class_line_is_matched_by_its_end_not_a_fixed_prefix(self) -> None:
        """442's host really says `.class public final LX/8Ec;`. Matching
        `.class public <descriptor>` found nothing and reported it as the class
        being absent from the decode."""
        self.assertTrue(
            self.cli.declares(".class public final LX/8Ec;\n.super Lj;", "LX/8Ec;")
        )
        self.assertTrue(self.cli.declares(".class LX/8Ec;", "LX/8Ec;"))
        self.assertFalse(self.cli.declares(".class public final LX/8Ecc;", "LX/8Ec;"))
        self.assertFalse(self.cli.declares("# nothing here", "LX/8Ec;"))

    def test_the_class_body_is_found_under_any_smali_directory(self) -> None:
        path = self.root / "decode" / "smali_classes4" / "X"
        path.mkdir(parents=True)
        (path / "8Ec.smali").write_text(
            ".class public final LX/8Ec;\n.super Lj;\n", encoding="utf-8"
        )
        self.assertIn("LX/8Ec;", self.cli.source_of("LX/8Ec;", self.root / "decode"))

    def test_a_name_that_matches_no_class_is_refused(self) -> None:
        (self.root / "decode").mkdir()
        with self.assertRaises(SystemExit) as caught:
            self.cli.source_of("LX/Nope;", self.root / "decode")
        self.assertIn("0 file(s)", str(caught.exception))


class TheWrongEndpointInTheRightClassTests(unittest.TestCase):
    """The finding a live run produced, kept as a test.

    Three real proposers were asked to re-derive 442's repair on 2026-08-16. Two
    rediscovered the hand-written anchor — same landmark, different capture names,
    which is why agreement was never the acceptance rule. The third read the
    discover literal as having been RENAMED to `clips/discover/stream/` and
    anchored on the stream site, which `replace_reels_stream_endpoint` already
    patches.

    **It passed every check there was.** It compiled, matched exactly once inside
    the host, was selective across every decode, and its form constructed. Two
    hooks would have patched one site, with a different marker each so nothing
    collided, and the endpoint the hook exists for would never have been blanked.
    Counting is necessary and it is not sufficient.
    """

    #: Verbatim from the run. Capture names and all.
    CORRECT = (
        "invoke-static {<sess:reg>}, Lcom/instagram/clips/api/ClipsApiUtilHelper;"
        "->A00(Lcom/instagram/common/session/UserSession;)<reqtype:type>",
        "move-result-object <req:reg>",
        "sget-object <enumreg:reg>, <enumcls:type>-><enumfield:member>:Ljava/lang/Integer;",
        "const/16 <idx:reg>, <idxval:any>",
        "invoke-static {<idx>}, <pool:type>-><poolm:member>(I)Ljava/lang/String;",
        "move-result-object <r:reg>",
    )
    WRONG_SITE = (
        'const-string <r:reg>, "clips/discover/stream/"',
        "goto/16 <l:any>",
    )

    def setUp(self) -> None:
        self.host = "\n".join([
            ".class public final LX/8Ec;",
            ".super Ljava/lang/Object;",
            "",
            ".method public final A0A()V",
            "    .locals 4",
            "",
            '    const-string v9, "clips/discover/stream/"',
            "",
            "    goto/16 :goto_2",
            "",
            "    return-void",
            ".end method",
            "",
        ])
        self.stream_hook = Hook(
            hook_id="replace_reels_stream_endpoint",
            intent="blank the stream path",
            tier="robust",
            strategy="endpoint_replace",
            semantic_deps=("clips/discover/stream/",),
            hosts=(HostFingerprint("by_literal", literal="clips/discover/stream/", note="n"),),
            anchor=('const-string <r:reg>, "clips/discover/stream/"',),
            payload=("    # dfinsta_reels_stream_endpoint",),
            marker="# dfinsta_reels_stream_endpoint",
            expected_marker_count=1,
            mode="replace",
        )
        self.discover_hook = Hook(
            hook_id="replace_reels_discover_endpoint",
            intent="blank the discover path",
            tier="robust",
            strategy="endpoint_replace",
            semantic_deps=("clips/discover/",),
            hosts=(HostFingerprint("by_literal", literal="clips/discover/", note="n"),),
            anchor=('const-string <r:reg>, "clips/discover/"',),
            payload=("    # dfinsta_reels_discover_endpoint",
                     "    invoke-static {<r>}, LH;->f(Ljava/lang/String;)V"),
            marker="# dfinsta_reels_discover_endpoint",
            expected_marker_count=1,
            mode="replace",
        )

    def judge(self, anchor, others):
        return check(
            self.discover_hook,
            Candidate(
                "live",
                anchor,
                ("    # dfinsta_reels_discover_endpoint",
                 "    invoke-static {<r>}, LH;->f(Ljava/lang/String;)V"),
                "replace",
            ),
            self.host,
            {"442": 1},
            others=others,
        )

    def test_without_the_other_hooks_the_wrong_site_is_accepted(self) -> None:
        """The control, and the state this was in before the live run. Every
        count says yes."""
        self.assertEqual(ACCEPTED, self.judge(self.WRONG_SITE, others=()).outcome)

    def test_with_them_it_is_refused_and_names_the_hook_it_would_collide_with(self) -> None:
        checked = self.judge(self.WRONG_SITE, others=(self.stream_hook, self.discover_hook))
        self.assertEqual(NOT_MY_SITE, checked.outcome)
        self.assertIn("replace_reels_stream_endpoint", checked.reason)

    def test_a_hook_does_not_collide_with_itself(self) -> None:
        """Its own old form matches its own old site; that is not a collision, it
        is the thing being replaced."""
        checked = check(
            self.stream_hook,
            Candidate("live", self.WRONG_SITE,
                      ("    # dfinsta_reels_stream_endpoint",), "replace"),
            self.host,
            {"442": 1},
            others=(self.stream_hook, self.discover_hook),
        )
        self.assertEqual(ACCEPTED, checked.outcome, checked.reason)

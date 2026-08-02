"""Tests for the version-independent hook manifest and its pattern engine.

The synthetic smali in this file is deliberately tiny and hand-written. The point
of `hook_manifest` is that it resolves against *any* decode, so binding it to the
multi-gigabyte 430/439 decodes in a unit test would only make the tests slow and
version-locked. The one test that does touch a real artifact reads
`manifest/hooks.json`, which is small and checked in.

`KnownGapTests` holds characterisation tests: they pin behaviour that is arguably
wrong so that a future fix fails loudly rather than silently changing what the
resolver emits. Eight such gaps have since been fixed; those tests now assert the
corrected behaviour and their docstrings record what the bug was and what it
would have cost, because "this was once wrong" is the reason the assertion exists.
"""

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

from dfinsta_pipeline.hook_manifest import (
    CAPTURE,
    KIND_PATTERNS,
    PROBE_KINDS,
    RESERVED_CAPTURE_NAMES,
    Hook,
    HostFingerprint,
    ManifestError,
    Probe,
    Resolution,
    compile_anchor,
    compile_pattern,
    load_manifest,
    render,
    resolve_in_source,
    significant,
    strip_comment,
)
from dfinsta_pipeline.runtime_identity import instrument, is_instrumented, probe_call


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "manifest" / "hooks.json"
RECONSTRUCTION_TOOLS = ROOT / "tools" / "reconstruction"


def kind_pattern(kind: str) -> re.Pattern[str]:
    """Compile a bare `<x:kind>` capture so one kind can be probed in isolation."""
    pattern, names = compile_pattern(f"<x:{kind}>")
    assert names == ["x"]
    return pattern


def make_hook(**overrides: object) -> Hook:
    """A minimal valid hook; individual fields overridden per test."""
    fields: dict[str, object] = {
        "hook_id": "probe_hook",
        "intent": "test",
        "tier": "robust",
        "strategy": "test_strategy",
        "semantic_deps": (),
        "hosts": (HostFingerprint("named", descriptor="LFoo;"),),
        "anchor": ('const-string <r:reg>, "needle"',),
        "payload": ("    invoke-static {<r>}, LH;->f(Ljava/lang/String;)V",),
        "marker": "LH;->f(Ljava/lang/String;)V",
        "expected_marker_count": 1,
    }
    fields.update(overrides)
    return Hook(**fields)  # type: ignore[arg-type]


class CapturePatternTests(unittest.TestCase):
    """The `<name:kind>` lexer itself, before any regex is built."""

    def test_declaration_with_kind_is_a_capture(self):
        self.assertEqual(CAPTURE.findall("<app:reg>"), [("app", "reg")])

    def test_bare_reference_is_a_capture_with_no_kind(self):
        self.assertEqual(CAPTURE.findall("<app>"), [("app", "")])

    def test_names_are_lowercase_snake_only(self):
        self.assertEqual(CAPTURE.findall("<a_b2:type>"), [("a_b2", "type")])
        # An uppercase or digit-leading name is not a capture at all; it stays literal.
        self.assertEqual(CAPTURE.findall("<App:reg>"), [])
        self.assertEqual(CAPTURE.findall("<2nd:reg>"), [])

    def test_register_list_braces_do_not_collide(self):
        line = "invoke-static {v0, v1}, LFoo;->f(II)V"
        self.assertEqual(CAPTURE.findall(line), [])

    def test_unknown_kind_is_not_recognised_as_a_declaration(self):
        # `<x:label>` has no valid kind, so the lexer does not treat it as a capture.
        self.assertEqual(CAPTURE.findall("<x:label>"), [])

    def test_all_four_kinds_are_known(self):
        self.assertEqual(set(KIND_PATTERNS), {"reg", "type", "member", "any"})


class KindMatchingTests(unittest.TestCase):
    """Each kind must accept the smali tokens it names and reject the others."""

    def test_reg_accepts_locals_and_parameters(self):
        pattern = kind_pattern("reg")
        for good in ("v0", "v1", "v10", "p0", "p1", "p12"):
            with self.subTest(good=good):
                self.assertTrue(pattern.match(good))

    def test_reg_rejects_non_registers(self):
        pattern = kind_pattern("reg")
        for bad in ("LX/Foo;", "A08", "onCreate", "v", "p", "0", "vv0", "v0;", "w0", "v0x"):
            with self.subTest(bad=bad):
                self.assertIsNone(pattern.match(bad))

    def test_type_accepts_descriptors(self):
        pattern = kind_pattern("type")
        for good in (
            "LX/05ez;",
            "LX/03AS;",
            "Lcom/instagram/app/InstagramAppShell;",
            "Lcom/instagram/Foo$Bar;",
            "Ljava/net/URI;",
        ):
            with self.subTest(good=good):
                self.assertTrue(pattern.match(good))

    def test_type_rejects_non_descriptors(self):
        pattern = kind_pattern("type")
        for bad in ("v0", "A08", "LX/Foo", "L;", "X/Foo;", "Lfoo;bar;", ""):
            with self.subTest(bad=bad):
                self.assertIsNone(pattern.match(bad))

    def test_type_accepts_array_descriptors(self):
        """Was a real gap: `type` was `L[^;\\s]+;` and rejected every array.

        `[Ljava/lang/String;` is exactly how a `String[]` parameter is spelled, so
        an anchor over one could not use `type` at all. The only way to write it
        was `any` (`\\S+`), which matches punctuation and register lists too — i.e.
        the bug pushed authors from a checked kind onto an unchecked one.
        """
        pattern = kind_pattern("type")
        for good in ("[Ljava/lang/String;", "[[Lcom/instagram/Foo;", "[LX/05ez;"):
            with self.subTest(good=good):
                self.assertTrue(pattern.match(good))

    def test_type_accepts_primitive_array_descriptors(self):
        # The leading `[` is allowed only in front of an `L...;` object name, so
        # `[I` and friends remain out of reach. Pinned so the limit is known rather
        # than discovered by an anchor that silently never matches.
        pattern = kind_pattern("type")
        for good in ("[I", "[[B"):
            with self.subTest(good=good):
                self.assertIsNotNone(re.compile(KIND_PATTERNS["type"]).fullmatch(good))
        for bad in ("[", "[;", "[LFoo"):
            with self.subTest(bad=bad):
                self.assertIsNone(pattern.match(bad))

    def test_member_accepts_field_and_method_names(self):
        pattern = kind_pattern("member")
        for good in ("A08", "onCreate", "_hidden", "$r8$lambda$x", "a"):
            with self.subTest(good=good):
                self.assertTrue(pattern.match(good))

    def test_member_rejects_descriptors_and_punctuation(self):
        pattern = kind_pattern("member")
        for bad in ("LX/Foo;", "0abc", "a-b", "a.b", "a b", ""):
            with self.subTest(bad=bad):
                self.assertIsNone(pattern.match(bad))

    def test_any_accepts_one_non_space_token(self):
        pattern = kind_pattern("any")
        for good in ("v0", "LX/Foo;", "0x7f0a0123", "-0x1", ":cond_0"):
            with self.subTest(good=good):
                self.assertTrue(pattern.match(good))

    def test_any_rejects_whitespace_and_empty(self):
        pattern = kind_pattern("any")
        for bad in ("a b", "", " "):
            with self.subTest(bad=bad):
                self.assertIsNone(pattern.match(bad))


class CompilePatternTests(unittest.TestCase):
    def test_returns_capture_names_in_declaration_order(self):
        pattern, names = compile_pattern(
            "iget-object <uri:reg>, <req:reg>, <reqcls:type>-><urifield:member>:Ljava/net/URI;"
        )
        self.assertEqual(names, ["uri", "req", "reqcls", "urifield"])
        match = pattern.match(
            "iget-object v2, p1, LX/03AS;->A00:Ljava/net/URI;"
        )
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(
            [match.group(f"g{i}") for i in range(1, 5)],
            ["v2", "p1", "LX/03AS;", "A00"],
        )

    def test_literal_text_is_escaped_not_interpreted(self):
        # The `.` and `(` here must be literal, and `$` must not act as an anchor.
        pattern, names = compile_pattern("invoke-super {<a:reg>}, LFoo$Bar;->f()V")
        self.assertEqual(names, ["a"])
        self.assertTrue(pattern.match("invoke-super {v0}, LFoo$Bar;->f()V"))
        self.assertIsNone(pattern.match("invoke-super {v0}, LFooXBar;->f()V"))

    def test_pattern_is_anchored_at_both_ends(self):
        pattern, _ = compile_pattern("nop <a:reg>")
        self.assertIsNone(pattern.match("prefix nop v0"))
        self.assertIsNone(pattern.match("nop v0 trailing"))
        self.assertTrue(pattern.match("nop v0"))

    def test_line_with_no_captures_is_an_exact_literal_match(self):
        pattern, names = compile_pattern(":try_start_0")
        self.assertEqual(names, [])
        self.assertTrue(pattern.match(":try_start_0"))
        self.assertIsNone(pattern.match(":try_start_1"))

    def test_repeat_within_one_line_becomes_a_backreference(self):
        pattern, names = compile_pattern(
            "invoke-static {<r:reg>, <r>}, LFoo;->f(II)V"
        )
        # The repeat does not introduce a second binding.
        self.assertEqual(names, ["r"])
        self.assertIn("(?P=g1)", pattern.pattern)

    def test_within_line_backreference_rejects_differing_registers(self):
        pattern, _ = compile_pattern("invoke-static {<r:reg>, <r>}, LFoo;->f(II)V")
        self.assertTrue(pattern.match("invoke-static {v3, v3}, LFoo;->f(II)V"))
        self.assertIsNone(pattern.match("invoke-static {v3, v4}, LFoo;->f(II)V"))

    def test_first_bare_use_of_an_undeclared_name_raises(self):
        with self.assertRaises(ManifestError) as caught:
            compile_pattern('const-string <r>, "x"')
        self.assertIn("must declare a kind", str(caught.exception))
        self.assertIn("'r'", str(caught.exception))

    def test_redeclaring_a_kind_within_the_same_line_raises(self):
        with self.assertRaises(ManifestError) as caught:
            compile_pattern("move <r:reg>, <r:reg>")
        self.assertIn("re-declared with a kind", str(caught.exception))

    def test_redeclaring_a_kind_with_a_conflicting_kind_on_a_later_line_raises(self):
        """Was a real bug: the conflict check only consulted the per-line `seen` map.

        `seen` is rebuilt for every line, so `<r:reg>` on line one and `<r:type>`
        on line two both compiled. One capture name then meant two different things
        inside one anchor, `kinds` kept only the first, and the resolver's
        cross-line agreement check compared a register against a descriptor and
        simply never matched — a hook that silently could not resolve.
        """
        kinds: dict[str, str] = {}
        compile_pattern('const-string <r:reg>, "x"', kinds)
        with self.assertRaises(ManifestError) as caught:
            compile_pattern("invoke-static {<r:type>}, LF;->g()V", kinds)
        message = str(caught.exception)
        self.assertIn("'r'", message)
        self.assertIn("'reg'", message)
        self.assertIn("'type'", message)
        self.assertIn("one name must have one kind", message)

    def test_repeating_the_same_kind_on_a_later_line_is_tolerated(self):
        # Only a *conflicting* re-declaration raises. Restating the kind it already
        # has is redundant but harmless, and each line still captures into its own
        # group so the resolver keeps checking the two occurrences agree.
        kinds: dict[str, str] = {}
        compile_pattern("new-instance <l:reg>, LFoo;", kinds)
        pattern, names = compile_pattern("invoke-virtual {<l:reg>}, LFoo;->go()V", kinds)
        self.assertEqual(names, ["l"])
        self.assertEqual(kinds, {"l": "reg"})
        self.assertTrue(pattern.match("invoke-virtual {v0}, LFoo;->go()V"))

    def test_declared_kinds_are_written_back_into_the_shared_dict(self):
        kinds: dict[str, str] = {}
        compile_pattern("new-instance <l:reg>, <cls:type>", kinds)
        self.assertEqual(kinds, {"l": "reg", "cls": "type"})

    def test_supplied_kinds_resolve_a_bare_first_use(self):
        pattern, names = compile_pattern("move-result-object <r>", {"r": "reg"})
        self.assertEqual(names, ["r"])
        self.assertTrue(pattern.match("move-result-object v1"))
        self.assertIsNone(pattern.match("move-result-object LX/Foo;"))

    def test_a_bare_repeat_of_a_threaded_name_is_still_a_backreference(self):
        # `<cfg>` first, then `<cfg>` again on the same line: the second is a
        # backreference even though the kind came from an earlier line.
        pattern, names = compile_pattern(
            "iput <cfg>, <cfg>, LFoo;->a:I", {"cfg": "reg"}
        )
        self.assertEqual(names, ["cfg"])
        self.assertTrue(pattern.match("iput v2, v2, LFoo;->a:I"))
        self.assertIsNone(pattern.match("iput v2, v3, LFoo;->a:I"))


class CompileAnchorTests(unittest.TestCase):
    def test_compiles_one_entry_per_line(self):
        compiled = compile_anchor(
            [
                ":try_start_0",
                "iget-object <uri:reg>, <req:reg>, <cls:type>-><f:member>:Ljava/net/URI;",
            ]
        )
        self.assertEqual(len(compiled), 2)
        self.assertEqual(compiled[0][1], [])
        self.assertEqual(compiled[1][1], ["uri", "req", "cls", "f"])

    def test_kinds_thread_across_lines_so_a_later_bare_use_works(self):
        compiled = compile_anchor(
            [
                "new-instance <l:reg>, <cls:type>",
                "invoke-virtual {<l>}, LBar;->go()V",
            ]
        )
        second_pattern, second_names = compiled[1]
        self.assertEqual(second_names, ["l"])
        self.assertTrue(second_pattern.match("invoke-virtual {v0}, LBar;->go()V"))
        # It inherited kind `reg`, so a descriptor in that slot is rejected.
        self.assertIsNone(second_pattern.match("invoke-virtual {LX/A;}, LBar;->go()V"))

    def test_each_line_recaptures_a_threaded_name_into_its_own_group(self):
        # Lines are matched independently, so cross-line agreement cannot be a
        # regex backreference; it must be re-captured and checked by the resolver.
        compiled = compile_anchor(
            ["new-instance <l:reg>, LFoo;", "invoke-direct {<l>}, LFoo;-><init>()V"]
        )
        self.assertIn("(?P<g1>", compiled[1][0].pattern)
        self.assertNotIn("(?P=", compiled[1][0].pattern)

    def test_bare_first_use_on_the_first_line_still_raises(self):
        with self.assertRaises(ManifestError):
            compile_anchor(["move-result-object <r>", "nop"])

    def test_one_name_cannot_carry_two_kinds_across_the_anchor(self):
        # `compile_anchor` is what threads `kinds` between lines, so this is the
        # path a real manifest takes into the cross-line conflict check.
        with self.assertRaises(ManifestError) as caught:
            compile_anchor(
                [
                    'const-string <r:reg>, "clips/discover/"',
                    "invoke-static {<r:type>}, LF;->g()V",
                ]
            )
        self.assertIn("one name must have one kind", str(caught.exception))

    def test_real_manifest_anchors_all_compile(self):
        for hook in load_manifest(MANIFEST):
            with self.subTest(hook=hook.hook_id):
                compiled = compile_anchor(hook.anchor)
                self.assertEqual(len(compiled), len(hook.anchor))


class ReservedConstructorTests(unittest.TestCase):
    """`<init>` / `<clinit>` collide with the capture syntax; they must stay literal."""

    def test_init_is_not_lexed_as_a_capture(self):
        self.assertEqual(CAPTURE.findall("invoke-direct {v0}, LFoo;-><init>()V"), [])

    def test_clinit_is_not_lexed_as_a_capture(self):
        self.assertEqual(CAPTURE.findall("invoke-static {}, LFoo;-><clinit>()V"), [])

    def test_init_compiles_to_literal_text(self):
        pattern, names = compile_pattern(
            "invoke-direct {<l:reg>}, <cls:type>-><init>()V"
        )
        self.assertEqual(names, ["l", "cls"])
        self.assertTrue(pattern.match("invoke-direct {v0}, LX/0Ab;-><init>()V"))
        # `<init>` is literal, so a different method name must not match.
        self.assertIsNone(pattern.match("invoke-direct {v0}, LX/0Ab;->onCreate()V"))

    def test_clinit_compiles_to_literal_text(self):
        pattern, names = compile_pattern("invoke-static {}, <cls:type>-><clinit>()V")
        self.assertEqual(names, ["cls"])
        self.assertTrue(pattern.match("invoke-static {}, LX/0Ab;-><clinit>()V"))
        self.assertIsNone(pattern.match("invoke-static {}, LX/0Ab;-><init>()V"))

    def test_init_survives_render_without_a_binding(self):
        # If `<init>` were a capture, render would raise for an unbound name.
        self.assertEqual(
            render("invoke-direct {<l>}, <cls>-><init>()V", {"l": "v0", "cls": "LFoo;"}),
            "invoke-direct {v0}, LFoo;-><init>()V",
        )

    def test_clinit_survives_render_without_a_binding(self):
        self.assertEqual(
            render("invoke-static {}, <cls>-><clinit>()V", {"cls": "LFoo;"}),
            "invoke-static {}, LFoo;-><clinit>()V",
        )

    def test_a_payload_using_init_needs_no_anchor_capture(self):
        hook = make_hook(
            anchor=("new-instance <l:reg>, LFoo;",),
            payload=("    invoke-direct {<l>}, LFoo;-><init>()V",),
            marker="LFoo;-><init>()V",
        )
        self.assertEqual(hook.payload, ("    invoke-direct {<l>}, LFoo;-><init>()V",))

    def test_names_that_merely_start_with_init_are_ordinary_captures(self):
        # The reservation is exact: only `<init>` and `<clinit>` are literal.
        self.assertEqual(CAPTURE.findall("<init_reg:reg>"), [("init_reg", "reg")])
        self.assertEqual(CAPTURE.findall("<clinit_x:reg>"), [("clinit_x", "reg")])
        self.assertEqual(CAPTURE.findall("<initial:type>"), [("initial", "type")])

    def test_init_cannot_be_declared_as_a_capture_name_at_all(self):
        """Was a real bug: the reservation was keyed on the exact text `<init>`.

        `<init:reg>` therefore lexed as a *declaration* while every reference form
        `<init>` stayed literal — a capture nothing could ever read. The anchor
        would bind a register under the name `init`, and the payload's `<init>`
        would render as the four literal characters instead of that register,
        emitting `LFoo;-><init>()V` where the author asked for a register.
        """
        for line in ("nop <init:reg>", "nop <clinit:reg>", "nop <init:type>"):
            with self.subTest(line=line):
                self.assertEqual(CAPTURE.findall(line), [])
        pattern, names = compile_pattern("nop <init:reg>")
        self.assertEqual(names, [])
        # It is plain literal text now, so only the literal line matches.
        self.assertTrue(pattern.match("nop <init:reg>"))
        self.assertIsNone(pattern.match("nop v0"))

    def test_the_reserved_names_constant_matches_what_the_lexer_reserves(self):
        self.assertEqual(set(RESERVED_CAPTURE_NAMES), {"init", "clinit"})
        for name in RESERVED_CAPTURE_NAMES:
            with self.subTest(name=name):
                self.assertEqual(CAPTURE.findall(f"<{name}>"), [])
                self.assertEqual(CAPTURE.findall(f"<{name}:reg>"), [])
        # A name that is not reserved is still lexed in both forms.
        self.assertEqual(CAPTURE.findall("<l>"), [("l", "")])
        self.assertEqual(CAPTURE.findall("<l:reg>"), [("l", "reg")])

    def test_the_real_ui_anchor_uses_init_as_a_literal(self):
        hooks = {hook.hook_id: hook for hook in load_manifest(MANIFEST)}
        hook = hooks["install_settings_long_click"]
        index = next(i for i, entry in enumerate(hook.anchor) if "<init>" in entry)
        pattern, names = compile_anchor(hook.anchor)[index]
        self.assertNotIn("init", names)
        self.assertEqual(names, ["l", "a", "b", "c", "d", "listener"])
        self.assertTrue(
            pattern.match(
                "invoke-direct {v0, v1, v2, v3, v4}, LX/0DnT;-><init>"
                "(ILjava/lang/Object;Ljava/lang/Object;Ljava/lang/Object;)V"
            )
        )
        # `<init>` is literal: the same shape with a different method must not match.
        self.assertIsNone(
            pattern.match(
                "invoke-direct {v0, v1, v2, v3, v4}, LX/0DnT;->A00"
                "(ILjava/lang/Object;Ljava/lang/Object;Ljava/lang/Object;)V"
            )
        )


class RenderTests(unittest.TestCase):
    def test_substitutes_bare_references(self):
        self.assertEqual(
            render(
                "    invoke-static {<app>}, Lcom/dfinstagram/startapp;->setContext"
                "(Landroid/app/Application;)V",
                {"app": "v4"},
            ),
            "    invoke-static {v4}, Lcom/dfinstagram/startapp;->setContext"
            "(Landroid/app/Application;)V",
        )

    def test_substitutes_every_occurrence_of_a_repeated_name(self):
        self.assertEqual(
            render("iput-object <long>, <cfg>, <cfgcls>-><lcf>:LX;", {
                "long": "v5",
                "cfg": "v2",
                "cfgcls": "LX/0Di2;",
                "lcf": "A03",
            }),
            "iput-object v5, v2, LX/0Di2;->A03:LX;",
        )

    def test_also_substitutes_a_declaration_form_reference(self):
        # Payloads use the bare form, but the declaration form is accepted too.
        self.assertEqual(render("<r:reg>", {"r": "v9"}), "v9")

    def test_leaves_lines_without_captures_untouched(self):
        for line in ("", "    # dfinsta_reels_discover_endpoint", "    return-void"):
            with self.subTest(line=line):
                self.assertEqual(render(line, {"r": "v0"}), line)

    def test_raises_on_an_unbound_capture(self):
        with self.assertRaises(ManifestError) as caught:
            render("    invoke-static {<missing>}, LFoo;->f()V", {"r": "v0"})
        self.assertIn("unbound capture", str(caught.exception))
        self.assertIn("<missing>", str(caught.exception))

    def test_extra_bindings_are_harmless(self):
        self.assertEqual(render("<a>", {"a": "v0", "unused": "v1"}), "v0")

    def test_rendering_the_whole_real_payload_of_a_reels_hook(self):
        hooks = {hook.hook_id: hook for hook in load_manifest(MANIFEST)}
        hook = hooks["replace_reels_stream_endpoint"]
        rendered = [render(line, {"r": "v7"}) for line in hook.payload]
        self.assertIn('    const-string v7, "clips/discover/stream/"', rendered)
        self.assertIn("    move-result-object v7", rendered)
        self.assertNotIn("<", "".join(rendered))


class HostFingerprintTests(unittest.TestCase):
    def test_named_requires_a_descriptor(self):
        with self.assertRaises(ManifestError) as caught:
            HostFingerprint("named")
        self.assertIn("descriptor", str(caught.exception))

    def test_named_with_an_empty_descriptor_is_rejected(self):
        with self.assertRaises(ManifestError):
            HostFingerprint("named", descriptor="")

    def test_named_with_a_descriptor_is_accepted(self):
        host = HostFingerprint("named", descriptor="Lcom/instagram/app/InstagramAppShell;")
        self.assertEqual(host.descriptor, "Lcom/instagram/app/InstagramAppShell;")
        self.assertIsNone(host.literal)
        self.assertEqual(host.note, "")

    def test_by_literal_requires_a_literal(self):
        with self.assertRaises(ManifestError) as caught:
            HostFingerprint("by_literal")
        self.assertIn("literal", str(caught.exception))

    def test_by_literal_with_an_empty_literal_is_rejected(self):
        with self.assertRaises(ManifestError):
            HostFingerprint("by_literal", literal="")

    def test_by_literal_with_a_literal_is_accepted(self):
        host = HostFingerprint("by_literal", literal="clips/discover/")
        self.assertEqual(host.literal, "clips/discover/")

    def test_by_agent_needs_neither_descriptor_nor_literal(self):
        host = HostFingerprint("by_agent", note="located by drawable id")
        self.assertIsNone(host.descriptor)
        self.assertIsNone(host.literal)
        self.assertEqual(host.note, "located by drawable id")

    def test_unknown_kind_is_rejected(self):
        for bad in ("by_name", "", "NAMED", "by_literals"):
            with self.subTest(bad=bad):
                with self.assertRaises(ManifestError) as caught:
                    HostFingerprint(bad, descriptor="LFoo;", literal="x")
                self.assertIn("unknown host fingerprint kind", str(caught.exception))

    def test_from_dict_reads_every_field(self):
        host = HostFingerprint.from_dict(
            {"kind": "by_literal", "literal": "clips/discover/", "note": "n"}
        )
        self.assertEqual(
            (host.kind, host.literal, host.note), ("by_literal", "clips/discover/", "n")
        )

    def test_from_dict_defaults_the_note(self):
        host = HostFingerprint.from_dict({"kind": "named", "descriptor": "LFoo;"})
        self.assertEqual(host.note, "")

    def test_from_dict_requires_a_kind(self):
        with self.assertRaises(KeyError):
            HostFingerprint.from_dict({"descriptor": "LFoo;"})

    def test_from_dict_propagates_validation(self):
        with self.assertRaises(ManifestError):
            HostFingerprint.from_dict({"kind": "named"})

    def test_is_frozen(self):
        host = HostFingerprint("by_agent")
        with self.assertRaises(Exception):
            host.kind = "named"  # type: ignore[misc]


class ProbeTests(unittest.TestCase):
    def test_from_dict_reads_every_field(self):
        probe = Probe.from_dict(
            {
                "kind": "logcat_delta",
                "signal": "java.io.IOException: Blocked by DFInsta setting",
                "surface": "feed_tab",
                "requires_two_directional_delta": True,
                "note": "n",
            }
        )
        self.assertEqual(probe.kind, "logcat_delta")
        self.assertEqual(probe.surface, "feed_tab")
        self.assertTrue(probe.requires_two_directional_delta)
        self.assertEqual(probe.note, "n")

    def test_two_directional_delta_defaults_to_required(self):
        probe = Probe.from_dict(
            {"kind": "logcat_delta", "signal": "Blocked by DFInsta", "surface": "feed_tab"}
        )
        self.assertTrue(probe.requires_two_directional_delta)
        self.assertEqual(probe.note, "")

    def test_two_directional_delta_can_be_waived_with_a_note(self):
        probe = Probe.from_dict(
            {
                "kind": "startup_no_fatal",
                "signal": "VerifyError",
                "surface": "app_launch",
                "requires_two_directional_delta": False,
                "note": "not toggleable; launching without a fatal is the whole proof",
            }
        )
        self.assertFalse(probe.requires_two_directional_delta)
        self.assertIn("not toggleable", probe.note)

    def test_every_declared_probe_kind_is_accepted(self):
        for kind in PROBE_KINDS:
            with self.subTest(kind=kind):
                self.assertEqual(Probe(kind, "s", "f").kind, kind)

    def test_probe_kinds_are_exactly_the_three_the_verify_stage_runs(self):
        self.assertEqual(
            set(PROBE_KINDS), {"logcat_delta", "ui_dialog", "startup_no_fatal"}
        )

    def test_an_unknown_probe_kind_is_rejected(self):
        """Was a real gap: unlike `HostFingerprint`, `Probe` had no `__post_init__`.

        Any string at all was accepted as a probe kind, so a typo like
        `logcat_deltas` reached the Verify stage as an unrunnable probe — a hook
        that looked verified in the manifest but had no executable check behind it.
        """
        for bad in ("not_a_real_probe", "", "LOGCAT_DELTA", "logcat", "ui-dialog"):
            with self.subTest(bad=bad):
                with self.assertRaises(ManifestError) as caught:
                    Probe(bad, "s", "f")
                self.assertIn("unknown probe kind", str(caught.exception))

    def test_an_empty_signal_is_rejected(self):
        for bad in ("", "   ", "\n"):
            with self.subTest(bad=bad):
                with self.assertRaises(ManifestError) as caught:
                    Probe("logcat_delta", bad, "feed_tab")
                self.assertIn("non-empty signal and surface", str(caught.exception))

    def test_an_empty_surface_is_rejected(self):
        for bad in ("", "   ", "\t"):
            with self.subTest(bad=bad):
                with self.assertRaises(ManifestError) as caught:
                    Probe("logcat_delta", "some signal", bad)
                self.assertIn("non-empty signal and surface", str(caught.exception))

    def test_waiving_the_two_directional_delta_without_a_note_is_rejected(self):
        """A silent waiver is how an inert hook passes verification.

        The manifest shipped exactly one of these: the actionbar settings hook
        waived the delta with no explanation, and that is the hook that applied
        cleanly on 430 while being completely inert at runtime.
        """
        for note in ("", "   "):
            with self.subTest(note=repr(note)):
                with self.assertRaises(ManifestError) as caught:
                    Probe(
                        "ui_dialog",
                        "Distraction-free settings",
                        "profile_options_long_press",
                        False,
                        note,
                    )
                message = str(caught.exception)
                self.assertIn("requires_two_directional_delta", message)
                self.assertIn("note", message)

    def test_from_dict_propagates_the_missing_waiver_note(self):
        with self.assertRaises(ManifestError):
            Probe.from_dict(
                {
                    "kind": "ui_dialog",
                    "signal": "Distraction-free settings",
                    "surface": "profile_options_long_press",
                    "requires_two_directional_delta": False,
                }
            )

    def test_a_required_delta_needs_no_note(self):
        # The note is only compulsory for the waiver; the delta itself is the proof.
        probe = Probe("logcat_delta", "Blocked by DFInsta setting", "feed_tab")
        self.assertEqual(probe.note, "")

    def test_from_dict_requires_kind_signal_and_surface(self):
        complete = {"kind": "logcat_delta", "signal": "s", "surface": "f"}
        for missing in ("kind", "signal", "surface"):
            with self.subTest(missing=missing):
                data = {key: value for key, value in complete.items() if key != missing}
                with self.assertRaises(KeyError):
                    Probe.from_dict(data)

    def test_is_frozen(self):
        probe = Probe("logcat_delta", "s", "f")
        with self.assertRaises(Exception):
            probe.signal = "other"  # type: ignore[misc]

    def test_only_the_non_toggleable_real_hooks_waive_the_delta(self):
        # Waiving the two-directional delta is the exception and is reserved for
        # hooks that have no toggle at all.
        waived = {
            hook.hook_id
            for hook in load_manifest(MANIFEST)
            if hook.probe and not hook.probe.requires_two_directional_delta
        }
        self.assertEqual(
            waived,
            {
                "set_app_context",
                "install_settings_long_click",
                "install_settings_long_click_actionbar",
            },
        )


class HookValidationTests(unittest.TestCase):
    def test_a_minimal_hook_is_accepted(self):
        hook = make_hook()
        self.assertEqual(hook.hook_id, "probe_hook")
        self.assertEqual(hook.mode, "insert_after")
        self.assertEqual(hook.expected_anchor_count, 1)
        self.assertEqual(hook.constraints, ())
        self.assertIsNone(hook.probe)
        self.assertEqual(hook.status, "active")

    def test_unknown_tier_is_rejected(self):
        for bad in ("solid", "", "ROBUST", "brittle"):
            with self.subTest(bad=bad):
                with self.assertRaises(ManifestError) as caught:
                    make_hook(tier=bad)
                self.assertIn("unknown tier", str(caught.exception))
                self.assertIn("probe_hook", str(caught.exception))

    def test_every_declared_tier_is_accepted(self):
        for tier in ("robust", "fragile", "ui"):
            with self.subTest(tier=tier):
                self.assertEqual(make_hook(tier=tier).tier, tier)

    def test_unknown_mode_is_rejected(self):
        for bad in ("insert_before", "delete", "", "REPLACE"):
            with self.subTest(bad=bad):
                with self.assertRaises(ManifestError) as caught:
                    make_hook(mode=bad)
                self.assertIn("unknown mode", str(caught.exception))

    def test_both_supported_modes_are_accepted(self):
        for mode in ("insert_after", "replace"):
            with self.subTest(mode=mode):
                self.assertEqual(make_hook(mode=mode).mode, mode)

    def test_insert_before_is_rejected_even_though_the_applier_supports_it(self):
        # The applier understands insert_before; the manifest deliberately does not
        # offer it, so an operation can never be emitted in that mode.
        with self.assertRaises(ManifestError):
            make_hook(mode="insert_before")

    def test_empty_hosts_are_rejected(self):
        with self.assertRaises(ManifestError) as caught:
            make_hook(hosts=())
        self.assertIn("at least one host fingerprint", str(caught.exception))

    def test_empty_anchor_is_rejected(self):
        with self.assertRaises(ManifestError) as caught:
            make_hook(anchor=())
        self.assertIn("needs an anchor", str(caught.exception))

    def test_empty_payload_is_rejected(self):
        # A hook with nothing to insert is an operation the applier would happily
        # "apply" while changing nothing, and its marker could never appear.
        with self.assertRaises(ManifestError) as caught:
            make_hook(payload=())
        self.assertIn("needs a payload", str(caught.exception))
        self.assertIn("probe_hook", str(caught.exception))

    def test_an_empty_marker_is_rejected_at_construction(self):
        """Was a real bug: `marker` was never validated, and an empty one is fatal.

        `str.count("")` returns len+1, so an empty marker made the already-applied
        guard fire against every class in the decode: the hook could never resolve
        anywhere, and the reason it gave blamed a partial patch that did not exist.
        """
        with self.assertRaises(ManifestError) as caught:
            make_hook(marker="")
        self.assertIn("marker must be a non-empty string", str(caught.exception))
        self.assertIn("probe_hook", str(caught.exception))

    def test_a_whitespace_only_marker_is_rejected(self):
        for bad in ("   ", "\n", "\t"):
            with self.subTest(bad=bad):
                with self.assertRaises(ManifestError) as caught:
                    make_hook(marker=bad)
                self.assertIn("marker must be a non-empty string", str(caught.exception))

    def test_a_marker_that_is_absent_from_its_own_payload_is_rejected(self):
        # If the payload never writes the marker, the applier's idempotence check
        # can never see the hook land: every re-run would re-apply it.
        with self.assertRaises(ManifestError) as caught:
            make_hook(marker="LH;->g(Ljava/lang/String;)V")
        message = str(caught.exception)
        self.assertIn("appears 0 time(s) in its own payload", message)
        self.assertIn("LH;->g(Ljava/lang/String;)V", message)

    def test_expected_marker_count_below_one_is_rejected(self):
        for bad in (0, -1):
            with self.subTest(bad=bad):
                with self.assertRaises(ManifestError) as caught:
                    make_hook(expected_marker_count=bad)
                self.assertIn("expected_marker_count must be >= 1", str(caught.exception))

    def test_an_expected_anchor_count_other_than_one_is_rejected(self):
        """Was a real bug: `expected_anchor_count > 1` emitted an unusable operation.

        Only `hits[0]`'s bindings rendered the anchor, but the operation still
        declared N, so the applier's literal anchor search found 1 of N whenever
        the sites bound different registers and refused the patch. Multi-site hooks
        need one manifest entry per site; the count is not a substitute.
        """
        for bad in (2, 3):
            with self.subTest(bad=bad):
                with self.assertRaises(ManifestError) as caught:
                    make_hook(expected_anchor_count=bad)
                message = str(caught.exception)
                self.assertIn("expected_anchor_count must be 1", message)
                self.assertIn("one entry per site", message)

    def test_an_expected_anchor_count_below_one_is_also_rejected(self):
        for bad in (0, -1):
            with self.subTest(bad=bad):
                with self.assertRaises(ManifestError) as caught:
                    make_hook(expected_anchor_count=bad)
                self.assertIn("expected_anchor_count must be 1", str(caught.exception))

    def test_payload_referencing_an_uncaptured_name_is_rejected(self):
        with self.assertRaises(ManifestError) as caught:
            make_hook(
                anchor=('const-string <r:reg>, "needle"',),
                payload=("    invoke-static {<other>}, LH;->f(Ljava/lang/String;)V",),
            )
        message = str(caught.exception)
        self.assertIn("<other>", message)
        self.assertIn("which no anchor line captures", message)
        self.assertIn("probe_hook", message)

    def test_payload_capture_declared_on_a_later_anchor_line_is_accepted(self):
        hook = make_hook(
            anchor=(
                "new-instance <l:reg>, LFoo;",
                "invoke-static {<l>, <view:reg>}, LBar;->attach(LFoo;LView;)V",
            ),
            payload=("    invoke-virtual {<view>, <l>}, LView;->set(LFoo;)V",),
            marker="LView;->set(LFoo;)V",
        )
        self.assertEqual(len(hook.anchor), 2)

    def test_payload_validation_ignores_reserved_constructor_names(self):
        # `<init>` in the payload must not be mistaken for an uncaptured reference.
        hook = make_hook(
            anchor=("new-instance <l:reg>, LFoo;",),
            payload=("    invoke-direct {<l>}, LBar;-><init>()V",),
            marker="LBar;-><init>()V",
        )
        self.assertIn("<init>", hook.payload[0])

    def test_an_unusable_anchor_pattern_fails_at_construction_not_resolve_time(self):
        with self.assertRaises(ManifestError) as caught:
            make_hook(
                anchor=("move-result-object <r>",),
                payload=("    invoke-static {}, LH;->f()V",),
                marker="LH;->f()V",
            )
        self.assertIn("must declare a kind", str(caught.exception))

    def test_from_dict_applies_every_default(self):
        hook = Hook.from_dict(
            {
                "hook_id": "h",
                "intent": "i",
                "tier": "robust",
                "strategy": "s",
                "hosts": [{"kind": "by_agent"}],
                "anchor": ["nop"],
                "payload": ["    invoke-static {}, LH;->f()V"],
                "marker": "LH;->f()V",
                "expected_marker_count": 1,
            }
        )
        self.assertEqual(hook.semantic_deps, ())
        self.assertEqual(hook.mode, "insert_after")
        self.assertEqual(hook.expected_anchor_count, 1)
        self.assertEqual(hook.constraints, ())
        self.assertIsNone(hook.probe)
        self.assertEqual(hook.status, "active")

    def test_from_dict_coerces_sequences_to_tuples(self):
        hook = Hook.from_dict(
            {
                "hook_id": "h",
                "intent": "i",
                "tier": "ui",
                "strategy": "s",
                "semantic_deps": ["a"],
                "hosts": [{"kind": "by_agent"}],
                "anchor": ["nop"],
                "payload": [
                    "    invoke-static {}, LH;->f()V",
                    "    invoke-static {}, LH;->f()V",
                ],
                "marker": "LH;->f()V",
                "expected_marker_count": "2",
                "expected_anchor_count": "1",
                "constraints": ["c"],
                "probe": {"kind": "ui_dialog", "signal": "s", "surface": "f"},
                "status": "retired",
            }
        )
        self.assertIsInstance(hook.semantic_deps, tuple)
        self.assertIsInstance(hook.hosts, tuple)
        self.assertIsInstance(hook.anchor, tuple)
        self.assertIsInstance(hook.payload, tuple)
        self.assertIsInstance(hook.constraints, tuple)
        # The string forms are coerced, not stored: `"2" == 2` is False.
        self.assertEqual(hook.expected_marker_count, 2)
        self.assertEqual(hook.expected_anchor_count, 1)
        self.assertIsInstance(hook.probe, Probe)
        self.assertEqual(hook.status, "retired")

    def test_from_dict_propagates_probe_validation(self):
        with self.assertRaises(ManifestError) as caught:
            Hook.from_dict(
                {
                    "hook_id": "h",
                    "intent": "i",
                    "tier": "robust",
                    "strategy": "s",
                    "hosts": [{"kind": "by_agent"}],
                    "anchor": ["nop"],
                    "payload": ["    invoke-static {}, LH;->f()V"],
                    "marker": "LH;->f()V",
                    "expected_marker_count": 1,
                    "probe": {"kind": "smoke_test", "signal": "s", "surface": "f"},
                }
            )
        self.assertIn("unknown probe kind", str(caught.exception))

    def test_from_dict_requires_the_core_fields(self):
        complete = {
            "hook_id": "h",
            "intent": "i",
            "tier": "robust",
            "strategy": "s",
            "hosts": [{"kind": "by_agent"}],
            "anchor": ["nop"],
            "payload": ["    invoke-static {}, LH;->f()V"],
            "marker": "LH;->f()V",
            "expected_marker_count": 1,
        }
        for missing in complete:
            with self.subTest(missing=missing):
                data = {key: value for key, value in complete.items() if key != missing}
                with self.assertRaises(KeyError):
                    Hook.from_dict(data)

    def test_is_frozen(self):
        hook = make_hook()
        with self.assertRaises(Exception):
            hook.tier = "ui"  # type: ignore[misc]


class SignificantTests(unittest.TestCase):
    """The resolver must see the same lines the applier sees, or anchors drift."""

    def test_drops_blank_line_directives_and_comments(self):
        source = [
            "",
            "    .line 42",
            "    # a comment",
            "    const-string v0, \"x\"",
            "   ",
        ]
        self.assertEqual(significant(source), [(3, 'const-string v0, "x"')])

    def test_reports_original_indices(self):
        source = ["", "nop", "", "return-void"]
        self.assertEqual(significant(source), [(1, "nop"), (3, "return-void")])

    def test_strips_indentation(self):
        self.assertEqual(significant(["        nop"]), [(0, "nop")])

    def test_matches_the_reconstruction_applier_view(self):
        sys.path.insert(0, str(RECONSTRUCTION_TOOLS))
        try:
            from apply_anchored_patches import significant_lines
        except ImportError:  # pragma: no cover - tool tree is present in-repo
            self.skipTest("reconstruction tools not importable")
        finally:
            sys.path.remove(str(RECONSTRUCTION_TOOLS))
        source = [
            ".method public a()V",
            "    .locals 2",
            "",
            "    .line 7",
            "    # dfinsta marker",
            '    const-string v1, "clips/discover/"',
            "    .end method",
        ]
        self.assertEqual(significant(source), significant_lines(source))

    def test_a_trailing_annotation_stays_in_the_significant_view(self):
        """`significant` must NOT strip what `strip_comment` strips.

        Three other modules reimplement this view — `apply._significant`,
        `verifier._significant`, `apply_anchored_patches.significant_lines` — and
        each finds a concrete anchor by comparing against it literally. Narrowing
        the view here alone would make an anchor resolve and then match nothing at
        apply time, which is worse than the bug it would be fixing.
        """
        source = ["    const v0, 0x7f134a34    # 1.957818E38f"]
        self.assertEqual(
            significant(source), [(0, "const v0, 0x7f134a34    # 1.957818E38f")]
        )


class StripCommentTests(unittest.TestCase):
    """A baksmali annotation must not decide whether an anchor matches.

    Every sample here is a real line shape counted in the 439 or 430 decode, not
    an invented one: 66,169 lines of 439 and 62,135 of 430 end in a comment on an
    otherwise-code line, and 1,152 lines of 439 carry a `#` inside a string.
    """

    def test_a_float_annotation_on_a_resource_id_is_removed(self):
        # The exact line that made `install_settings_long_click_actionbar` fail.
        self.assertEqual(
            strip_comment("const v0, 0x7f134a34    # 1.957818E38f"),
            "const v0, 0x7f134a34",
        )

    def test_every_measured_annotation_shape_is_removed(self):
        for line, wanted in (
            ("const/high16 v0, 0x40000000    # 2.0f", "const/high16 v0, 0x40000000"),
            (
                "const-wide v4, 0x412e848000000000L    # 1000000.0",
                "const-wide v4, 0x412e848000000000L",
            ),
            (
                "const-wide/high16 v3, 0x3fe8000000000000L    # 0.75",
                "const-wide/high16 v3, 0x3fe8000000000000L",
            ),
            ("const/16 v1, 0x593    # 2.0E-42f", "const/16 v1, 0x593"),
            # array-data payload entries carry them too, with no opcode at all
            ("0x3f800000    # 1.0f", "0x3f800000"),
            ("-0x40800000    # -1.0f", "-0x40800000"),
            (".param p5    # Ljava/lang/String;", ".param p5"),
        ):
            with self.subTest(line=line):
                self.assertEqual(strip_comment(line), wanted)

    def test_a_line_with_no_comment_is_returned_unchanged(self):
        line = "iput-object v13, v1, LX/07uJ;->A0H:Landroid/view/View$OnLongClickListener;"
        self.assertIs(strip_comment(line), line)

    def test_a_hash_inside_a_string_literal_is_not_a_comment(self):
        """Splitting on the first `#` would truncate the literal into a lie.

        These are lines this project's own decodes contain, so it is not a
        hypothetical: `const-string v0, "#"` would become `const-string v0, "`,
        which is not smali and would never match anything — but the two with a
        space before the `#` are worse, because the truncated prefix is still a
        well-formed token and could match an anchor the real line does not.
        """
        for line in (
            'const-string v0, "#"',
            'const-string v0, "a#b"',
            'const-string v0, "#ffffff"',
            'const-string v0, "Using more than the expected # of framebuffers"',
            'const-string v0, "More than one \'any-setter\' specified (parameter #%d)"',
            'const-string v1, "http://www.w3.org/ns/ttml#parameter"',
        ):
            with self.subTest(line=line):
                self.assertIs(strip_comment(line), line)

    def test_a_comment_after_a_string_literal_is_still_a_comment(self):
        self.assertEqual(
            strip_comment('const-string v0, "a#b"    # trailing'),
            'const-string v0, "a#b"',
        )

    def test_an_escaped_quote_does_not_end_the_string(self):
        # `"You can\'t ... RequestBuilder#error(...)"` is a real 439 line: the
        # escape run has to be tracked or the scan leaves the string early and
        # reads the `#` as a comment.
        line = 'const-string v0, "he said \\" then # not a comment"'
        self.assertIs(strip_comment(line), line)

    def test_a_doubled_backslash_does_not_swallow_the_closing_quote(self):
        self.assertEqual(
            strip_comment('const-string v0, "ends with a backslash\\\\"  # gone'),
            'const-string v0, "ends with a backslash\\\\"',
        )

    def test_a_line_that_is_only_a_comment_becomes_empty(self):
        # It never reaches `strip_comment` in practice — `significant` drops it
        # first — but an empty result is the honest answer, not the whole line.
        self.assertEqual(strip_comment("# dfinsta_settings_long_click_actionbar"), "")

    def test_an_unterminated_quote_strips_nothing(self):
        # Not valid smali. Refusing to cut is the conservative failure: the line
        # simply does not match an anchor, rather than matching a truncated one.
        line = 'const-string v0, "unterminated # still inside'
        self.assertIs(strip_comment(line), line)


CLEAN_APP_SHELL = """.class public Lcom/instagram/app/InstagramAppShell;
.super Landroid/app/Application;

.method public onCreate()V
    .locals 0

    .line 12
    invoke-super {p0}, Landroid/app/Application;->onCreate()V

    return-void
.end method
"""


class ResolveInSourceTests(unittest.TestCase):
    def setUp(self):
        self.hooks = {hook.hook_id: hook for hook in load_manifest(MANIFEST)}

    def test_clean_single_match_resolves_with_bindings_and_rendered_lines(self):
        hook = self.hooks["set_app_context"]
        result = resolve_in_source(
            hook, "Lcom/instagram/app/InstagramAppShell;", CLEAN_APP_SHELL
        )
        self.assertTrue(result.resolved)
        self.assertEqual(result.hook_id, "set_app_context")
        self.assertEqual(result.descriptor, "Lcom/instagram/app/InstagramAppShell;")
        self.assertEqual(result.bindings, {"app": "p0"})
        self.assertEqual(result.occurrences, 1)
        self.assertEqual(result.reason, "")
        self.assertEqual(
            result.anchor,
            ["invoke-super {p0}, Landroid/app/Application;->onCreate()V"],
        )
        # The payload now leads with the hook's runtime-identity call: one
        # no-argument static named after the hook, whose whole point is that it
        # needs no register and so renders identically whatever `app` binds to.
        self.assertEqual(
            result.payload,
            [
                "",
                "    invoke-static {}, Lcom/dfinstagram/probe;->h_set_app_context()V",
                "",
                "    invoke-static {p0}, Lcom/dfinstagram/startapp;->setContext"
                "(Landroid/app/Application;)V",
            ],
        )

    def test_the_same_hook_resolves_against_a_different_register(self):
        # This is the whole point: 430 used v0, 439 used v4, one manifest entry.
        source = CLEAN_APP_SHELL.replace("{p0}", "{v4}")
        result = resolve_in_source(
            self.hooks["set_app_context"], "Lcom/instagram/app/InstagramAppShell;", source
        )
        self.assertTrue(result.resolved)
        self.assertEqual(result.bindings, {"app": "v4"})
        # payload[1] is the register-free probe call; the register-bearing
        # setContext call is now at payload[3].
        self.assertIn("invoke-static {v4}", result.payload[3])

    def test_a_multi_line_anchor_binds_across_lines(self):
        hook = self.hooks["tigon_url_block"]
        source = (
            ".class public Lcom/instagram/api/tigon/TigonServiceLayer;\n"
            "\n"
            ".method public A00()V\n"
            "    .locals 3\n"
            "\n"
            "    :try_start_0\n"
            "    iget-object v2, p1, LX/03AS;->A0B:Ljava/net/URI;\n"
            "\n"
            "    return-void\n"
            ".end method\n"
        )
        result = resolve_in_source(
            hook, "Lcom/instagram/api/tigon/TigonServiceLayer;", source
        )
        self.assertTrue(result.resolved)
        self.assertEqual(
            result.bindings,
            {"uri": "v2", "req": "p1", "reqcls": "LX/03AS;", "urifield": "A0B"},
        )
        self.assertEqual(result.anchor[0], ":try_start_0")
        # payload[1] is the register-free probe call; throwIfBlocked, which takes
        # the captured uri register, is now at payload[3].
        self.assertIn("invoke-static {v2}", result.payload[3])

    def test_anchor_lines_must_be_consecutive_in_the_significant_view(self):
        hook = self.hooks["tigon_url_block"]
        source = (
            "    :try_start_0\n"
            "    nop\n"
            "    iget-object v2, p1, LX/03AS;->A0B:Ljava/net/URI;\n"
        )
        result = resolve_in_source(hook, "LFoo;", source)
        self.assertFalse(result.resolved)

    def test_comments_and_line_directives_do_not_break_adjacency(self):
        hook = self.hooks["tigon_url_block"]
        source = (
            "    :try_start_0\n"
            "\n"
            "    .line 91\n"
            "    # noise\n"
            "    iget-object v2, p1, LX/03AS;->A0B:Ljava/net/URI;\n"
        )
        result = resolve_in_source(hook, "LFoo;", source)
        self.assertTrue(result.resolved)

    def test_zero_matches_reports_a_reason(self):
        result = resolve_in_source(
            self.hooks["set_app_context"],
            "LFoo;",
            ".class LFoo;\n    return-void\n",
        )
        self.assertFalse(result.resolved)
        self.assertFalse(result.already_applied)
        self.assertEqual(result.reason, "anchor pattern did not match")
        self.assertEqual(result.occurrences, 0)
        self.assertEqual(result.bindings, {})
        self.assertEqual(result.anchor, [])
        self.assertEqual(result.payload, [])
        # descriptor is now recorded on EVERY failure path, not only the marker
        # ones — it was inconsistent before and looked accidental.
        self.assertEqual(result.descriptor, "LFoo;")

    def test_two_matches_against_an_expected_count_of_one_is_ambiguous(self):
        hook = self.hooks["replace_reels_discover_endpoint"]
        self.assertEqual(hook.expected_anchor_count, 1)
        source = (
            ".class LX/04tC;\n"
            '    const-string v1, "clips/discover/"\n'
            "    nop\n"
            '    const-string v3, "clips/discover/"\n'
        )
        result = resolve_in_source(hook, "LX/04tC;", source)
        self.assertFalse(result.resolved)
        self.assertFalse(result.already_applied)
        self.assertEqual(result.occurrences, 2)
        self.assertIn("ambiguous", result.reason)
        self.assertIn("matched 2 times", result.reason)
        self.assertIn("expected 1", result.reason)
        # Nothing is rendered from an ambiguous match.
        self.assertEqual(result.anchor, [])
        self.assertEqual(result.payload, [])
        self.assertEqual(result.bindings, {})

    def test_a_preexisting_marker_reports_already_applied(self):
        hook = self.hooks["set_app_context"]
        source = CLEAN_APP_SHELL.replace(
            "    return-void",
            "    invoke-static {p0}, Lcom/dfinstagram/startapp;->setContext"
            "(Landroid/app/Application;)V\n\n    return-void",
        )
        self.assertEqual(source.count(hook.marker), hook.expected_marker_count)
        result = resolve_in_source(
            hook, "Lcom/instagram/app/InstagramAppShell;", source
        )
        self.assertFalse(result.resolved)
        self.assertTrue(result.already_applied)
        self.assertIn("already applied", result.reason)
        self.assertEqual(result.descriptor, "Lcom/instagram/app/InstagramAppShell;")
        # Nothing is rendered: there is no work left to emit.
        self.assertEqual(result.anchor, [])
        self.assertEqual(result.payload, [])
        self.assertEqual(result.bindings, {})

    def test_a_comment_marker_is_seen_even_though_it_is_not_significant(self):
        # `replace`-mode markers are comments, which `significant` filters out; the
        # marker check must therefore run against the raw text, not the filtered view.
        hook = self.hooks["replace_reels_stream_endpoint"]
        source = (
            ".class LX/04tC;\n"
            "    # dfinsta_reels_stream_endpoint\n"
            '    const-string v1, "clips/discover/stream/"\n'
        )
        self.assertEqual(
            significant(source.splitlines()),
            [(0, ".class LX/04tC;"), (2, 'const-string v1, "clips/discover/stream/"')],
        )
        result = resolve_in_source(hook, "LX/04tC;", source)
        self.assertFalse(result.resolved)
        self.assertTrue(result.already_applied)
        self.assertIn("already applied", result.reason)

    def test_a_fully_applied_class_is_reported_as_already_applied(self):
        """Was a real bug: the resolver never compared against expected_marker_count.

        Any non-zero count was a failure whose reason blamed a partial patch, so a
        re-run over an already-patched decode raised a false alarm on every hook.
        The applier has always distinguished the two; the resolver now matches it,
        and `already_applied` says so in a field rather than only in prose.
        """
        hook = self.hooks["replace_reels_discover_endpoint"]
        clean = '.class LX/04tC;\n    const-string v1, "clips/discover/"\n'
        applied = clean.replace(
            '    const-string v1, "clips/discover/"',
            "\n".join(render(line, {"r": "v1"}) for line in hook.payload),
        )
        self.assertEqual(applied.count(hook.marker), hook.expected_marker_count)
        result = resolve_in_source(hook, "LX/04tC;", applied)
        self.assertFalse(result.resolved)
        self.assertTrue(result.already_applied)
        self.assertIn("already applied", result.reason)
        # `replace` mode re-emits the anchor line, so the applied text still holds a
        # line that demonstrably resolves. That pins the ordering too: the marker is
        # checked first and wins over an anchor match that would otherwise succeed.
        self.assertTrue(resolve_in_source(hook, "LX/04tC;", clean).resolved)
        self.assertIn('    const-string v1, "clips/discover/"', applied)
        # Already applied is still not an operation: there is nothing to emit.
        with self.assertRaises(ManifestError) as caught:
            result.as_operation(hook)
        self.assertIn("already applied", str(caught.exception))

    def test_a_partially_applied_class_names_the_marker_and_both_counts(self):
        # An interrupted or half-reverted patch leaves one of two occurrences
        # behind. A synthetic two-marker hook rather than a manifest one, because
        # every shipped hook now uses a single distinct marker — a manifest hook
        # would tie this test to a count that is free to change.
        hook = make_hook(
            payload=(
                "    invoke-static {<r>}, LH;->f(Ljava/lang/String;)V",
                "    invoke-static {<r>}, LH;->f(Ljava/lang/String;)V",
            ),
            expected_marker_count=2,
        )
        source = ".class LX/0DnT;\n    invoke-static {v0}, LH;->f(Ljava/lang/String;)V\n"
        result = resolve_in_source(hook, "LX/0DnT;", source)
        self.assertFalse(result.resolved)
        self.assertFalse(result.already_applied)
        self.assertIn(hook.marker, result.reason)
        self.assertIn("1/2", result.reason)
        self.assertIn("partially applied", result.reason)
        self.assertEqual(result.descriptor, "LX/0DnT;")
        self.assertEqual(result.anchor, [])
        self.assertEqual(result.payload, [])
        self.assertEqual(result.bindings, {})
        self.assertEqual(result.occurrences, 0)

    def test_a_partial_patch_is_diagnosed_as_partial_not_as_a_missing_anchor(self):
        """This is why the marker check runs before anchor matching.

        A half-applied hook usually leaves a mangled anchor behind, so checking the
        anchor first would report "anchor pattern did not match" and send whoever
        reads it hunting for a version drift that never happened.
        """
        hook = make_hook(
            payload=(
                "    invoke-static {<r>}, LH;->f(Ljava/lang/String;)V",
                "    invoke-static {<r>}, LH;->f(Ljava/lang/String;)V",
            ),
            expected_marker_count=2,
        )
        mangled = ".class LX/0DnT;\n    nop\n"
        marker_line = "    invoke-static {v0}, LH;->f(Ljava/lang/String;)V\n"
        # With no marker at all, this class reports the anchor failure...
        self.assertEqual(
            resolve_in_source(hook, "LX/0DnT;", mangled).reason,
            "anchor pattern did not match",
        )
        # ...but one marker present says a patch already started here, which is the
        # more useful diagnosis even though the anchor is just as unmatchable.
        result = resolve_in_source(hook, "LX/0DnT;", mangled + marker_line)
        self.assertIn("partially applied", result.reason)
        self.assertNotIn("anchor", result.reason)

    def test_a_marker_count_above_the_expected_count_is_reported_with_both_counts(self):
        # An over-count is not a partial patch, but it is just as wrong, and the
        # reason carries both numbers so the reader can tell which case it is.
        # (The applier words this case the same way, so the two stay comparable.)
        hook = self.hooks["install_settings_long_click"]
        self.assertEqual(hook.expected_marker_count, 1)
        source = ".class LX/0DnT;\n" + f"    {hook.marker}\n" * 3
        result = resolve_in_source(hook, "LX/0DnT;", source)
        self.assertFalse(result.resolved)
        self.assertFalse(result.already_applied)
        self.assertIn("3/1", result.reason)

    def test_already_applied_is_true_only_for_an_exact_marker_count(self):
        hook = make_hook(
            payload=(
                "    invoke-static {<r>}, LH;->f(Ljava/lang/String;)V",
                "    invoke-static {<r>}, LH;->f(Ljava/lang/String;)V",
            ),
            expected_marker_count=2,
        )
        states = (
            (0, True, False),  # clean class: resolve normally
            (1, False, False),  # partial: one of the two markers landed
            (2, False, True),  # the normal re-run state
            (3, False, False),  # over-applied
        )
        for count, resolved, already in states:
            with self.subTest(marker_count=count):
                source = (
                    ".class LFoo;\n"
                    '    const-string v0, "needle"\n'
                    + f"    invoke-static {{v0}}, {hook.marker}\n" * count
                )
                self.assertEqual(source.count(hook.marker), count)
                result = resolve_in_source(hook, "LFoo;", source)
                self.assertEqual(result.resolved, resolved)
                self.assertEqual(result.already_applied, already)

    def test_a_resolution_is_not_already_applied_by_default(self):
        self.assertFalse(Resolution("x", False).already_applied)
        self.assertFalse(Resolution("x", True).already_applied)

    def test_cross_line_binding_must_agree(self):
        hook = make_hook(
            anchor=(
                "new-instance <l:reg>, <cls:type>",
                "invoke-direct {<l>}, <cls>-><init>()V",
            ),
            payload=("    invoke-virtual {<l>}, LBar;->go()V",),
            marker="LBar;->go()V",
        )
        agreeing = (
            ".class LFoo;\n"
            "    new-instance v0, LX/0Ab;\n"
            "    invoke-direct {v0}, LX/0Ab;-><init>()V\n"
        )
        result = resolve_in_source(hook, "LFoo;", agreeing)
        self.assertTrue(result.resolved)
        self.assertEqual(result.bindings, {"l": "v0", "cls": "LX/0Ab;"})

    def test_cross_line_binding_rejects_a_differing_register(self):
        hook = make_hook(
            anchor=(
                "new-instance <l:reg>, <cls:type>",
                "invoke-direct {<l>}, <cls>-><init>()V",
            ),
            payload=("    invoke-virtual {<l>}, LBar;->go()V",),
            marker="LBar;->go()V",
        )
        differing = (
            ".class LFoo;\n"
            "    new-instance v0, LX/0Ab;\n"
            "    invoke-direct {v1}, LX/0Ab;-><init>()V\n"
        )
        result = resolve_in_source(hook, "LFoo;", differing)
        self.assertFalse(result.resolved)
        self.assertEqual(result.reason, "anchor pattern did not match")

    def test_cross_line_binding_rejects_a_differing_type(self):
        hook = make_hook(
            anchor=(
                "new-instance <l:reg>, <cls:type>",
                "invoke-direct {<l>}, <cls>-><init>()V",
            ),
            payload=("    invoke-virtual {<l>}, LBar;->go()V",),
            marker="LBar;->go()V",
        )
        differing = (
            ".class LFoo;\n"
            "    new-instance v0, LX/0Ab;\n"
            "    invoke-direct {v0}, LX/0Zz;-><init>()V\n"
        )
        result = resolve_in_source(hook, "LFoo;", differing)
        self.assertFalse(result.resolved)

    def test_the_register_writeback_constraint_is_enforced_by_the_repeated_capture(self):
        # replace_reels_* must write the result back to the SAME register. The
        # anchor only captures the register once, so the guarantee comes from the
        # payload rendering both uses from one binding, never from two matches.
        hook = self.hooks["replace_reels_homecoming_endpoint"]
        source = '.class LX/04tC;\n    const-string v6, "clips/homecoming/"\n'
        result = resolve_in_source(hook, "LX/04tC;", source)
        self.assertTrue(result.resolved)
        self.assertIn('    const-string v6, "clips/homecoming/"', result.payload)
        self.assertIn("    move-result-object v6", result.payload)
        # The probe call and its trailing blank now lead the replace payload, so
        # the register-bearing replaceReelsEndpoint call has shifted from
        # payload[3] to payload[5]; the repeated <r> still binds every use to v6.
        self.assertIn("    invoke-static {v6}, Lcom/dfinstagram/hooks;", result.payload[5])

    def test_an_anchor_wider_than_the_source_cannot_match(self):
        hook = make_hook(
            anchor=("nop", "nop", "nop"),
            payload=("    return-void",),
            marker="return-void",
        )
        result = resolve_in_source(hook, "LFoo;", "nop\nnop\n")
        self.assertFalse(result.resolved)
        self.assertEqual(result.reason, "anchor pattern did not match")

    def test_an_empty_source_cannot_match(self):
        result = resolve_in_source(make_hook(), "LFoo;", "")
        self.assertFalse(result.resolved)
        self.assertEqual(result.reason, "anchor pattern did not match")

    def test_resolution_does_not_mutate_the_hook(self):
        hook = self.hooks["set_app_context"]
        before = (hook.anchor, hook.payload)
        resolve_in_source(hook, "LFoo;", CLEAN_APP_SHELL)
        self.assertEqual((hook.anchor, hook.payload), before)
        # payload[1] is the register-free probe call; the <app> capture the
        # resolver must not have consumed lives on in payload[3].
        self.assertIn("<app>", hook.payload[3])


#: The five lines `install_settings_long_click_actionbar` matches in 439's
#: `LX/0Di2;`, copied verbatim from `smali_classes6/X/0Di2.smali` including the
#: annotation baksmali generated on the label id. Reproduced here rather than read
#: from the decode so the test runs without a multi-gigabyte artifact, but nothing
#: about the shape is invented.
ANNOTATED_ACTION_BAR = """.class public final LX/0Di2;

.method public final A02()V
    .locals 15

    .line 91
    iput-object v11, v1, LX/07uJ;->A0F:Landroid/graphics/drawable/Drawable;

    const v0, 0x7f134a34    # 1.957818E38f

    iput v0, v1, LX/07uJ;->A06:I

    iput-object v14, v1, LX/07uJ;->A0G:Landroid/view/View$OnClickListener;

    iput-object v13, v1, LX/07uJ;->A0H:Landroid/view/View$OnLongClickListener;

    return-void
.end method
"""


class AnnotatedLineAnchorTests(unittest.TestCase):
    """Anchors must survive baksmali's generated comments. A real 439 defect.

    `install_settings_long_click_actionbar` resolved on 430, where the label id
    was the bare `const v0, 0x7f134a0e`, and reported "anchor pattern did not
    match" on 439, where the id changed to `0x7f134a34` and baksmali decided that
    one spelled a float worth annotating. Whether the annotation appears is a
    property of the number, so this is not specific to the action bar or to 439:
    any anchor whose last capture lands on a constant can acquire one in the next
    version and go quiet.
    """

    def setUp(self):
        self.hooks = {hook.hook_id: hook for hook in load_manifest(MANIFEST)}

    def test_an_any_capture_matches_across_a_generated_annotation(self):
        hook = self.hooks["install_settings_long_click_actionbar"]
        result = resolve_in_source(hook, "LX/0Di2;", ANNOTATED_ACTION_BAR)
        self.assertTrue(result.resolved, result.reason)
        self.assertEqual(result.occurrences, 1)
        self.assertEqual(
            result.bindings,
            {
                "drawable": "v11",
                "cfg": "v1",
                "cfgcls": "LX/07uJ;",
                "df": "A0F",
                "lbl": "v0",
                "labelid": "0x7f134a34",
                "lf": "A06",
                "click": "v14",
                "cf": "A0G",
                "long": "v13",
                "lcf": "A0H",
            },
        )

    def test_the_capture_stops_at_the_constant_and_never_eats_the_comment(self):
        # Stated separately from the bindings above because this is the value that
        # renders into the payload: a `labelid` carrying `# 1.957818E38f` would
        # emit smali that does not assemble.
        hook = self.hooks["install_settings_long_click_actionbar"]
        result = resolve_in_source(hook, "LX/0Di2;", ANNOTATED_ACTION_BAR)
        self.assertEqual(result.bindings["labelid"], "0x7f134a34")
        self.assertNotIn("#", result.bindings["labelid"])
        self.assertNotIn("1.957818E38f", result.bindings["labelid"])

    def test_the_emitted_anchor_keeps_the_annotation_so_the_applier_finds_it(self):
        """The half of the fix that a resolve-only check would miss.

        The applier does not re-run the pattern. It takes the concrete anchor and
        compares it literally against its own significant view, which keeps the
        comment. An anchor rendered from the template alone drops it and matches
        zero lines — measured, not argued: this exact test against the real
        `LX/0Di2.smali` returned 0 hits before the emitted form changed.
        """
        sys.path.insert(0, str(RECONSTRUCTION_TOOLS))
        try:
            from apply_anchored_patches import find_anchors
        except ImportError:  # pragma: no cover - tool tree is present in-repo
            self.skipTest("reconstruction tools not importable")
        finally:
            sys.path.remove(str(RECONSTRUCTION_TOOLS))
        hook = self.hooks["install_settings_long_click_actionbar"]
        result = resolve_in_source(hook, "LX/0Di2;", ANNOTATED_ACTION_BAR)
        self.assertEqual(result.anchor[1], "const v0, 0x7f134a34    # 1.957818E38f")
        hits = find_anchors(ANNOTATED_ACTION_BAR.splitlines(), result.anchor)
        self.assertEqual(len(hits), hook.expected_anchor_count)

    def test_the_emitted_anchor_is_the_rendered_template_when_nothing_is_annotated(self):
        # Emitting matched source rather than the rendered template must be a
        # no-op everywhere the old behaviour was already right, or this fix would
        # be a rewrite of what every other hook emits.
        cases = {
            "set_app_context": (
                "Lcom/instagram/app/InstagramAppShell;",
                CLEAN_APP_SHELL,
            ),
            "replace_reels_discover_endpoint": (
                "LX/04tC;",
                '.class LX/04tC;\n    const-string v1, "clips/discover/"\n',
            ),
            "replace_reels_homecoming_endpoint": (
                "LX/04tC;",
                '.class LX/04tC;\n    const-string v6, "clips/homecoming/"\n',
            ),
            "replace_reels_stream_endpoint": (
                "LX/04tC;",
                '.class LX/04tC;\n    const-string v7, "clips/discover/stream/"\n',
            ),
        }
        for hook_id, (descriptor, source) in cases.items():
            with self.subTest(hook_id=hook_id):
                hook = self.hooks[hook_id]
                result = resolve_in_source(hook, descriptor, source)
                self.assertTrue(result.resolved, result.reason)
                self.assertEqual(
                    result.anchor,
                    [render(line, result.bindings) for line in hook.anchor],
                )

    def test_a_marker_comment_is_still_counted_on_a_class_full_of_annotations(self):
        """The trap. The idempotence marker IS a comment.

        A marker must be a comment and never a label, because baksmali deletes an
        unreferenced label — so any change that treats comments as noise is one
        step from making every hook re-apply on top of itself. The marker is
        counted against the raw text, before the significant view exists at all,
        and that has to stay true on a class where real annotations are present.
        """
        hook = self.hooks["install_settings_long_click_actionbar"]
        self.assertTrue(hook.marker.startswith("#"))
        applied = ANNOTATED_ACTION_BAR.replace(
            "    iput-object v13, v1, LX/07uJ;->A0H:"
            "Landroid/view/View$OnLongClickListener;",
            "\n".join(
                render(
                    line,
                    {"long": "v13", "cfg": "v1", "cfgcls": "LX/07uJ;", "lcf": "A0H"},
                )
                for line in hook.payload
            ),
        )
        self.assertEqual(applied.count(hook.marker), hook.expected_marker_count)
        result = resolve_in_source(hook, "LX/0Di2;", applied)
        self.assertTrue(result.already_applied)
        self.assertFalse(result.resolved)
        self.assertIn("already applied", result.reason)

    def test_a_marker_stripped_from_the_matching_view_is_still_a_marker(self):
        # Positive control for the test above: prove the marker really is invisible
        # to the significant view, so "already applied" can only have come from the
        # raw-text count and not from some line that happened to survive.
        hook = self.hooks["install_settings_long_click_actionbar"]
        self.assertEqual(significant([f"    {hook.marker}"]), [])
        self.assertEqual(strip_comment(f"    {hook.marker}").strip(), "")

    def test_a_hash_inside_a_string_literal_does_not_truncate_a_capture(self):
        """The failure mode of the fix that was not taken.

        Stripping at the first `#` — or letting the regex treat ` #` as a comment
        opener — binds `s` to `"Using` here and calls it a match. That is worse
        than the original bug: it does not fail, it patches the wrong line with a
        payload built from a truncated literal.
        """
        hook = make_hook(
            anchor=("const-string <r:reg>, <s:any>",),
            payload=("    invoke-static {<r>}, LH;->f(Ljava/lang/String;)V",),
        )
        exact = 'const-string v0, "a#b"\n'
        result = resolve_in_source(hook, "LFoo;", exact)
        self.assertTrue(result.resolved, result.reason)
        self.assertEqual(result.bindings, {"r": "v0", "s": '"a#b"'})

        spaced = 'const-string v0, "Using more than the expected # of framebuffers"\n'
        result = resolve_in_source(hook, "LFoo;", spaced)
        self.assertFalse(result.resolved)
        self.assertEqual(result.reason, "anchor pattern did not match")

    def test_an_anchor_line_cannot_be_satisfied_by_a_comment_alone(self):
        # Negative control. `strip_comment` turns a whole-line comment into "",
        # so a bug that fed stripped text through without dropping empties could
        # let a comment stand in for a missing instruction. `significant` removes
        # it first, and this pins that ordering.
        hook = make_hook(
            anchor=("nop", "return-void"),
            payload=("    invoke-static {}, LH;->f()V",),
            marker="LH;->f()V",
        )
        # Positive control: the same anchor over real instructions does match, so
        # the failure below is the comment and not a broken fixture.
        control = resolve_in_source(hook, "LFoo;", "    nop\n    return-void\n")
        self.assertTrue(control.resolved, control.reason)
        result = resolve_in_source(hook, "LFoo;", "    nop\n    # return-void\n")
        self.assertFalse(result.resolved)
        self.assertEqual(result.reason, "anchor pattern did not match")


class AsOperationTests(unittest.TestCase):
    def setUp(self):
        self.hooks = {hook.hook_id: hook for hook in load_manifest(MANIFEST)}

    def test_emits_exactly_the_keys_the_applier_reads(self):
        hook = self.hooks["set_app_context"]
        result = resolve_in_source(
            hook, "Lcom/instagram/app/InstagramAppShell;", CLEAN_APP_SHELL
        )
        operation = result.as_operation(hook)
        self.assertEqual(
            set(operation),
            {
                "id",
                "descriptor",
                "mode",
                "anchor",
                "expected_anchor_count",
                "marker",
                "expected_marker_count",
                "payload",
            },
        )

    def test_carries_hook_level_fields_and_resolution_level_fields(self):
        hook = self.hooks["set_app_context"]
        result = resolve_in_source(
            hook, "Lcom/instagram/app/InstagramAppShell;", CLEAN_APP_SHELL
        )
        operation = result.as_operation(hook)
        self.assertEqual(operation["id"], hook.hook_id)
        self.assertEqual(operation["mode"], hook.mode)
        self.assertEqual(operation["marker"], hook.marker)
        self.assertEqual(operation["expected_marker_count"], hook.expected_marker_count)
        self.assertEqual(operation["expected_anchor_count"], hook.expected_anchor_count)
        self.assertEqual(operation["descriptor"], "Lcom/instagram/app/InstagramAppShell;")
        self.assertEqual(operation["anchor"], result.anchor)
        self.assertEqual(operation["payload"], result.payload)

    def test_emits_plain_lists_so_the_operation_is_json_serialisable(self):
        hook = self.hooks["set_app_context"]
        result = resolve_in_source(
            hook, "Lcom/instagram/app/InstagramAppShell;", CLEAN_APP_SHELL
        )
        operation = result.as_operation(hook)
        self.assertIsInstance(operation["anchor"], list)
        self.assertIsInstance(operation["payload"], list)
        json.dumps(operation)

    def test_emitted_lists_are_copies(self):
        hook = self.hooks["set_app_context"]
        result = resolve_in_source(
            hook, "Lcom/instagram/app/InstagramAppShell;", CLEAN_APP_SHELL
        )
        operation = result.as_operation(hook)
        operation["anchor"].append("tampered")
        self.assertNotIn("tampered", result.anchor)

    def test_no_capture_syntax_survives_into_the_operation(self):
        for hook_id, descriptor, source in (
            (
                "set_app_context",
                "Lcom/instagram/app/InstagramAppShell;",
                CLEAN_APP_SHELL,
            ),
            (
                "replace_reels_discover_endpoint",
                "LX/04tC;",
                '.class LX/04tC;\n    const-string v1, "clips/discover/"\n',
            ),
        ):
            with self.subTest(hook=hook_id):
                hook = self.hooks[hook_id]
                # The templates definitely contain captures before resolution.
                self.assertTrue(CAPTURE.search("\n".join(hook.anchor + hook.payload)))
                operation = resolve_in_source(hook, descriptor, source).as_operation(hook)
                rendered = "\n".join(operation["anchor"] + operation["payload"])
                self.assertIsNone(CAPTURE.search(rendered))

    def test_raises_when_unresolved(self):
        hook = self.hooks["set_app_context"]
        result = resolve_in_source(hook, "LFoo;", ".class LFoo;\n    return-void\n")
        self.assertFalse(result.resolved)
        with self.assertRaises(ManifestError) as caught:
            result.as_operation(hook)
        self.assertIn("set_app_context", str(caught.exception))
        self.assertIn("unresolved", str(caught.exception))
        self.assertIn(result.reason, str(caught.exception))

    def test_raises_for_a_hand_built_unresolved_resolution(self):
        resolution = Resolution("x", False, reason="nope")
        with self.assertRaises(ManifestError):
            resolution.as_operation(make_hook())

    def test_the_real_applier_accepts_the_emitted_operation(self):
        """End-to-end: resolve -> emit -> the checked-in applier patches the file."""
        sys.path.insert(0, str(RECONSTRUCTION_TOOLS))
        try:
            from apply_anchored_patches import apply_operation
        except ImportError:  # pragma: no cover - tool tree is present in-repo
            self.skipTest("reconstruction tools not importable")
        finally:
            sys.path.remove(str(RECONSTRUCTION_TOOLS))

        hook = self.hooks["set_app_context"]
        result = resolve_in_source(
            hook, "Lcom/instagram/app/InstagramAppShell;", CLEAN_APP_SHELL
        )
        operation = result.as_operation(hook)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "InstagramAppShell.smali"
            target.write_text(CLEAN_APP_SHELL, encoding="utf-8")
            self.assertEqual(apply_operation(target, operation), "applied")
            patched = target.read_text(encoding="utf-8")
            self.assertIn(hook.marker, patched)
            self.assertEqual(patched.count(hook.marker), hook.expected_marker_count)
            # The applier is itself idempotent on a second pass.
            self.assertEqual(apply_operation(target, operation), "already_applied")

    def test_the_real_applier_accepts_a_replace_mode_operation(self):
        sys.path.insert(0, str(RECONSTRUCTION_TOOLS))
        try:
            from apply_anchored_patches import apply_operation
        except ImportError:  # pragma: no cover - tool tree is present in-repo
            self.skipTest("reconstruction tools not importable")
        finally:
            sys.path.remove(str(RECONSTRUCTION_TOOLS))

        hook = self.hooks["replace_reels_discover_endpoint"]
        source = (
            ".class LX/04tC;\n"
            ".method public A00()Ljava/lang/String;\n"
            "    .locals 1\n"
            "\n"
            '    const-string v1, "clips/discover/"\n'
            "\n"
            "    return-object v1\n"
            ".end method\n"
        )
        result = resolve_in_source(hook, "LX/04tC;", source)
        operation = result.as_operation(hook)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "0.smali"
            target.write_text(source, encoding="utf-8")
            self.assertEqual(apply_operation(target, operation), "applied")
            patched = target.read_text(encoding="utf-8")
            self.assertIn(
                "invoke-static {v1}, Lcom/dfinstagram/hooks;->replaceReelsEndpoint",
                patched,
            )
            self.assertIn("    move-result-object v1", patched)
            self.assertEqual(patched.count(hook.marker), hook.expected_marker_count)


class LoadManifestTests(unittest.TestCase):
    def test_loads_the_real_manifest(self):
        hooks = load_manifest(MANIFEST)
        self.assertEqual(len(hooks), 7)
        self.assertTrue(all(isinstance(hook, Hook) for hook in hooks))

    def test_accepts_a_string_path(self):
        hooks = load_manifest(str(MANIFEST))  # type: ignore[arg-type]
        self.assertEqual(len(hooks), 7)

    def test_rejects_an_unsupported_schema_version(self):
        payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
        for version in (2, 0, "1", None):
            with self.subTest(version=version):
                payload["schema_version"] = version
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "hooks.json"
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(ManifestError) as caught:
                        load_manifest(path)
                    self.assertIn("unsupported hook manifest schema", str(caught.exception))

    def test_rejects_a_manifest_with_no_schema_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hooks.json"
            path.write_text(json.dumps({"hooks": []}), encoding="utf-8")
            with self.assertRaises(ManifestError):
                load_manifest(path)

    def test_propagates_hook_validation_failures(self):
        payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
        payload["hooks"][0]["tier"] = "bogus"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hooks.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ManifestError) as caught:
                load_manifest(path)
            self.assertIn("unknown tier", str(caught.exception))

    def test_rejects_a_duplicate_hook_id(self):
        # Callers index hooks by id (every test here does), so a duplicate would
        # silently shadow the first entry and one of the two hooks would never run.
        payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
        duplicate = json.loads(json.dumps(payload["hooks"][0]))
        duplicate["intent"] = "a second entry claiming the same id"
        payload["hooks"].append(duplicate)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hooks.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ManifestError) as caught:
                load_manifest(path)
            message = str(caught.exception)
            self.assertIn("duplicate hook_id", message)
            self.assertIn("set_app_context", message)

    def test_accepts_distinct_hook_ids(self):
        # The duplicate check must key on the id, not on the entry as a whole.
        # The clone needs its own marker too: a hook that shares one reads the
        # other's applied patch as its own, so that is refused separately.
        payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
        clone = json.loads(json.dumps(payload["hooks"][0]))
        clone["hook_id"] = "set_app_context_second_shell"
        clone["marker"] = "Lcom/dfinstagram/startapp;->setSecondContext(Landroid/app/Application;)V"
        # load_manifest now rejects any active hook whose payload does not call
        # its own runtime identity, so the clone must be instrumented for its new
        # id. instrument() is the single source of that exact line.
        clone["payload"] = list(
            instrument(
                ["", "    invoke-static {<app>}, " + clone["marker"]],
                clone["hook_id"],
            )
        )
        payload["hooks"].append(clone)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hooks.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            hooks = load_manifest(path)
        self.assertEqual(len(hooks), 8)
        self.assertEqual(hooks[-1].hook_id, "set_app_context_second_shell")

    def test_rejects_two_hooks_sharing_a_marker(self):
        """A shared marker makes each hook report the other's patch as its own.

        Both then drop out of the build while the run still reports complete —
        the one failure mode that produces a silently incomplete APK. Two hooks
        in this repo's own manifest really did share
        `Lcom/dfinstagram/SettingsWrapper;`.
        """
        payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
        clone = json.loads(json.dumps(payload["hooks"][0]))
        clone["hook_id"] = "set_app_context_clone"
        payload["hooks"].append(clone)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hooks.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ManifestError) as caught:
                load_manifest(path)
        self.assertIn("share the marker", str(caught.exception))

    def test_rejects_an_active_hook_whose_payload_omits_its_probe_call(self):
        """An active hook that does not announce its own execution is refused.

        This is the load-time half of the runtime-identity change: presence in
        the manifest is no longer allowed to stand in for a hook that reports
        when it runs. Four patches in this project were applied and never
        executed; the manifest now refuses the shape that hides that.
        """
        payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
        hook = payload["hooks"][0]
        hook_id = hook["hook_id"]
        self.assertEqual(hook.get("status", "active"), "active")
        call = probe_call(hook_id)
        # Strip the hook's own probe call back out, leaving it uninstrumented.
        hook["payload"] = [
            line for line in hook["payload"] if line.strip() != call.strip()
        ]
        self.assertNotIn(call.strip(), [line.strip() for line in hook["payload"]])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hooks.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ManifestError) as caught:
                load_manifest(path)
        message = str(caught.exception)
        # It must name the offending hook and quote the exact call to add, so a
        # human can fix the manifest without reading the loader's source.
        self.assertIn(hook_id, message)
        self.assertIn(call.strip(), message)

    def test_exempts_a_non_active_hook_from_the_probe_requirement(self):
        """A retired hook is not required to be instrumented.

        Only active hooks reach a build, so only active hooks can be silently
        dead; the requirement is scoped to them deliberately, as the loader's
        `status != "active"` skip records.
        """
        payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
        retired = json.loads(json.dumps(payload["hooks"][0]))
        retired["hook_id"] = "set_app_context_retired"
        retired["marker"] = (
            "Lcom/dfinstagram/startapp;->setRetiredContext(Landroid/app/Application;)V"
        )
        retired["status"] = "retired"
        # Deliberately NOT instrumented — no probe call for its id anywhere.
        retired["payload"] = ["", "    invoke-static {<app>}, " + retired["marker"]]
        payload["hooks"].append(retired)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hooks.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            hooks = load_manifest(path)  # must not raise
        loaded = {hook.hook_id: hook for hook in hooks}
        self.assertIn("set_app_context_retired", loaded)
        self.assertEqual(loaded["set_app_context_retired"].status, "retired")
        # The exemption is a genuine skip, not a silent instrumentation.
        self.assertFalse(
            is_instrumented(
                loaded["set_app_context_retired"].payload, "set_app_context_retired"
            )
        )

    def test_the_shipped_manifest_satisfies_the_probe_requirement(self):
        """The checked-in manifest must itself pass the new load-time gate."""
        if not MANIFEST.exists():
            self.skipTest(f"{MANIFEST} is not present")
        hooks = load_manifest(MANIFEST)  # raises if any active hook is uninstrumented
        for hook in hooks:
            if hook.status == "active":
                with self.subTest(hook=hook.hook_id):
                    self.assertTrue(is_instrumented(hook.payload, hook.hook_id))


class RealManifestContentTests(unittest.TestCase):
    """The checked-in manifest must satisfy the invariants the resolver assumes."""

    def setUp(self):
        self.hooks = load_manifest(MANIFEST)

    def test_hook_ids_are_unique_and_expected(self):
        ids = [hook.hook_id for hook in self.hooks]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(
            set(ids),
            {
                "set_app_context",
                "tigon_url_block",
                "replace_reels_discover_endpoint",
                "replace_reels_homecoming_endpoint",
                "replace_reels_stream_endpoint",
                "install_settings_long_click",
                "install_settings_long_click_actionbar",
            },
        )

    def test_every_hook_has_at_least_one_host(self):
        for hook in self.hooks:
            with self.subTest(hook=hook.hook_id):
                self.assertGreaterEqual(len(hook.hosts), 1)

    def test_every_hook_has_a_marker(self):
        for hook in self.hooks:
            with self.subTest(hook=hook.hook_id):
                self.assertTrue(hook.marker.strip())
                self.assertGreaterEqual(hook.expected_marker_count, 1)

    def test_every_marker_actually_appears_in_its_own_payload(self):
        # Otherwise the applier's idempotence check can never see the hook land.
        for hook in self.hooks:
            with self.subTest(hook=hook.hook_id):
                rendered = "\n".join(hook.payload)
                self.assertIn(hook.marker, rendered)
                self.assertEqual(rendered.count(hook.marker), hook.expected_marker_count)

    def test_every_by_literal_host_literal_is_declared_as_a_semantic_dep(self):
        for hook in self.hooks:
            for host in hook.hosts:
                if host.kind == "by_literal":
                    with self.subTest(hook=hook.hook_id, literal=host.literal):
                        assert host.literal is not None
                        self.assertIn(host.literal, hook.semantic_deps)

    def test_every_by_literal_host_literal_also_appears_in_its_anchor(self):
        for hook in self.hooks:
            for host in hook.hosts:
                if host.kind == "by_literal":
                    with self.subTest(hook=hook.hook_id, literal=host.literal):
                        assert host.literal is not None
                        self.assertIn(host.literal, "\n".join(hook.anchor))

    def test_every_host_note_is_populated(self):
        for hook in self.hooks:
            for index, host in enumerate(hook.hosts):
                with self.subTest(hook=hook.hook_id, host=index):
                    self.assertTrue(host.note.strip())

    def test_every_hook_carries_a_probe(self):
        for hook in self.hooks:
            with self.subTest(hook=hook.hook_id):
                self.assertIsInstance(hook.probe, Probe)
                assert hook.probe is not None
                self.assertTrue(hook.probe.kind)
                self.assertTrue(hook.probe.signal)
                self.assertTrue(hook.probe.surface)

    def test_every_probe_kind_is_one_the_verify_stage_can_run(self):
        for hook in self.hooks:
            with self.subTest(hook=hook.hook_id):
                assert hook.probe is not None
                self.assertIn(hook.probe.kind, PROBE_KINDS)

    def test_every_waived_probe_says_why_in_its_note(self):
        # This one caught a real omission: the actionbar hook waived the delta with
        # no explanation at all, and it is precisely the hook that applied cleanly
        # on 430 while being inert at runtime. Waiving is reserved for hooks with no
        # toggle, so the note has to say that much.
        for hook in self.hooks:
            if hook.probe and not hook.probe.requires_two_directional_delta:
                with self.subTest(hook=hook.hook_id):
                    self.assertTrue(hook.probe.note.strip())
                    self.assertIn("toggle", hook.probe.note.lower())

    def test_every_hook_is_active(self):
        for hook in self.hooks:
            with self.subTest(hook=hook.hook_id):
                self.assertEqual(hook.status, "active")

    def test_replace_mode_markers_are_comments_not_labels(self):
        # baksmali deletes unreferenced labels, so a `:label` marker would vanish.
        for hook in self.hooks:
            if hook.mode == "replace":
                with self.subTest(hook=hook.hook_id):
                    self.assertTrue(hook.marker.startswith("#"))

    def test_every_payload_capture_is_declared_by_its_anchor(self):
        # `__post_init__` already enforces this; assert it holds for the real file
        # so a manifest edit that slips a stray `<name>` in fails here by name.
        for hook in self.hooks:
            declared: set[str] = set()
            for _, names in compile_anchor(hook.anchor):
                declared.update(names)
            for line in hook.payload:
                for match in CAPTURE.finditer(line):
                    with self.subTest(hook=hook.hook_id, capture=match.group("name")):
                        self.assertIn(match.group("name"), declared)


class DesignIntentTests(unittest.TestCase):
    """Pins the claim in the module docstring: five hooks resolve without an agent."""

    def setUp(self):
        self.hooks = load_manifest(MANIFEST)

    def test_there_are_five_robust_hooks_and_two_ui_hooks(self):
        tiers = [hook.tier for hook in self.hooks]
        self.assertEqual(tiers.count("robust"), 5)
        self.assertEqual(tiers.count("ui"), 2)
        self.assertEqual(tiers.count("fragile"), 0)

    def test_every_robust_anchor_contains_at_least_one_capture(self):
        # A capture-free anchor would be a hardcoded 430/439 line, which is exactly
        # what this manifest exists to replace.
        robust = [hook for hook in self.hooks if hook.tier == "robust"]
        self.assertEqual(len(robust), 5)
        for hook in robust:
            with self.subTest(hook=hook.hook_id):
                captures = {
                    match.group("name")
                    for line in hook.anchor
                    for match in CAPTURE.finditer(line)
                }
                self.assertTrue(
                    captures, f"{hook.hook_id} anchor is fully hardcoded"
                )

    def test_no_robust_anchor_names_an_obfuscated_class_literally(self):
        # Obfuscated names (LX/....;) moved between 430 and 439; a robust anchor
        # must capture them, never spell them out.
        for hook in self.hooks:
            if hook.tier != "robust":
                continue
            with self.subTest(hook=hook.hook_id):
                self.assertNotRegex("\n".join(hook.anchor), r"LX/\w+;")
                self.assertNotRegex("\n".join(hook.payload), r"LX/\w+;")

    def test_no_robust_host_needs_an_agent(self):
        for hook in self.hooks:
            if hook.tier != "robust":
                continue
            with self.subTest(hook=hook.hook_id):
                self.assertTrue(
                    all(host.kind in {"named", "by_literal"} for host in hook.hosts)
                )

    def test_both_ui_hooks_are_declared_by_agent(self):
        ui = [hook for hook in self.hooks if hook.tier == "ui"]
        self.assertEqual(len(ui), 2)
        for hook in ui:
            with self.subTest(hook=hook.hook_id):
                self.assertEqual([host.kind for host in hook.hosts], ["by_agent"])

    def test_robust_anchors_capture_the_parts_the_430_439_diff_showed_moving(self):
        hooks = {hook.hook_id: hook for hook in self.hooks}
        expectations = {
            # only the register moved
            "set_app_context": {"app"},
            # the request parameter's type moved
            "tigon_url_block": {"uri", "req", "reqcls", "urifield"},
            # only the owning class moved; the register is captured for writeback
            "replace_reels_discover_endpoint": {"r"},
            "replace_reels_homecoming_endpoint": {"r"},
            "replace_reels_stream_endpoint": {"r"},
        }
        for hook_id, expected in expectations.items():
            with self.subTest(hook=hook_id):
                declared: set[str] = set()
                for _, names in compile_anchor(hooks[hook_id].anchor):
                    declared.update(names)
                self.assertEqual(declared, expected)

    def test_the_ui_anchors_are_wide_enough_to_disambiguate(self):
        # Both UI hooks sit in classes where the single-line form is not unique;
        # the manifest records that by using multi-line anchors.
        hooks = {hook.hook_id: hook for hook in self.hooks}
        self.assertGreaterEqual(len(hooks["install_settings_long_click"].anchor), 3)
        self.assertGreaterEqual(
            len(hooks["install_settings_long_click_actionbar"].anchor), 5
        )


class KnownGapTests(unittest.TestCase):
    """Characterisation tests for behaviour that is reported, not fixed.

    Each of these pins what the module does today. If one starts failing, the
    corresponding gap was fixed and the test should be rewritten to assert the
    new, better behaviour.

    Eight gaps that once lived here have been fixed. Their tests moved out to the
    class each one belongs to and now assert the corrected behaviour, keeping a
    docstring that records what the bug was:

        marker vs expected_marker_count  ResolveInSourceTests
        empty marker accepted            HookValidationTests
        empty payload accepted           HookValidationTests
        expected_anchor_count > 1        HookValidationTests
        cross-line kind re-declaration   CompilePatternTests / CompileAnchorTests
        `<init:reg>` lexed as a capture  ReservedConstructorTests
        probe fields never validated     ProbeTests
        `type` rejected array descriptors KindMatchingTests
    """

    def test_gap_resolution_smali_path_is_never_populated(self):
        """`Resolution.smali_path` exists but `resolve_in_source` never sets it."""
        hooks = {hook.hook_id: hook for hook in load_manifest(MANIFEST)}
        result = resolve_in_source(
            hooks["set_app_context"],
            "Lcom/instagram/app/InstagramAppShell;",
            CLEAN_APP_SHELL,
        )
        self.assertTrue(result.resolved)
        self.assertIsNone(result.smali_path)


if __name__ == "__main__":
    unittest.main()

"""Tests for `runtime_identity`: the per-hook execution identity.

Every hook's payload now carries one no-argument static call named after the
hook. The name is the identity, so the call needs no register and can never
force a `.locals` change — the whole reason the design puts the id in the method
name rather than an argument. These tests pin that property and the generator
that produces the class, and a `MutationTests` class shows each guard biting: a
guard that never changes an answer would be decoration.

The hooks here are built with `hook_manifest.Hook(...)` directly. That is
deliberate — a directly constructed hook is NOT checked for instrumentation
(only `load_manifest` is), so these fixtures can hold whatever shape a test
needs without the manifest loader refusing them first.
"""

import unittest

from dfinsta_pipeline.hook_manifest import Hook, HostFingerprint, ManifestError
from dfinsta_pipeline.runtime_identity import (
    PROBE_DESCRIPTOR,
    expected_dex_symbols,
    instrument,
    is_instrumented,
    probe_call,
    probe_method,
    render_probe_class,
)


def make_hook(hook_id: str, *, status: str = "active", marker: str | None = None) -> Hook:
    """A minimal valid hook. Only `hook_id` and `status` matter to this module.

    render_probe_class and expected_dex_symbols read nothing but the id and the
    status, so the anchor/payload here just have to satisfy `Hook.__post_init__`.
    """
    marker = marker if marker is not None else f"# mark::{hook_id}"
    return Hook(
        hook_id=hook_id,
        intent="i",
        tier="robust",
        strategy="s",
        semantic_deps=(),
        hosts=(HostFingerprint("named", descriptor="LFoo;"),),
        anchor=("nop",),
        payload=(f"    {marker}", "    return-void"),
        marker=marker,
        expected_marker_count=1,
        status=status,
    )


class ProbeMethodTests(unittest.TestCase):
    def test_derives_the_method_name_from_the_hook_id(self):
        self.assertEqual(
            probe_method("replace_reels_discover_endpoint"),
            "h_replace_reels_discover_endpoint",
        )

    def test_prefixes_h_so_a_digit_leading_id_is_a_valid_identifier(self):
        # A hook id may begin with a digit (the 430/439 investigations are named
        # that way); the `h_` prefix is what keeps the method name legal smali.
        self.assertEqual(probe_method("430_settings"), "h_430_settings")
        self.assertTrue(probe_method("439").isidentifier())

    def test_replaces_every_non_identifier_character(self):
        self.assertEqual(probe_method("a.b-c/d"), "h_a_b_c_d")
        self.assertEqual(probe_method("clips/discover/"), "h_clips_discover_")
        # Each offending character is replaced individually, not collapsed.
        self.assertEqual(probe_method("a..b"), "h_a__b")

    def test_an_empty_or_blank_id_raises(self):
        for bad in ("", "   ", "\t\n"):
            with self.subTest(bad=bad):
                with self.assertRaises(ManifestError):
                    probe_method(bad)


class ProbeCallTests(unittest.TestCase):
    def test_names_the_hook_method_on_the_probe_class(self):
        self.assertEqual(
            probe_call("set_app_context"),
            "    invoke-static {}, Lcom/dfinstagram/probe;->h_set_app_context()V",
        )

    def test_takes_no_registers(self):
        # Register pressure is the entire reason the identity lives in the method
        # name: an empty {} argument list can never force a `.locals` bump and can
        # never clobber a register the payload still needs.
        for hook_id in ("set_app_context", "430_settings", "a.b/c"):
            with self.subTest(hook_id=hook_id):
                call = probe_call(hook_id)
                self.assertIn("{}", call)
                self.assertNotIn("{v", call)
                self.assertNotIn("{p", call)


class RenderProbeClassTests(unittest.TestCase):
    def test_one_method_per_active_hook_and_none_for_retired(self):
        hooks = [
            make_hook("alpha"),
            make_hook("beta"),
            make_hook("gone", status="retired"),
        ]
        text = render_probe_class(hooks)
        self.assertIn(".method public static h_alpha()V", text)
        self.assertIn(".method public static h_beta()V", text)
        self.assertNotIn("h_gone", text)
        # Exactly the two active hooks, no more.
        self.assertEqual(text.count(".method public static h_"), 2)

    def test_raises_on_an_empty_hook_set(self):
        with self.assertRaises(ManifestError):
            render_probe_class([])

    def test_raises_when_no_hook_is_active(self):
        with self.assertRaises(ManifestError):
            render_probe_class([make_hook("gone", status="retired")])

    def test_raises_when_two_ids_collide_into_one_method(self):
        # `a.b` and `a_b` both derive `h_a_b`; a single method cannot carry two
        # hooks' identities, so the generator must refuse the set.
        with self.assertRaises(ManifestError) as caught:
            render_probe_class([make_hook("a.b"), make_hook("a_b")])
        message = str(caught.exception)
        self.assertIn("collide", message)
        self.assertIn("h_a_b", message)

    def test_declares_the_class_the_clinit_and_the_private_fired_method(self):
        text = render_probe_class([make_hook("alpha")])
        self.assertIn(".class public final Lcom/dfinstagram/probe;", text)
        self.assertIn(".method static constructor <clinit>()V", text)
        self.assertIn(".method private static fired(Ljava/lang/String;)V", text)

    def test_dedup_uses_concurrent_hash_map_put_if_absent_not_a_hash_set(self):
        """First-touch dedup must be `ConcurrentHashMap.putIfAbsent`, not a `HashSet`.

        `fired` is reached from `throwIfBlocked`, which runs on Instagram's own
        network threads, so the set of already-logged ids is mutated
        concurrently. A plain `HashSet` mutated from several threads can spin
        forever or corrupt its table, and the manifest requires this helper never
        to throw or hang — a logging aid that can wedge the app is a worse bug
        than the silent-dead-hook it exists to expose. `putIfAbsent` gives
        lock-free "is this the first execution?" with none of that risk.
        """
        text = render_probe_class([make_hook("alpha")])
        self.assertIn("Ljava/util/concurrent/ConcurrentHashMap;", text)
        self.assertIn("putIfAbsent", text)
        self.assertNotIn("HashSet", text)


class InstrumentTests(unittest.TestCase):
    def test_prepends_the_probe_call_and_a_blank(self):
        payload = ("    body-a", "    body-b")
        self.assertEqual(
            instrument(payload, "h"),
            (probe_call("h"), "") + payload,
        )

    def test_prepends_rather_than_appends(self):
        result = instrument(("    body",), "h")
        self.assertEqual(result[0], probe_call("h"))
        self.assertNotEqual(result[-1], probe_call("h"))

    def test_is_idempotent(self):
        payload = ("    body-a", "    body-b")
        once = instrument(payload, "h")
        twice = instrument(once, "h")
        self.assertEqual(once, twice)

    def test_recognises_an_existing_call_anywhere_in_the_payload(self):
        # This is the shape the shipped manifest uses: a blank line, then the
        # call. instrument must treat it as already done and not prepend a second.
        payload = ("", probe_call("h"), "", "    body")
        self.assertEqual(instrument(payload, "h"), tuple(payload))

    def test_preserves_a_leading_blank_line(self):
        payload = ("", "    body")
        result = instrument(payload, "h")
        # The prepended call and its blank sit in front; the original leading
        # blank survives, it is not swallowed.
        self.assertEqual(result, (probe_call("h"), "", "", "    body"))


class IsInstrumentedTests(unittest.TestCase):
    def test_true_when_the_call_is_present(self):
        self.assertTrue(is_instrumented((probe_call("h"), "    body"), "h"))

    def test_true_regardless_of_indentation(self):
        self.assertTrue(is_instrumented((probe_call("h").strip(), "    body"), "h"))

    def test_false_when_the_call_is_absent(self):
        self.assertFalse(is_instrumented(("    body",), "h"))

    def test_false_for_a_different_hook_id(self):
        self.assertFalse(is_instrumented((probe_call("other"),), "h"))

    def test_agrees_with_instrument(self):
        payload = ("    body",)
        self.assertFalse(is_instrumented(payload, "h"))
        self.assertTrue(is_instrumented(instrument(payload, "h"), "h"))


class ExpectedDexSymbolsTests(unittest.TestCase):
    def test_one_pair_per_active_hook_keyed_by_id(self):
        hooks = [
            make_hook("alpha"),
            make_hook("beta"),
            make_hook("gone", status="retired"),
        ]
        symbols = expected_dex_symbols(hooks)
        self.assertEqual(set(symbols), {"alpha", "beta"})
        self.assertEqual(symbols["alpha"], (PROBE_DESCRIPTOR, "h_alpha"))
        self.assertEqual(symbols["beta"], (PROBE_DESCRIPTOR, "h_beta"))

    def test_excludes_retired_hooks(self):
        self.assertEqual(
            expected_dex_symbols([make_hook("gone", status="retired")]), {}
        )

    def test_every_method_name_is_distinct(self):
        # Distinct method names are exactly what let the static verifier turn
        # "some DFInsta call is in this DEX" into "THIS hook's call is in this
        # DEX"; if two hooks shared a name the DEX could not be attributed.
        hooks = [make_hook(name) for name in ("alpha", "beta", "gamma", "delta")]
        methods = [method for _, method in expected_dex_symbols(hooks).values()]
        self.assertEqual(len(methods), len(hooks))
        self.assertEqual(len(set(methods)), len(methods))


class MutationTests(unittest.TestCase):
    """Each guard, shown biting.

    A guard that never changes an answer is decoration. Each test re-applies the
    real rule in its broken form to the same inputs and asserts the observable
    result changes, so "the guard is present" and "the guard matters" stay
    separate claims.
    """

    def test_removing_the_collision_check_lets_two_hooks_report_as_one(self):
        """Mutation: emit a method per hook without the duplicate-name check.

        Two ids differing only in punctuation derive one method name, so both
        call sites invoke the same probe method and one hook's execution is
        logged under the other's identity. The real generator refuses the set;
        this is what that refusal holds back.
        """
        hooks = [make_hook("a.b"), make_hook("a_b")]

        # The real guard fires.
        with self.assertRaises(ManifestError):
            render_probe_class(hooks)

        # The mutant — the same per-hook emission with the guard deleted — emits
        # two method definitions that share one name: two hooks, one identity.
        mutant_methods = [
            f".method public static {probe_method(hook.hook_id)}()V"
            for hook in hooks
            if hook.status == "active"
        ]
        self.assertEqual(len(mutant_methods), 2)
        self.assertEqual(len(set(mutant_methods)), 1)
        # And each hook's own call site would land on that one method.
        self.assertEqual(probe_call("a.b"), probe_call("a_b"))

    def test_appending_the_probe_call_would_hide_an_early_throw(self):
        """Mutation: append the probe call instead of prepending it.

        The call answers "did control reach this site". A payload whose first
        real instruction throws would, with the call appended, never run it — so
        the site would read as never-executed at exactly the moment it partly
        executed, which is the failure this whole mechanism exists to expose.
        """
        payload = ("    might-throw", "    tail")

        real = instrument(payload, "h")
        self.assertEqual(real[0], probe_call("h"))  # runs before anything else

        mutant = tuple(payload) + (probe_call("h"),)  # append instead of prepend
        self.assertEqual(mutant[-1], probe_call("h"))
        self.assertNotEqual(mutant[0], probe_call("h"))
        # The observable difference: the real order puts the report ahead of the
        # instruction that might throw; the mutant puts it behind it.
        self.assertEqual(real.index(probe_call("h")), 0)
        self.assertGreater(mutant.index(probe_call("h")), 0)

    def test_a_register_argument_would_force_a_locals_change(self):
        """Mutation: give the probe call a register operand.

        The empty {} operand list is load-bearing: it can never force a
        `.locals` bump and can never clobber a register the payload still holds
        live. A call that named a register (say v0) would risk both — the exact
        cost the method-name identity was chosen to avoid.
        """
        real = probe_call("h")
        self.assertIn("{}", real)
        self.assertNotIn("{v", real)
        self.assertNotIn("{p", real)

        mutant = real.replace("{}", "{v0}")  # now consumes a register
        self.assertIn("{v0}", mutant)
        self.assertNotIn("{}", mutant)


class IdentityAgreementTests(unittest.TestCase):
    """The generator and the verifier map must agree on what a valid hook set is.

    They did not. `render_probe_class` refused two hook ids that collide into one
    probe method while `expected_dex_symbols` silently returned the same
    `(descriptor, method)` pair for both — defeating the static half of
    attribution at exactly the point its docstring claims it holds, because the
    verifier could no longer tell the two hooks apart. Latent at the time only
    because nothing called the map yet, which is the worst kind of safe.
    """

    def colliding(self):
        return [make_hook("a.b"), make_hook("a_b")]

    def test_both_paths_refuse_a_colliding_hook_set(self):
        for function in (render_probe_class, expected_dex_symbols):
            with self.subTest(function=function.__name__):
                with self.assertRaises(ManifestError) as caught:
                    function(self.colliding())
                self.assertIn("collide", str(caught.exception))

    def test_only_the_generator_refuses_a_hook_set_with_nothing_active(self):
        """The emptiness rule is deliberately NOT shared.

        A probe class with no methods means nothing is instrumented, so
        generating one is refused. An empty symbol map is a truthful answer to
        "what should the verifier look for" when every hook is retired — and the
        vacuity danger there is handled where it matters, by `verify_build`
        refusing an empty host-hook map.
        """
        retired = [make_hook("only_one", status="retired")]
        with self.assertRaises(ManifestError):
            render_probe_class(retired)
        self.assertEqual(expected_dex_symbols(retired), {})

    def test_a_distinct_set_is_accepted_by_both(self):
        """The guard must not over-fire: normal hook ids have to pass."""
        hooks = [make_hook("alpha"), make_hook("beta")]
        symbols = expected_dex_symbols(hooks)
        self.assertEqual(sorted(symbols), ["alpha", "beta"])
        self.assertEqual(len({pair for pair in symbols.values()}), 2)
        rendered = render_probe_class(hooks)
        for hook_id in ("alpha", "beta"):
            self.assertIn(probe_method(hook_id), rendered)

    def test_every_method_the_generator_emits_is_in_the_verifier_map(self):
        """Anything the class defines must be something the verifier looks for."""
        hooks = [make_hook("alpha"), make_hook("beta"), make_hook("gamma")]
        rendered = render_probe_class(hooks)
        for _, method in expected_dex_symbols(hooks).values():
            self.assertIn(f".method public static {method}()V", rendered)


if __name__ == "__main__":
    unittest.main()

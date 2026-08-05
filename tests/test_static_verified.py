"""`static_verified` evidence: the kind that was required of every hook and produced by nothing.

`EvidenceKind.STATIC_VERIFIED` is in `MECHANICAL_REQUIREMENTS`, in
`AGENT_REQUIREMENTS` and in `ALREADY_APPLIED_REQUIREMENTS`, and until 2026-08-05
nothing in this tree emitted one. `build.py` wrote a verifier report,
`tools/verify/verify_build.py` computed every assertion in it, and no code joined
the two to the ledger — so `EvidenceLedger.report("post_build")` escalated 7 of 7
hooks on every version for a reason no hook could fix, and release readiness was
unsatisfiable by construction. :class:`PostBuildGapTests` is that sentence
written as a test, and it is the one class here that would still be worth having
if every other assertion in this file were deleted.

The producer is two functions in `driver.py`:

* `hook_symbol_map` — the same DFInsta symbols `host_hook_map` collects, keyed by
  hook instead of unioned per DEX. The union is exactly what makes the verifier's
  own report unusable as per-hook evidence: it says `classes3.dex` carries
  `Lcom/dfinstagram/hooks; replaceReelsEndpoint` and three Reels hooks contribute
  that pair, so the report alone cannot say which of them was proven.
* `static_verified_claims` — that map plus the report, as one `EvidenceClaim` per
  hook.

Four properties are load-bearing and each is easy to break without breaking
anything else in the program:

**The build's overall verdict gates every per-hook verdict.** A build can carry
every DFInsta symbol and still have a mismatched preserved entry or an unexpected
added file. A hook reported `passed` beside a failed build would read as though
the hook were fine. :class:`BuildVerificationGateTests`.

**A hook that contributed nothing is `failed`, not skipped.** That is the vacuous
pass `verify_build` refuses globally (`host_hooks` empty makes `all(...)` true and
certifies an unpatched graft), applied one hook at a time.
:class:`VacuousPassTests`.

**Attribution is three-state and honest.** `sole` only when every one of a hook's
symbols is contributed by no other hook. :class:`AttributionTests` uses the real
440 build, where `tigon_url_block` is sole and the three Reels hooks share
`Lcom/dfinstagram/hooks; replaceReelsEndpoint`.

**The per-hook and per-DEX views are one fact.** :class:`SymbolMapAgreementTests`
flattens `hook_symbol_map` and requires `host_hook_map` exactly. They are read
from the same payloads by two near-identical loops, which is precisely the shape
that drifts — the per-hook one was added rather than the per-DEX one
reinterpreted, so nothing but a test keeps them equal.

**Fixture provenance.** `SYMBOLS_440` and `HOST_HOOKS_440` are the real 440 clean
build: `work/440-clean/dfinsta.verification.json` and the map
`hook_symbol_map` produces from `work/440-clean/resolution.json` against
`work/440-clean/index`. They are inlined rather than read because `work/` is
gitignored, and a test that skips when its fixture is absent is a test that stops
running the moment the tree is cloned. `HOST_HOOKS_440` was checked against the
committed `host-hooks.json` of that run at the time it was copied in: flattening
`SYMBOLS_440` reproduces it, which :meth:`SymbolMapAgreementTests.
test_the_real_440_fixture_flattens_to_the_map_the_build_was_verified_against`
re-asserts on every run so the two constants cannot drift apart in the file.

**Mutation results.** Every guard here was re-attacked out of tree, one mutation
at a time in a fresh copy of `src`, with the unmutated copy passing first as the
control:

* drop `and overall` from `passed` → :class:`BuildVerificationGateTests`
* `passed = overall` alone, ignoring the hook's own symbols →
  `test_a_missing_symbol_fails_the_hook_even_when_the_report_passed_overall`
* let an empty `triples` pass → :class:`VacuousPassTests`
* `attribution` always `"sole"` → :class:`AttributionTests`
* drop the `FIELD_TARGET` branch from `hook_symbol_map` →
  :class:`SymbolMapAgreementTests` (the action-bar hook stores a listener in a
  stock field and never calls into DFInsta, so it loses its only symbol)
* stop seeding an empty entry per resolved hook → :class:`SymbolLessHookTests`

:class:`SymbolLessHookTests` records a defect this suite found in the producer an
hour after it was written and which is now fixed, plus the one half of it that
stays open: a grafted DEX whose only hook contributes no symbol is asserted to
differ from stock and nothing more.
"""

from __future__ import annotations

import json
import unittest
from typing import Any, Iterable, Mapping, Sequence

from dfinsta_pipeline.driver import (
    hook_symbol_map,
    host_hook_map,
    static_verified_claims,
)
from dfinsta_pipeline.evidence import (
    ALLOWED_PRODUCERS,
    POST_BUILD,
    PRE_APPLY,
    EvidenceClaim,
    EvidenceError,
    EvidenceKind,
    EvidenceLedger,
    Producer,
    Subject,
    Verdict,
)
from dfinsta_pipeline.hook_manifest import Hook, HostFingerprint
from tests.test_driver import (
    ACTION_BAR_HOOK,
    CONTEXT_HOOK,
    ENDPOINT_HOOK,
    SHELL,
    DriverCase,
)
from tests.test_evidence import claim_for

# --------------------------------------------------------------------- the 440 build

#: `hook_symbol_map` over the real 440 clean build. Seven hooks, three DEX files,
#: and two kinds of sharing: three Reels hooks in one class share the endpoint
#: call, and the two settings hooks in two different classes share the
#: `SettingsWrapper` construction. Every `Lcom/dfinstagram/probe; h_<hook_id>`
#: is unique to its hook by construction — that is what the probe symbol is for.
SYMBOLS_440: dict[str, list[list[str]]] = {
    "install_settings_long_click": [
        ["classes6.dex", "Lcom/dfinstagram/SettingsWrapper;", "<init>"],
        ["classes6.dex", "Lcom/dfinstagram/probe;", "h_install_settings_long_click"],
    ],
    "install_settings_long_click_actionbar": [
        ["classes6.dex", "Lcom/dfinstagram/SettingsWrapper;", "<init>"],
        [
            "classes6.dex",
            "Lcom/dfinstagram/probe;",
            "h_install_settings_long_click_actionbar",
        ],
    ],
    "replace_reels_discover_endpoint": [
        ["classes3.dex", "Lcom/dfinstagram/hooks;", "replaceReelsEndpoint"],
        ["classes3.dex", "Lcom/dfinstagram/probe;", "h_replace_reels_discover_endpoint"],
    ],
    "replace_reels_homecoming_endpoint": [
        ["classes3.dex", "Lcom/dfinstagram/hooks;", "replaceReelsEndpoint"],
        [
            "classes3.dex",
            "Lcom/dfinstagram/probe;",
            "h_replace_reels_homecoming_endpoint",
        ],
    ],
    "replace_reels_stream_endpoint": [
        ["classes3.dex", "Lcom/dfinstagram/hooks;", "replaceReelsEndpoint"],
        ["classes3.dex", "Lcom/dfinstagram/probe;", "h_replace_reels_stream_endpoint"],
    ],
    "set_app_context": [
        ["classes3.dex", "Lcom/dfinstagram/probe;", "h_set_app_context"],
        ["classes3.dex", "Lcom/dfinstagram/startapp;", "setContext"],
    ],
    "tigon_url_block": [
        ["classes.dex", "Lcom/dfinstagram/hooks;", "throwIfBlocked"],
        ["classes.dex", "Lcom/dfinstagram/probe;", "h_tigon_url_block"],
    ],
}

#: `host_hooks` out of `work/440-clean/dfinsta.verification.json`, verbatim. The
#: verifier writes `{dex: {"<descriptor> <method>": bool}}` because a DEX stores
#: a method reference as three separate indices — only the type descriptor and
#: the bare method name exist as literal strings to search for.
HOST_HOOKS_440: dict[str, dict[str, bool]] = {
    "classes.dex": {
        "Lcom/dfinstagram/hooks; throwIfBlocked": True,
        "Lcom/dfinstagram/probe; h_tigon_url_block": True,
    },
    "classes3.dex": {
        "Lcom/dfinstagram/hooks; replaceReelsEndpoint": True,
        "Lcom/dfinstagram/probe; h_replace_reels_discover_endpoint": True,
        "Lcom/dfinstagram/probe; h_replace_reels_homecoming_endpoint": True,
        "Lcom/dfinstagram/probe; h_replace_reels_stream_endpoint": True,
        "Lcom/dfinstagram/probe; h_set_app_context": True,
        "Lcom/dfinstagram/startapp; setContext": True,
    },
    "classes6.dex": {
        "Lcom/dfinstagram/SettingsWrapper; <init>": True,
        "Lcom/dfinstagram/probe; h_install_settings_long_click": True,
        "Lcom/dfinstagram/probe; h_install_settings_long_click_actionbar": True,
    },
}

#: One symbol of one hook, for the "missing" cases. Chosen as a probe symbol
#: because it belongs to exactly one hook, so the blast radius of removing it is
#: the thing under test.
STREAM_PROBE = (
    "classes3.dex",
    "Lcom/dfinstagram/probe;",
    "h_replace_reels_stream_endpoint",
)


def report_440(
    *, passed: bool = True, absent: Iterable[tuple[str, str, str]] = ()
) -> dict[str, Any]:
    """The 440 verifier report, with the named symbols flipped to absent.

    Only the two fields `static_verified_claims` reads are modelled; the real
    report carries eighteen more, and asserting on a copy of all of them would
    make this file fail whenever an unrelated check is added to the verifier.
    """
    host_hooks = {dex: dict(pairs) for dex, pairs in HOST_HOOKS_440.items()}
    for dex, descriptor, method in absent:
        assert f"{descriptor} {method}" in host_hooks[dex], "no such symbol in the fixture"
        host_hooks[dex][f"{descriptor} {method}"] = False
    return {"schema_version": 1, "passed": passed, "host_hooks": host_hooks}


# A hook whose payload never mentions DFInsta: it resolves, it patches a real
# class, and it contributes no symbol either map can assert on. Used for the
# empty-together invariant and for `KnownGapTests`.
NO_SYMBOL_HOOK = Hook(
    hook_id="probe_hook_that_asserts_nothing",
    intent="a payload with no DFInsta reference at all",
    tier="robust",
    strategy="none",
    semantic_deps=(),
    hosts=(HostFingerprint("named", descriptor=SHELL, note="present in the fixture"),),
    anchor=("invoke-super {<app:reg>}, Landroid/app/Application;->onCreate()V",),
    payload=("    invoke-static {}, Landroid/os/StrictMode;->enableDefaults()V",),
    marker="Landroid/os/StrictMode;->enableDefaults()V",
    expected_marker_count=1,
)

# Names a class this version does not have, so it escalates rather than resolving.
MISSING_SYMBOL_HOOK = Hook(
    hook_id="probe_hook_whose_host_is_gone",
    intent="a hook whose named host moved",
    tier="robust",
    strategy="none",
    semantic_deps=(),
    hosts=(HostFingerprint("named", descriptor="LX/0Gone;", note="not in this version"),),
    anchor=("nop",),
    payload=("    invoke-static {}, Lcom/dfinstagram/hooks;->vanished()V",),
    marker="Lcom/dfinstagram/hooks;->vanished()V",
    expected_marker_count=1,
)


def by_hook(claims: Sequence[EvidenceClaim]) -> dict[str, EvidenceClaim]:
    return {claim.hook_id: claim for claim in claims}


def flatten(symbols: Mapping[str, list[list[str]]]) -> dict[str, list[list[str]]]:
    """`hook_symbol_map`'s output in `host_hook_map`'s shape.

    Deliberately written the long way rather than by calling anything in
    `driver`: a helper that shared code with either map could not detect the two
    drifting apart.
    """
    out: dict[str, set[tuple[str, str]]] = {}
    for triples in symbols.values():
        for dex, descriptor, method in triples:
            out.setdefault(dex, set()).add((descriptor, method))
    return {dex: sorted([list(pair) for pair in pairs]) for dex, pairs in out.items()}


# --------------------------------------------------------------- per-hook verdicts


class StaticVerifiedClaimTests(unittest.TestCase):
    """One claim per hook, and each one's verdict is about that hook's own symbols."""

    def test_a_hook_whose_every_symbol_is_present_passes(self):
        """The base case: a clean build makes a passing claim for every hook.

        If this were not true the change would have replaced an unsatisfiable
        requirement with an unsatisfiable claim, and the post-build gate would
        still refuse every release for a reason no hook could fix.
        """
        claims = by_hook(static_verified_claims(SYMBOLS_440, report_440()))

        self.assertEqual(sorted(claims), sorted(SYMBOLS_440))
        for hook_id, claim in claims.items():
            with self.subTest(hook=hook_id):
                self.assertIs(claim.verdict, Verdict.PASSED)
                self.assertIn("present in the built DEX", claim.summary)
                self.assertTrue(all(claim.detail["symbols"].values()))

    def test_a_missing_symbol_fails_that_hook_and_names_it_in_the_summary(self):
        """A human reading an escalation must learn WHICH symbol was absent.

        `verify_build` already knows a symbol is missing; what it cannot say is
        whose it was, because its report is unioned per DEX. A claim that failed
        without naming the symbol would send the reader back to the same union
        this map exists to split.
        """
        claims = by_hook(
            static_verified_claims(
                SYMBOLS_440, report_440(passed=False, absent=[STREAM_PROBE])
            )
        )

        failed = claims["replace_reels_stream_endpoint"]
        self.assertIs(failed.verdict, Verdict.FAILED)
        self.assertIn(
            "classes3.dex Lcom/dfinstagram/probe; h_replace_reels_stream_endpoint",
            failed.summary,
        )
        self.assertIn("missing", failed.summary)

    def test_the_two_sibling_reels_hooks_are_not_failed_by_it(self):
        """Positive control for the test above: the failure is hook-scoped.

        All three Reels hooks live in `classes3.dex` and share the endpoint call,
        so a claim derived from the per-DEX union would fail all three the moment
        one probe symbol went missing — which is the false attribution the
        per-hook map was added to prevent. They are `failed` here only because
        the build's own verdict is false; their SYMBOLS are all present, and the
        summary says exactly that.
        """
        claims = by_hook(
            static_verified_claims(
                SYMBOLS_440, report_440(passed=False, absent=[STREAM_PROBE])
            )
        )

        for hook_id in (
            "replace_reels_discover_endpoint",
            "replace_reels_homecoming_endpoint",
        ):
            with self.subTest(hook=hook_id):
                claim = claims[hook_id]
                self.assertTrue(all(claim.detail["symbols"].values()))
                self.assertNotIn("missing", claim.summary)
                self.assertIn("symbols present", claim.summary)

    def test_a_missing_symbol_fails_the_hook_even_when_the_report_passed_overall(self):
        """Mutation: `passed = overall`, dropping the hook's own symbol check.

        The report shape here is one `verify_build` cannot emit — its own
        `passed` folds in `all(all(v.values()) for v in host_hooks.values())`, so
        an absent symbol always drags the build's verdict down with it. It is
        constructed anyway because it is the only way to prove the two conditions
        are independent: a per-hook claim that merely copies one global flag
        would be seven identical claims wearing seven hook ids, and every hook in
        a build would pass or fail together forever.
        """
        claims = by_hook(
            static_verified_claims(SYMBOLS_440, report_440(absent=[STREAM_PROBE]))
        )

        self.assertIs(claims["replace_reels_stream_endpoint"].verdict, Verdict.FAILED)
        self.assertIs(claims["replace_reels_discover_endpoint"].verdict, Verdict.PASSED)

    def test_the_detail_records_every_symbol_checked_and_its_state(self):
        """The claim carries the evidence, not just the conclusion.

        A gate that can only see `failed` has to re-run the verifier to learn
        anything; the ledger is meant to be the durable record of what was
        looked at, and a summary string is not a structure anything can query.
        """
        claims = by_hook(
            static_verified_claims(
                SYMBOLS_440, report_440(passed=False, absent=[STREAM_PROBE])
            )
        )

        self.assertEqual(
            claims["replace_reels_stream_endpoint"].detail["symbols"],
            {
                "classes3.dex Lcom/dfinstagram/hooks; replaceReelsEndpoint": True,
                "classes3.dex Lcom/dfinstagram/probe; h_replace_reels_stream_endpoint": (
                    False
                ),
            },
        )

    def test_a_symbol_the_report_never_mentions_is_absent_and_not_ignored(self):
        """A build whose report has no entry for a DEX proves nothing about it.

        `dict.get(dex, {}).get(name)` returns None for a DEX the verifier was
        never asked about, and None must read as "not found" rather than be
        skipped. Treating an unasked question as an answer is the same vacuous
        pass this module refuses everywhere else — and it is reachable for real:
        `host_hooks` is derived per run, so a run that derived it wrongly writes
        a report with a whole DEX missing.
        """
        stripped = report_440(passed=False)
        del stripped["host_hooks"]["classes.dex"]

        claim = by_hook(static_verified_claims(SYMBOLS_440, stripped))["tigon_url_block"]
        self.assertIs(claim.verdict, Verdict.FAILED)
        self.assertEqual(sorted(claim.detail["symbols"].values()), [False, False])
        self.assertIn("Lcom/dfinstagram/hooks; throwIfBlocked", claim.summary)

    def test_a_report_with_no_host_hooks_at_all_fails_every_hook(self):
        """The whole-report version of the same rule.

        `verification.get("host_hooks") or {}` is what makes this safe, and an
        empty map is what a caller that forgot to pass `--host-hooks` produces.
        `verify_build` refuses that input outright; a reader of its report gets no
        such refusal and must decide for itself, so it decides "unproven".
        """
        claims = static_verified_claims(SYMBOLS_440, {"passed": True})

        self.assertEqual(len(claims), len(SYMBOLS_440))
        self.assertEqual({claim.verdict for claim in claims}, {Verdict.FAILED})

    def test_claims_are_ordered_by_hook_id(self):
        """The claims are appended to a JSONL that runs get diffed against.

        Iteration order of the symbol map follows resolution order, which follows
        the manifest, so an unsorted output makes two runs of the same version
        produce ledgers that differ by line order alone.
        """
        shuffled = dict(reversed(list(SYMBOLS_440.items())))
        self.assertNotEqual(list(shuffled), sorted(SYMBOLS_440))  # the input is unsorted

        claims = static_verified_claims(shuffled, report_440())
        self.assertEqual([claim.hook_id for claim in claims], sorted(SYMBOLS_440))


# ------------------------------------------------------- the build's own verdict


class BuildVerificationGateTests(unittest.TestCase):
    """`passed: false` in the report fails every hook, whatever its symbols say."""

    def test_a_failed_build_fails_every_hook_though_all_symbols_are_present(self):
        """Mutation: drop `and overall` from the `passed` computation.

        A build can carry every DFInsta symbol and still be unshippable — a
        preserved entry that no longer matches stock, an added file, a duplicate
        ZIP entry, a grafted DEX identical to the one it replaced. Each of those
        makes the APK wrong while leaving every hook's bytes exactly where this
        check looks for them. Seven `passed` claims beside a failed build is the
        pipeline reporting success over a build it already rejected.
        """
        control = static_verified_claims(SYMBOLS_440, report_440(passed=True))
        self.assertEqual({claim.verdict for claim in control}, {Verdict.PASSED})

        claims = static_verified_claims(SYMBOLS_440, report_440(passed=False))
        self.assertEqual({claim.verdict for claim in claims}, {Verdict.FAILED})
        for claim in claims:
            with self.subTest(hook=claim.hook_id):
                self.assertTrue(all(claim.detail["symbols"].values()))
                self.assertIs(claim.detail["build_verification_passed"], False)

    def test_the_summary_says_the_build_failed_rather_than_naming_a_symbol(self):
        """The reason given must be the reason, or the reader chases the wrong bug.

        Every symbol IS present. A summary reading "missing …" would send someone
        looking for an injection that never went in, and the actual defect — an
        added file, a mismatched preserved entry — is somewhere else entirely and
        would be found by nobody.
        """
        claims = by_hook(static_verified_claims(SYMBOLS_440, report_440(passed=False)))

        summary = claims["tigon_url_block"].summary
        self.assertIn("symbols present, but the build verification failed", summary)
        self.assertNotIn("missing", summary)

    def test_a_failed_build_with_a_missing_symbol_still_names_the_symbol(self):
        """The build-failed wording must not swallow the more specific finding.

        This is the ordinary real case — an absent symbol forces `verify_build`'s
        own `passed` to false — so if the global message won here, the specific
        one would never be printed in production at all.
        """
        claims = by_hook(
            static_verified_claims(
                SYMBOLS_440, report_440(passed=False, absent=[STREAM_PROBE])
            )
        )

        summary = claims["replace_reels_stream_endpoint"].summary
        self.assertIn("missing", summary)
        self.assertIn("h_replace_reels_stream_endpoint", summary)

    def test_a_report_with_no_passed_field_is_not_a_pass(self):
        """`verification.get("passed") is True`, not a truthiness test.

        A report from an older or a partial verifier has no verdict to read, and
        "the question was never asked" must not resolve to "the answer was yes".
        The certificate pin in this project had exactly that bug once: an empty
        value was falsy and silently disabled the check.
        """
        for verdict_field in ({}, {"passed": "true"}, {"passed": 1}, {"passed": None}):
            with self.subTest(report=verdict_field):
                claims = static_verified_claims(
                    SYMBOLS_440, {"host_hooks": HOST_HOOKS_440, **verdict_field}
                )
                self.assertEqual({claim.verdict for claim in claims}, {Verdict.FAILED})


# -------------------------------------------------------------- the vacuous pass


class VacuousPassTests(unittest.TestCase):
    """A hook that contributed nothing to prove has not been proven."""

    def test_a_hook_with_no_symbols_is_failed_and_not_skipped(self):
        """Mutation: let an empty `triples` pass.

        `all([])` is True, so every "are this hook's symbols present?" check
        answers yes for a hook with no symbols. That is the exact vacuous pass
        `verify_build` refuses globally — it raises rather than accept an empty
        `host_hooks`, because with nothing to prove it would certify an unpatched
        graft. Skipping the hook instead would be no better: it leaves the ledger
        silent, and silence is what the post-build gate treats as `not_exercised`
        rather than as a measured negative.
        """
        symbols = {"tigon_url_block": SYMBOLS_440["tigon_url_block"], "empty_hook": []}
        claims = by_hook(static_verified_claims(symbols, report_440()))

        self.assertIn("empty_hook", claims)  # not skipped
        self.assertIs(claims["empty_hook"].verdict, Verdict.FAILED)
        self.assertEqual(claims["empty_hook"].detail["symbols"], {})
        self.assertIn("contributed no DFInsta symbol", claims["empty_hook"].summary)
        # The control: the same call, the same passing report, a hook that did
        # contribute. Without it "everything failed" would satisfy the assertion.
        self.assertIs(claims["tigon_url_block"].verdict, Verdict.PASSED)

    def test_a_hook_with_no_symbols_is_attributed_to_nothing(self):
        """`none`, never `shared`.

        `all(...)` over no symbols is True, so the sole/shared test alone would
        call it `sole` — "proven, and by this hook alone", which is the strongest
        thing this claim can say, asserted about a hook nothing was checked for.
        """
        claims = by_hook(static_verified_claims({"empty_hook": []}, report_440()))

        self.assertEqual(claims["empty_hook"].detail["attribution"], "none")

    def test_an_empty_symbol_map_produces_no_claims_at_all(self):
        """Pinned so the caller's guard stays load-bearing rather than decorative.

        The build stage refuses to build at all when `host_hook_map` is empty
        ("the verifier would pass vacuously"), and
        :meth:`SymbolMapAgreementTests.test_the_two_maps_are_empty_together` pins
        that the two maps empty together. That is what stands between this empty
        list and a run reporting `static_verified: 0/0` as though it had checked
        something.
        """
        self.assertEqual(static_verified_claims({}, report_440()), [])


# ------------------------------------------------------------------ attribution


class AttributionTests(unittest.TestCase):
    """Whose proof is it? Three states, measured across the whole build."""

    def test_a_hook_whose_every_symbol_is_its_own_is_sole(self):
        """`tigon_url_block` on 440: nothing else calls `throwIfBlocked`.

        `sole` is the claim's strongest form and the only one a reader may take
        as "this hook, specifically, is in the APK". It has to be earned against
        the whole build rather than asserted per hook, which is why the count is
        taken over every hook's symbols before any claim is made.
        """
        claims = by_hook(static_verified_claims(SYMBOLS_440, report_440()))

        self.assertEqual(claims["tigon_url_block"].detail["attribution"], "sole")

    def test_the_three_reels_hooks_share_their_endpoint_call_and_say_so(self):
        """Mutation: `attribution` always `"sole"`.

        All three route through `Lcom/dfinstagram/hooks; replaceReelsEndpoint`, so
        that symbol's presence proves at least one of them shipped and not which.
        Calling that `sole` would let a reader conclude a specific Reels hook was
        statically proven from a byte search that cannot tell them apart — and
        two Reels hooks on 439 were already measured never executing, which is
        exactly the mistake this field exists to keep visible.
        """
        claims = by_hook(static_verified_claims(SYMBOLS_440, report_440()))

        shared = [
            hook_id
            for hook_id, claim in claims.items()
            if claim.detail["attribution"] == "shared"
        ]
        self.assertEqual(
            sorted(shared),
            [
                "install_settings_long_click",
                "install_settings_long_click_actionbar",
                "replace_reels_discover_endpoint",
                "replace_reels_homecoming_endpoint",
                "replace_reels_stream_endpoint",
            ],
        )

    def test_the_full_440_attribution(self):
        """All three states in one real build, so the field is not two-valued.

        The two settings hooks share `Lcom/dfinstagram/SettingsWrapper; <init>`
        the same way the Reels hooks share the endpoint call — they are the 430
        and the modern action bar, a deliberate pair, so their sharing is
        permanent and not an artifact of this version.
        """
        symbols = dict(SYMBOLS_440, contributed_nothing=[])
        attribution = {
            claim.hook_id: claim.detail["attribution"]
            for claim in static_verified_claims(symbols, report_440())
        }

        self.assertEqual(
            attribution,
            {
                "contributed_nothing": "none",
                "install_settings_long_click": "shared",
                "install_settings_long_click_actionbar": "shared",
                "replace_reels_discover_endpoint": "shared",
                "replace_reels_homecoming_endpoint": "shared",
                "replace_reels_stream_endpoint": "shared",
                "set_app_context": "sole",
                "tigon_url_block": "sole",
            },
        )

    def test_the_probe_symbol_is_unique_to_its_hook(self):
        """`Lcom/dfinstagram/probe; h_<hook_id>` is the one symbol that cannot collide.

        It is named after the hook, so a probe-instrumented build attributes
        every hook individually and a bare one cannot — which is a fact about the
        build and not a defect in the reader. Pinned because the sharing above is
        only meaningful if something in the build is genuinely unshared.
        """
        owners: dict[tuple[str, str, str], set[str]] = {}
        for hook_id, triples in SYMBOLS_440.items():
            for triple in triples:
                owners.setdefault(tuple(triple), set()).add(hook_id)

        probes = {
            symbol: hooks for symbol, hooks in owners.items() if "probe;" in symbol[1]
        }
        self.assertEqual(len(probes), len(SYMBOLS_440))
        for symbol, hooks in probes.items():
            with self.subTest(symbol=symbol):
                self.assertEqual(hooks, {symbol[2].removeprefix("h_")})

    def test_attribution_is_recorded_beside_the_verdict_and_does_not_set_it(self):
        """A shared symbol is still a present symbol.

        Downgrading a `shared` hook to `failed` would fail five of 440's seven
        hooks on a perfect build; treating attribution as a second verdict is how
        a useful nuance turns into an unsatisfiable gate. The nuance belongs to
        whoever reads the claim.
        """
        claims = by_hook(static_verified_claims(SYMBOLS_440, report_440()))

        for hook_id in ("replace_reels_stream_endpoint", "install_settings_long_click"):
            with self.subTest(hook=hook_id):
                self.assertEqual(claims[hook_id].detail["attribution"], "shared")
                self.assertIs(claims[hook_id].verdict, Verdict.PASSED)


# ------------------------------------------------------ the two views of one fact


class SymbolMapAgreementTests(DriverCase):
    """`hook_symbol_map` flattened per DEX must be `host_hook_map`, exactly.

    Both read the same payloads through the same two regexes in two near-identical
    loops. One is written to `host-hooks.json` and handed to the verifier; the
    other becomes evidence about what that verifier proved. If they disagree the
    ledger describes a build that was never checked, and nothing else in the tree
    would notice.
    """

    def test_the_real_440_fixture_flattens_to_the_map_the_build_was_verified_against(
        self,
    ):
        """The two inlined constants at the top of this file describe one build.

        `SYMBOLS_440` was produced by `hook_symbol_map` from that run's
        resolution and index; `HOST_HOOKS_440` is the `host_hooks` section of the
        report the run's verifier wrote, whose keys come from the `host-hooks.json`
        the same run derived with `host_hook_map`. An edit to one and not the
        other would leave every other test in this file passing against a build
        that never existed.
        """
        self.assertEqual(
            flatten(SYMBOLS_440),
            {
                dex: sorted(name.split(" ", 1) for name in pairs)
                for dex, pairs in HOST_HOOKS_440.items()
            },
        )

    def test_the_two_maps_agree_over_three_hooks_in_three_dex_files(self):
        """The ordinary case, derived rather than asserted from a constant."""
        fixture = self.three_dex_fixture()
        hooks = [CONTEXT_HOOK, ACTION_BAR_HOOK, ENDPOINT_HOOK]
        report = self.resolve(hooks, fixture)

        self.assertEqual(
            flatten(hook_symbol_map(report, fixture.index, hooks)),
            host_hook_map(report, fixture.index, hooks),
        )

    def test_a_field_only_payload_reaches_both_maps(self):
        """Mutation: drop the `FIELD_TARGET` branch from `hook_symbol_map`.

        The action-bar hook calls nothing at all — it builds the mod's listener
        and stores it in a stock field, so its only DFInsta reference is a
        `new-instance`. A per-hook map derived from invocations alone gives it an
        empty symbol list, which the vacuous-pass rule then turns into a `failed`
        claim on a build where the hook is present and correct. The per-DEX map
        already handles this; the two must not part company over it.
        """
        fixture = self.three_dex_fixture()
        report = self.resolve([ACTION_BAR_HOOK], fixture)
        payload = report.resolutions[0].resolution.payload
        self.assertNotIn("invoke", "\n".join(payload))

        symbols = hook_symbol_map(report, fixture.index, [ACTION_BAR_HOOK])
        self.assertEqual(
            symbols,
            {
                ACTION_BAR_HOOK.hook_id: [
                    ["classes3.dex", "Lcom/dfinstagram/SettingsWrapper;", "<init>"]
                ]
            },
        )
        self.assertEqual(flatten(symbols), host_hook_map(report, fixture.index, [ACTION_BAR_HOOK]))

    def test_the_two_maps_agree_when_two_hooks_share_one_dex(self):
        """The union is where the per-DEX view loses information.

        Two hosts in one tree collapse into a single `host_hook_map` entry, and
        this is the flattening that has to reproduce it: not a relabelling of the
        same dict, but a genuine merge of two hooks' symbols.
        """
        fixture = self.shared_dex_fixture()
        hooks = [ENDPOINT_HOOK, ACTION_BAR_HOOK]
        report = self.resolve(hooks, fixture)

        per_hook = hook_symbol_map(report, fixture.index, hooks)
        self.assertEqual(sorted(per_hook), sorted(hook.hook_id for hook in hooks))
        self.assertEqual(list(host_hook_map(report, fixture.index, hooks)), ["classes10.dex"])
        self.assertEqual(flatten(per_hook), host_hook_map(report, fixture.index, hooks))

    def test_the_two_maps_agree_for_an_already_applied_hook(self):
        """Both cover the re-run path, which reads the manifest payload instead.

        An already-applied hook has no rendered payload, so both maps fall back
        to the template — and `host_dex_entries` grafts its DEX either way. A
        per-hook map that skipped it would leave the hook with no claim at all on
        every re-run, which is the state this whole change was made to end.
        """
        fixture = self.rerun_fixture()
        hooks = [CONTEXT_HOOK, ACTION_BAR_HOOK]
        report = self.resolve(hooks, fixture)
        outcomes = {item.hook_id: item.outcome.value for item in report.resolutions}
        self.assertEqual(outcomes[CONTEXT_HOOK.hook_id], "already_applied")

        per_hook = hook_symbol_map(report, fixture.index, hooks)
        self.assertIn(CONTEXT_HOOK.hook_id, per_hook)
        self.assertEqual(flatten(per_hook), host_hook_map(report, fixture.index, hooks))

    def test_neither_map_carries_a_hook_that_did_not_resolve(self):
        """An escalating hook proves nothing and must claim nothing.

        The run stops on the escalation anyway; what this pins is that both maps
        read outcomes rather than the manifest's length, so a hook whose host
        moved cannot arrive at the verifier as an assertion about a DEX nobody
        patched.
        """
        fixture = self.three_dex_fixture()
        hooks = [CONTEXT_HOOK, MISSING_SYMBOL_HOOK]
        report = self.resolve([CONTEXT_HOOK], fixture)

        per_hook = hook_symbol_map(report, fixture.index, hooks)
        self.assertEqual(sorted(per_hook), [CONTEXT_HOOK.hook_id])
        self.assertEqual(flatten(per_hook), host_hook_map(report, fixture.index, hooks))

    def test_the_two_maps_diverge_only_where_there_is_nothing_to_assert(self):
        """The one case where the maps are NOT two views of the same fact.

        A hook whose payload names nothing of DFInsta's contributes no assertion
        the verifier could make, so `host_hook_map` -- whose entries become
        `--host-hooks` -- must not name its DEX. But the per-hook map records it
        with an empty list, deliberately, so `static_verified_claims` can say
        `failed` ("there was nothing to look for") instead of the hook vanishing
        and the gate reporting `not_exercised` ("nobody looked").

        The upstream refusal still fires from the per-DEX side: `port` raises
        "no host hook could be derived; the verifier would pass vacuously" when
        `host_hook_map` is empty, which is what stops a whole run being certified
        on nothing.
        """
        fixture = self.three_dex_fixture()
        report = self.resolve([NO_SYMBOL_HOOK], fixture)
        self.assertEqual(report.resolutions[0].outcome.value, "resolved")

        self.assertEqual(host_hook_map(report, fixture.index, [NO_SYMBOL_HOOK]), {})
        self.assertEqual(
            hook_symbol_map(report, fixture.index, [NO_SYMBOL_HOOK]),
            {NO_SYMBOL_HOOK.hook_id: []},
        )


# ------------------------------------------------------------- claim well-formedness


class ClaimShapeTests(unittest.TestCase):
    """The claims must be evidence the ledger will actually take."""

    def test_every_claim_is_a_deterministic_static_verified_claim(self):
        """Kind and producer are what make the claim count for anything.

        `STATIC_VERIFIED` is the kind the requirement sets name; `DETERMINISTIC`
        is what makes it independent of the proposer. A claim of some other kind
        would be recorded happily, satisfy nothing, and leave the gate refusing
        with no visible cause.
        """
        for claim in static_verified_claims(SYMBOLS_440, report_440()):
            with self.subTest(hook=claim.hook_id):
                self.assertIs(claim.kind, EvidenceKind.STATIC_VERIFIED)
                self.assertIs(claim.producer, Producer.DETERMINISTIC)
                self.assertEqual(claim.actor, "tools/verify/verify_build.py")

    def test_static_verified_permits_the_deterministic_producer_and_not_the_device(self):
        """The schema, checked directly, and the refusal it implies.

        `ALLOWED_PRODUCERS` is what enforces "produced by something other than the
        proposer" at record time rather than by reviewer attention. The negative
        half is the control: if every producer were allowed, the positive half
        would pass while the taxonomy meant nothing.
        """
        self.assertIn(
            Producer.DETERMINISTIC, ALLOWED_PRODUCERS[EvidenceKind.STATIC_VERIFIED]
        )
        self.assertNotIn(Producer.DEVICE, ALLOWED_PRODUCERS[EvidenceKind.STATIC_VERIFIED])
        with self.assertRaises(EvidenceError):
            EvidenceClaim(
                hook_id="tigon_url_block",
                kind=EvidenceKind.STATIC_VERIFIED,
                verdict=Verdict.PASSED,
                producer=Producer.DEVICE,
                actor="device:R58N1234567",
                summary="the phone cannot see a DEX symbol",
            )

    def test_the_ledger_records_them_for_registered_subjects(self):
        """The claims go through `record`, which is where every rule is enforced.

        Constructing a claim proves nothing about whether the ledger will take
        it: `record` checks registration and self-attestation, and the driver
        calls it in a stretch of the build stage with no `try` around it, so a
        rejected claim raises out of a run whose APK is already built and
        verified.
        """
        ledger = EvidenceLedger()
        for hook_id in SYMBOLS_440:
            ledger.register(Subject(hook_id, "mechanical"))

        for claim in static_verified_claims(SYMBOLS_440, report_440()):
            ledger.record(claim)

        self.assertEqual(len(ledger.claims), len(SYMBOLS_440))
        for hook_id in SYMBOLS_440:
            with self.subTest(hook=hook_id):
                recorded = ledger.claims_for(hook_id, EvidenceKind.STATIC_VERIFIED)
                self.assertEqual(len(recorded), 1)
                self.assertIs(recorded[0].verdict, Verdict.PASSED)

    def test_an_unregistered_hook_is_refused(self):
        """Positive control for the test above: `record` really does check.

        Every hook in a real run is registered by `record_resolution_evidence`
        before the build, so this cannot happen today — which is exactly why the
        acceptance above would otherwise be indistinguishable from a `record`
        that accepts anything.
        """
        ledger = EvidenceLedger()
        claim = static_verified_claims(SYMBOLS_440, report_440())[0]

        with self.assertRaises(EvidenceError) as caught:
            ledger.record(claim)
        self.assertIn("not registered", str(caught.exception))

    def test_the_claims_survive_a_round_trip_through_the_jsonl(self):
        """The detail is a dict of primitives, and the ledger is a file.

        `EvidenceLedger.load` re-validates every claim it reads. A detail holding
        anything `json` cannot round-trip would be written and then reject the
        whole ledger on the next run, which is the sort of failure that only
        appears on the SECOND port of a version.
        """
        claims = static_verified_claims(
            SYMBOLS_440, report_440(passed=False, absent=[STREAM_PROBE])
        )

        for claim in claims:
            with self.subTest(hook=claim.hook_id):
                restored = EvidenceClaim.from_dict(json.loads(json.dumps(claim.to_dict())))
                self.assertEqual(restored, claim)


# ------------------------------------------------------------------- the gap closed


class PostBuildGapTests(unittest.TestCase):
    """The reason the producer was written, stated as a test.

    Before it, `static_verified` was required of every hook by all three
    requirement sets and emitted by nothing, so `report("post_build")` escalated
    every hook of every version no matter what the build proved. Nothing else in
    the suite pins this: the requirement sets have tests, the ledger has tests,
    and the fact that the two could never meet had none.
    """

    HOOK = "tigon_url_block"
    PROPOSER = "agent:resolver-1"

    def ledger_with_everything_else(self) -> EvidenceLedger:
        """An agent-resolved hook holding six of the seven required kinds.

        Agent provenance on purpose: it is the only one that requires all four
        pre-apply kinds, so `static_verified` is demonstrably the single thing
        missing rather than the only one this fixture happened to skip.
        """
        ledger = EvidenceLedger()
        ledger.register(
            Subject(
                self.HOOK,
                "agent",
                descriptor="Lcom/instagram/api/tigon/TigonServiceLayer;",
                proposed_by=self.PROPOSER,
            )
        )
        for kind in EvidenceKind:
            if kind is not EvidenceKind.STATIC_VERIFIED:
                ledger.record(claim_for(self.HOOK, kind))
        return ledger

    def test_the_hook_is_ready_pre_apply_and_escalated_post_build(self):
        """The starting state: everything derivable before the build is present.

        Without this the test below could pass because the fixture was broken in
        some other way, and "escalated for missing static_verified" would be an
        assertion about a hook that was failing for four reasons.
        """
        ledger = self.ledger_with_everything_else()

        self.assertIs(ledger.report(PRE_APPLY)["complete"], True)
        report = ledger.report(POST_BUILD)
        self.assertIs(report["complete"], False)
        self.assertEqual([item["hook_id"] for item in report["escalations"]], [self.HOOK])

    def test_the_only_unmet_item_is_static_verified(self):
        """Named exactly, because "escalated" alone does not identify the gap.

        The other two post-build kinds come from the device and are produced
        elsewhere; this hook has both. What it cannot have is the one kind whose
        producer did not exist.
        """
        readiness = self.ledger_with_everything_else().readiness(self.HOOK, POST_BUILD)

        self.assertEqual([kind.value for kind in readiness.missing], ["static_verified"])
        self.assertEqual(len(readiness.reasons), 1)
        self.assertIn("static_verified: no claim recorded", readiness.reasons[0])

    def test_recording_the_produced_claim_clears_the_escalation(self):
        """The whole point of the change, end to end.

        The claim is not hand-written here: it comes out of
        `static_verified_claims` over the real 440 symbols and a real-shaped
        report, so what clears the gate is the thing the build stage actually
        records — not a claim shaped the way this test would like it to be.
        """
        ledger = self.ledger_with_everything_else()
        claim = by_hook(static_verified_claims(SYMBOLS_440, report_440()))[self.HOOK]

        ledger.record(claim)

        post_build = ledger.report(POST_BUILD)
        self.assertIs(post_build["complete"], True)
        self.assertEqual(post_build["escalations"], [])
        # And the release phase, which is the gate that matters: all seven kinds.
        self.assertIs(ledger.report()["complete"], True)

    def test_a_failed_static_claim_does_not_clear_it(self):
        """Recording something is not the same as proving something.

        A producer that emitted a claim regardless of what the verifier found
        would close the gap on paper and reopen every hole it was opened for.
        The failed claim here is the one a build with an absent symbol really
        produces.
        """
        ledger = self.ledger_with_everything_else()
        claim = by_hook(
            static_verified_claims(
                SYMBOLS_440, report_440(passed=False, absent=[STREAM_PROBE])
            )
        )[self.HOOK]
        self.assertIs(claim.verdict, Verdict.FAILED)

        ledger.record(claim)

        readiness = ledger.readiness(self.HOOK, POST_BUILD)
        self.assertIs(readiness.ready, False)
        self.assertEqual(readiness.missing, ())  # measured, not absent
        self.assertIn("static_verified: failed", " ".join(readiness.reasons))

    def test_the_producer_is_not_the_proposer(self):
        """The ledger's central rule, checked against this specific producer.

        `verify_build.py` re-derives its findings from the built APK and has no
        idea which agent proposed anything, but the rule is enforced by comparing
        strings — so the actor the driver passes has to be one no proposer can
        ever be named.
        """
        ledger = self.ledger_with_everything_else()
        claim = by_hook(static_verified_claims(SYMBOLS_440, report_440()))[self.HOOK]

        self.assertNotEqual(claim.actor, self.PROPOSER)
        ledger.record(claim)  # would raise if it were the proposer

    def test_all_seven_440_hooks_clear_together_on_a_clean_build(self):
        """One hook clearing is a mechanism; seven clearing is the release.

        7 of 7 escalated on every version before this existed. A run of the real
        440 build's symbols against the real 440 report has to take all seven the
        other way, or the change closed the gap for a shape no port produces.
        """
        ledger = EvidenceLedger()
        for hook_id in SYMBOLS_440:
            ledger.register(Subject(hook_id, "mechanical"))
            for kind in (EvidenceKind.ANCHOR_UNIQUE, EvidenceKind.REGISTERS_SAFE,
                         EvidenceKind.RUNTIME_PROBE, EvidenceKind.DIFFERENTIAL):
                ledger.record(claim_for(hook_id, kind))

        self.assertEqual(len(ledger.report()["escalations"]), 7)
        for claim in static_verified_claims(SYMBOLS_440, report_440()):
            ledger.record(claim)
        self.assertEqual(ledger.report()["escalations"], [])


# ------------------------------------------------------------------- the wiring


class BuildStageWiringTests(DriverCase):
    """The build stage must actually join the verifier's report to the ledger.

    Every other class here tests the two functions directly. If nothing called
    them the gap would be closed in a library and open in every run, which is
    indistinguishable from the state before the change for anyone reading a
    ledger. `run_command` is stubbed by `DriverCase`, so the verifier report is
    written by hand where the real build would have left it.
    """

    #: What `verify_build` would report for the three-DEX fixture: the symbols
    #: `host_hook_map` derives from those three payloads, written out literally
    #: rather than derived, so a change in either map shows up here as a failure.
    VERIFICATION = {
        "schema_version": 1,
        "passed": True,
        "host_hooks": {
            "classes.dex": {"Lcom/dfinstagram/startapp; setContext": True},
            "classes3.dex": {"Lcom/dfinstagram/SettingsWrapper; <init>": True},
            "classes10.dex": {
                "Lcom/dfinstagram/adv_settings; noteEndpoint": True,
                "Lcom/dfinstagram/hooks; replaceEndpoint": True,
            },
        },
    }

    def write_verification(self, report: Mapping[str, Any]) -> None:
        """Leave a verifier report where `build.py` would have written one."""
        out = self.base / "run"
        out.mkdir(parents=True, exist_ok=True)
        (out / "dfinsta.verification.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )

    def static_claims_recorded(self) -> dict[str, dict[str, Any]]:
        """The `static_verified` rows of the run's evidence JSONL, by hook."""
        lines = (self.base / "run" / "evidence.jsonl").read_text(encoding="utf-8")
        rows = [json.loads(line) for line in lines.splitlines() if line.strip()]
        return {
            row["hook_id"]: row for row in rows if row["kind"] == "static_verified"
        }

    def test_the_build_stage_records_one_claim_per_hook(self):
        """A real run through `port`, ending with the claims on disk.

        This is the join that did not exist: `build.py` wrote the report,
        `verify_build.py` computed it, and nothing carried it into the ledger.
        """
        fixture = self.three_dex_fixture()
        hooks = [CONTEXT_HOOK, ACTION_BAR_HOOK, ENDPOINT_HOOK]
        self.write_verification(self.VERIFICATION)

        result = self.run_port(fixture, hooks)

        self.assertIs(result.ok, True, result.stopped_because)
        self.assertEqual(result.artifacts["static_verified"], "3/3")
        recorded = self.static_claims_recorded()
        self.assertEqual(sorted(recorded), sorted(hook.hook_id for hook in hooks))
        for hook_id, row in recorded.items():
            with self.subTest(hook=hook_id):
                self.assertEqual(row["verdict"], "passed")
                self.assertEqual(row["producer"], "deterministic")

    def test_the_post_build_escalation_survives_a_static_pass(self):
        """A build with static evidence is still not release-ready.

        The device kinds are missing and no amount of static proof supplies
        them — three inert patches passed everything up to here. A run that
        stopped warning once `static_verified` landed would be the exact
        regression this evidence was added to make impossible.
        """
        fixture = self.three_dex_fixture()
        self.write_verification(self.VERIFICATION)

        self.run_port(fixture, [CONTEXT_HOOK, ACTION_BAR_HOOK, ENDPOINT_HOOK])

        self.assertIn("This APK is not release-ready", self.printed)
        self.assertIn("[build] static_verified: 3 of 3 hook(s)", self.printed)

    def test_a_failed_report_records_failed_claims_rather_than_none(self):
        """The unhappy path still produces evidence, and it says `failed`.

        Skipping the claims when the report is bad would leave the ledger reading
        `not_exercised` — "nobody looked" — for a build that was looked at and
        found wanting. Those are different findings and a gate treats them
        differently.
        """
        fixture = self.three_dex_fixture()
        self.write_verification({**self.VERIFICATION, "passed": False})

        result = self.run_port(fixture, [CONTEXT_HOOK, ACTION_BAR_HOOK, ENDPOINT_HOOK])

        self.assertEqual(result.artifacts["static_verified"], "0/3")
        recorded = self.static_claims_recorded()
        self.assertEqual(len(recorded), 3)
        self.assertEqual({row["verdict"] for row in recorded.values()}, {"failed"})

    def test_an_unreadable_report_costs_the_evidence_and_not_the_run(self):
        """Positive control, and the documented decision.

        By the time this runs the APK is built and `build.py` has exited zero on
        its own verification, so a report that cannot be read is a bookkeeping
        problem. Turning it into a failed port would be the tail wagging the dog
        — but it must be visible, and no claim may be invented to cover it.
        """
        fixture = self.three_dex_fixture()
        self.write_verification(self.VERIFICATION)
        (self.base / "run" / "dfinsta.verification.json").write_text(
            "{ not json", encoding="utf-8"
        )

        result = self.run_port(fixture, [CONTEXT_HOOK, ACTION_BAR_HOOK, ENDPOINT_HOOK])

        self.assertIs(result.ok, True, result.stopped_because)
        self.assertNotIn("static_verified", result.artifacts)
        self.assertIn("[build] no static_verified evidence", self.printed)
        self.assertEqual(self.static_claims_recorded(), {})

    def test_a_missing_report_is_the_same_non_fatal_case(self):
        """The commoner shape of the above: the file was never written at all."""
        fixture = self.three_dex_fixture()

        result = self.run_port(fixture, [CONTEXT_HOOK, ACTION_BAR_HOOK, ENDPOINT_HOOK])

        self.assertIs(result.ok, True, result.stopped_because)
        self.assertNotIn("static_verified", result.artifacts)
        self.assertIn("[build] no static_verified evidence", self.printed)


# ------------------------------------------------------------------- known gaps


class SymbolLessHookTests(DriverCase):
    """A hook whose payload names nothing of DFInsta's.

    Found by this suite as a live defect and fixed the same hour: `hook_symbol_map`
    used to populate its map only where a symbol matched, so such a hook was
    simply ABSENT — no claim, and a gate reporting `not_exercised` ("nobody
    looked") where the truth is `failed` ("there was nothing to look for"). It
    now seeds an empty entry for every resolved hook before looking for anything.

    No hook in today's manifest triggers it; all seven contribute at least one
    symbol, which is exactly why it had to be pinned rather than watched for.
    """

    def test_a_hook_that_contributes_no_symbol_is_failed_rather_than_skipped(self):
        """The vacuous-pass branch must be reachable from its only production caller.

        `static_verified_claims`'s docstring says a hook with no symbols is
        `failed`, not skipped, and :class:`VacuousPassTests` shows it is — when
        the empty list is passed in. That is worth nothing unless the map that
        feeds it can actually produce an empty list.
        """
        fixture = self.three_dex_fixture()
        hooks = [ENDPOINT_HOOK, NO_SYMBOL_HOOK]
        report = self.resolve(hooks, fixture)
        outcomes = {item.hook_id: item.outcome.value for item in report.resolutions}
        self.assertEqual(outcomes[NO_SYMBOL_HOOK.hook_id], "resolved")

        symbols = hook_symbol_map(report, fixture.index, hooks)
        self.assertEqual(symbols[NO_SYMBOL_HOOK.hook_id], [])

        claims = by_hook(static_verified_claims(symbols, report_440()))
        claim = claims[NO_SYMBOL_HOOK.hook_id]
        self.assertIs(claim.verdict, Verdict.FAILED)
        self.assertEqual(claim.detail["attribution"], "none")
        self.assertIn("no DFInsta symbol", claim.summary)

        # And the claim is what clears "nobody looked" — a failed claim is still
        # an answer, and readiness must show the kind as answered-and-failed
        # rather than as never exercised.
        ledger = EvidenceLedger()
        ledger.register(Subject(NO_SYMBOL_HOOK.hook_id, "mechanical"))
        self.assertIn(
            EvidenceKind.STATIC_VERIFIED,
            ledger.readiness(NO_SYMBOL_HOOK.hook_id, POST_BUILD).missing,
        )
        ledger.record(claim)
        self.assertNotIn(
            EvidenceKind.STATIC_VERIFIED,
            ledger.readiness(NO_SYMBOL_HOOK.hook_id, POST_BUILD).missing,
        )

    def test_the_denominator_counts_every_resolved_hook(self):
        """The visible symptom, and why a reporting defect is worth fixing.

        `artifacts["static_verified"]` and the `[build]` line are both
        `f"{passed}/{len(static_claims)}"`. While `static_claims` had one entry
        per hook that CONTRIBUTED a symbol rather than one per hook in the run, a
        two-hook port printed "static_verified: 1 of 1 hook(s)" — full coverage
        of a run where one hook had no static evidence at all. The escalation was
        printed underneath, so nothing was ever certified wrongly; but a count is
        what gets read, and that one said the opposite of what happened.

        STILL OPEN, and a different question: `host_hook_map` omits `classes.dex`
        here, so `host_dex_entries` grafts it and `--host-hooks` asks the verifier
        to prove nothing about its contents — which is what `host_hook_map`'s own
        docstring forbids ("a DEX that is replaced in the output with nothing
        asserted about its contents is the vacuous pass the verifier refuses
        globally, reintroduced one DEX at a time"). Seeding an empty entry THERE
        is not the fix: `verify_build` would be handed an assertion it cannot
        check. What that DEX does still get is `grafted_dex_changed`, which
        `verify_build` requires of every replaced entry — so it is proven to
        differ from stock, just not proven to carry anything in particular.
        """
        fixture = self.three_dex_fixture()
        hooks = [ENDPOINT_HOOK, NO_SYMBOL_HOOK]
        out = self.base / "run"
        out.mkdir(parents=True, exist_ok=True)
        (out / "dfinsta.verification.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "passed": True,
                    "host_hooks": {
                        "classes10.dex": {
                            "Lcom/dfinstagram/adv_settings; noteEndpoint": True,
                            "Lcom/dfinstagram/hooks; replaceEndpoint": True,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        result = self.run_port(fixture, hooks)

        self.assertIs(result.ok, True, result.stopped_because)
        self.assertEqual(result.artifacts["static_verified"], "1/2")
        self.assertIn("[build] static_verified: 1 of 2 hook(s)", self.printed)
        # Still true, and still the open half: the DEX carrying the symbol-less
        # hook is grafted with nothing asserted about its contents.
        self.assertIn("classes.dex", result.artifacts["replace_dex"])
        self.assertNotIn(
            "classes.dex", json.loads((out / "host-hooks.json").read_text(encoding="utf-8"))
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""Tests for stage 10's decision-memory half — one Resolve run, made durable.

The module under test exists because the pipeline measures the right thing and
then throws it away: `resolve.search_hosts` knows that three endpoint literals
appear in 4, 3 and 2 classes on their own and in exactly one together, and
`HookResolution.to_dict` does not serialise any of it. Recording that is how
agent invocations per port fall instead of staying flat.

Every test here is written from one of three rules rather than from the code's
shape.

    1. **The function is pure.** Two calls on one report produce byte-identical
       records, the timestamp arrives as an argument, and the module contains no
       clock. Anything else and a Temporal replay writes a line that does not
       match the one already on disk.

    2. **Absence is never a pass.** An escalation records nothing. A record that
       says "resolved" has to have been earned by an outcome that resolved.

    3. **The forbidden values are the point.** A store that returns a confident
       wrong answer is worse than one that returns nothing, and exactly four
       things make it do that: an obfuscated descriptor claimed as a fingerprint,
       a per-version register or member claimed as identity, a resource id
       recorded at all (103 of 11,737 survived 430->439), and an absolute path
       that names one machine. `ForbiddenValueTests` constructs the report that
       *would* leak each of them and asserts the leak does not happen — each with
       a positive control, because a search that cannot succeed always passes.

`MutationTests` adds no coverage. It re-attacks the same guards from the
direction a plausible rewrite would take, and each docstring says what would
reach the next port if that guard were dropped.
"""

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dfinsta_pipeline import manifest_update as manifest_update_module
from dfinsta_pipeline.contracts import canonical_json
from dfinsta_pipeline.decisions import (
    API_PATH_LITERAL,
    DEFAULT_MEMORY_PATH,
    FORBIDDEN_SIGNAL,
    STABLE_NAMED_TYPE,
    STRUCTURAL_SHAPE,
    Compatibility,
    DecisionError,
    DecisionMemory,
    RecalledDescriptor,
    Reverification,
    seed_records,
)
from dfinsta_pipeline.hook_manifest import Resolution as SiteResolution
from dfinsta_pipeline.manifest_update import (
    AGENT_PROPOSAL,
    REDACTED_RESOURCE_ID,
    is_stable_named_type,
    open_memory,
    redact,
    resolution_records,
    update_memory,
)
from dfinsta_pipeline.resolve import (
    CandidateReport,
    HookResolution,
    HostSearch,
    Outcome,
    ResolveReport,
)


# --------------------------------------------------------------------- fixture

VERSION = "439"
STAMP = "2026-08-02T09:00:00Z"

REELS = "replace_reels_discover_endpoint"
CONTEXT = "set_app_context"

# The Reels request builder: obfuscated, and its name exists in 430 too, naming
# something else entirely. The one descriptor no record may fingerprint.
HOST = "LX/04tC;"
PATH = "smali_classes5/X/04tC.smali"

# A stable named type — the case the whitelist is supposed to let through.
SHELL = "Lcom/instagram/app/InstagramAppShell;"
SHELL_PATH = "smali_classes3/com/instagram/app/InstagramAppShell.smali"

# `ResolveReport.decode` is `str(decode.resolve())`, so a real report always
# carries an absolute path. It is the leak that costs nothing to make.
DECODE = "/home/arnav/AI/dfinsta-redux/work/439-explore/stock-439"

LITERALS = ["clips/discover/", "clips/homecoming/", "clips/discover/stream/"]
LITERAL_EVIDENCE = {
    "literals": list(LITERALS),
    "classes_per_literal": {LITERALS[0]: 4, LITERALS[1]: 3, LITERALS[2]: 2},
    "co_located": 1,
}


def site(
    descriptor: str = HOST,
    *,
    hook_id: str = REELS,
    smali_path: str = PATH,
    bindings: dict | None = None,
    occurrences: int = 1,
    resolved: bool = True,
) -> SiteResolution:
    """A `hook_manifest.Resolution` shaped exactly as `resolve._classify` leaves it."""
    return SiteResolution(
        hook_id,
        resolved,
        descriptor=descriptor,
        smali_path=smali_path,
        anchor=['const-string v0, "clips/discover/"'],
        payload=["    # dfinsta_reels_discover"],
        bindings={"r": "v0"} if bindings is None else bindings,
        occurrences=occurrences,
    )


def hook_resolution(
    hook_id: str = REELS,
    outcome: Outcome = Outcome.RESOLVED,
    *,
    found_by: str = "by_literal",
    descriptor: str = HOST,
    evidence: dict | None = None,
    resolution: SiteResolution | None = None,
    searches: tuple | None = None,
    candidates: tuple | None = None,
) -> HookResolution:
    if evidence is None:
        evidence = (
            dict(LITERAL_EVIDENCE) if found_by == "by_literal" else {"descriptor": descriptor}
        )
    if searches is None:
        searches = (HostSearch(found_by, (descriptor,), evidence),)
    if candidates is None:
        candidates = (
            CandidateReport(descriptor, PATH, found_by, resolved=True, occurrences=1),
        )
    if resolution is None and outcome is Outcome.RESOLVED:
        resolution = site(descriptor, hook_id=hook_id)
    return HookResolution(
        hook_id,
        outcome,
        reason=f"{descriptor} matched the anchor exactly once",
        descriptor=descriptor,
        resolution=resolution,
        searches=searches,
        candidates=candidates,
    )


def report(*items: HookResolution) -> ResolveReport:
    return ResolveReport(
        decode=DECODE,
        index_decode=DECODE,
        index_content_hash="0" * 64,
        resolutions=items,
    )


def only(records) -> object:
    """The single record a one-hook report must produce."""
    assert len(records) == 1, f"expected exactly one record, got {len(records)}"
    return records[0]


def identity(record) -> str:
    """The fields a future port ranks or joins on: the signals and the technique.

    These two are what `Compatibility.evidence_fingerprint` is computed from and
    what `decisions.precedence` orders. Anything per-version in here is a claim
    that a fact about one decode is a fact about the next one.
    """
    return canonical_json({"signals": list(record.signals), "technique": record.technique})


def everything(record) -> str:
    """The whole stored record, descriptor included, as it lands on disk."""
    return canonical_json(record.to_dict())


def records_for(*items: HookResolution, version: str = VERSION, **extra):
    return resolution_records(report(*items), version, STAMP, **extra)


class ManifestUpdateTestCase(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        self.path = self.tmp / "manifest" / "decisions.jsonl"


# ---------------------------------------------------------------------- purity


class PurityTests(ManifestUpdateTestCase):
    """The record written must depend on the report and nothing else.

    Temporal replays this stage. A record that differed between the original run
    and the replay would append a second, contradictory answer under one
    (hook_id, version) — the state `DecisionMemory.conflicts_for` exists to
    report, manufactured by the pipeline itself.
    """

    def test_two_calls_on_one_report_are_byte_identical(self):
        one = records_for(hook_resolution())
        two = records_for(hook_resolution())
        self.assertEqual(
            [everything(record) for record in one],
            [everything(record) for record in two],
        )

    def test_the_timestamp_is_the_callers_and_nothing_else_moves(self):
        first = only(records_for(hook_resolution()))
        second = only(resolution_records(report(hook_resolution()), VERSION, "1999-01-01"))
        self.assertEqual(first.recorded_at, STAMP)
        self.assertEqual(second.recorded_at, "1999-01-01")
        # Everything but the stamp is identical, so the stamp is the only thing
        # the caller's clock can influence.
        self.assertEqual(
            {**first.to_dict(), "recorded_at": ""},
            {**second.to_dict(), "recorded_at": ""},
        )

    def test_the_module_contains_no_clock_and_no_environment(self):
        """`stamped` exists so that this stage cannot stamp itself.

        A module that called `datetime.now()` would serialise a different line on
        every replay, and no assertion about the file could ever be written.
        """
        source = inspect.getsource(manifest_update_module)
        for forbidden in ("datetime", "time.time", "monotonic", "os.environ", "getenv"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_the_pure_function_takes_no_path_and_touches_no_file(self):
        """Purity asserted by construction, then again by removing the filesystem.

        The signature is the real argument — there is no path to pass, so there
        is no reading-the-manifest path to accidentally take — and the patched
        builtins prove no helper reaches around it.
        """
        parameters = inspect.signature(resolution_records).parameters
        self.assertEqual(
            tuple(parameters), ("report", "version", "recorded_at", "compatibility")
        )
        self.assertNotIn("path", parameters)

        def refuse(*args, **kwargs):
            raise AssertionError("the pure function touched the filesystem")

        with mock.patch("builtins.open", refuse), mock.patch.object(
            Path, "read_text", refuse
        ), mock.patch.object(Path, "exists", refuse):
            self.assertEqual(len(records_for(hook_resolution())), 1)

    def test_a_report_with_several_hooks_keeps_the_reports_order(self):
        # Two workers writing the same run must produce the same file, so the
        # order cannot come from a set or a dict of hook ids.
        records = records_for(
            hook_resolution(CONTEXT, found_by="named", descriptor=SHELL),
            hook_resolution(REELS),
        )
        self.assertEqual([record.hook_id for record in records], [CONTEXT, REELS])


# ------------------------------------------------------------- what is written


class RecordContentTests(ManifestUpdateTestCase):
    """The four facts stage 10 exists to keep, each in the field it belongs in."""

    def test_a_by_literal_hook_records_the_literal_fingerprint_and_the_anchor(self):
        record = only(records_for(hook_resolution()))
        self.assertEqual(record.signals, (API_PATH_LITERAL, STRUCTURAL_SHAPE))
        self.assertEqual(record.hook_id, REELS)
        self.assertEqual(record.version, VERSION)
        self.assertEqual(record.smali_path, PATH)
        for literal in LITERALS:
            with self.subTest(literal=literal):
                self.assertIn(literal, record.technique)

    def test_the_measured_selectivity_is_recorded_with_its_numbers(self):
        """The evidence that co-location, not the literal, picked the host.

        Without it the next port cannot tell a fingerprint that discriminates
        from one that happened to have a single candidate this time — and the
        Reels anchor matches cleanly in three classes, so guessing is a patch on
        an analytics map.
        """
        record = only(records_for(hook_resolution()))
        finding = record.chain[0].finding
        self.assertIn("'clips/discover/' in 4", finding)
        self.assertIn("'clips/homecoming/' in 3", finding)
        self.assertIn("'clips/discover/stream/' in 2", finding)
        self.assertIn("together in 1", finding)
        self.assertIn("would have left 4 candidate(s)", finding)

    def test_the_capture_bindings_are_recorded_in_the_version_stamped_chain(self):
        # Deliberately supplied out of order. `resolve_in_source` fills the
        # bindings dict in anchor-match order, so a record that iterated it
        # as-is would depend on how the pattern happened to be written.
        record = only(
            records_for(hook_resolution(resolution=site(bindings={"fld": "A08", "app": "v4"})))
        )
        finding = record.chain[1].finding
        self.assertIn("app=v4", finding)
        self.assertIn("fld=A08", finding)
        self.assertLess(finding.index("app=v4"), finding.index("fld=A08"))

    def test_the_fingerprint_recorded_is_the_one_that_proposed_the_winner(self):
        """A hook may declare several host fingerprints; only one found the host.

        Reading the kind off the first search instead of off the winning
        candidate would credit a fingerprint that proposed a class this run
        rejected, and the next port would trust it.
        """
        record = only(
            records_for(
                hook_resolution(
                    searches=(
                        HostSearch("named", (SHELL,), {"descriptor": SHELL}),
                        HostSearch("by_literal", (HOST,), dict(LITERAL_EVIDENCE)),
                    ),
                    candidates=(
                        CandidateReport(SHELL, SHELL_PATH, "named", reason="anchor missed"),
                        CandidateReport(HOST, PATH, "by_literal", resolved=True),
                    ),
                )
            )
        )
        self.assertEqual(record.signals, (API_PATH_LITERAL, STRUCTURAL_SHAPE))
        self.assertNotIn(SHELL, identity(record))
        self.assertIn("class counts in this version", record.chain[0].finding)

    def test_the_per_version_path_is_recorded_and_the_route_calls_it_then(self):
        # `Route.smali_path_then` is the existing framing for a version-scoped
        # observation, and it is the framing every per-version fact here inherits.
        record = only(records_for(hook_resolution()))
        self.assertEqual(record.route.smali_path_then, PATH)
        self.assertEqual([step.file for step in record.chain], [PATH, PATH])

    def test_a_named_host_records_the_stable_type_signal(self):
        record = only(
            records_for(
                hook_resolution(
                    CONTEXT,
                    found_by="named",
                    descriptor=SHELL,
                    resolution=site(SHELL, hook_id=CONTEXT, smali_path=SHELL_PATH),
                    candidates=(CandidateReport(SHELL, SHELL_PATH, "named", resolved=True),),
                )
            )
        )
        self.assertEqual(record.signals, (STABLE_NAMED_TYPE, STRUCTURAL_SHAPE))
        self.assertIn(SHELL, record.technique)

    def test_an_agent_found_host_says_so_because_that_is_the_number_being_driven_down(self):
        """`by_agent` is not a fingerprint; it is the cost of the port.

        A record that quietly labelled an agent's answer as a mechanical one
        would make the pipeline look like it was learning while the agent count
        stayed flat.
        """
        record = only(
            records_for(hook_resolution(found_by="by_agent", evidence={"proposed": [HOST]}))
        )
        self.assertEqual(record.signals, (AGENT_PROPOSAL, STRUCTURAL_SHAPE))
        self.assertIn("agent invocation per port", record.technique)

    def test_the_host_is_stored_only_behind_a_recalled_descriptor(self):
        record = only(records_for(hook_resolution()))
        self.assertIsInstance(record.host, RecalledDescriptor)
        self.assertEqual(
            record.host.reverify(
                Reverification(
                    target_version="440",
                    target_decode="work/440-explore/stock-440",
                    acknowledged_by="tests",
                )
            ),
            HOST,
        )
        # And exactly once in the file: any second copy is one that escaped the
        # wrapper, and a wrapper with a copy beside it protects nothing.
        self.assertEqual(everything(record).count(HOST), 1)

    def test_the_evidence_fingerprint_is_derived_and_the_rest_is_the_callers(self):
        stated = Compatibility(
            semantic_feature_identity="reels_endpoint.discover",
            delivery_mechanism="request_path_rewrite",
            policy_revision="r1",
        )
        record = only(records_for(hook_resolution(), compatibility=stated))
        self.assertEqual(record.compatibility.semantic_feature_identity, "reels_endpoint.discover")
        self.assertEqual(record.compatibility.delivery_mechanism, "request_path_rewrite")
        self.assertEqual(record.compatibility.policy_revision, "r1")
        self.assertTrue(record.compatibility.complete)
        self.assertEqual(len(record.compatibility.evidence_fingerprint), 64)

    def test_a_caller_that_states_nothing_gets_a_record_that_cannot_be_reused(self):
        """The honest outcome, not an error. `decisions` says so explicitly.

        A record whose staleness nobody can assess is still worth having — the
        technique survives — it just may never be replayed as an answer.
        """
        record = only(records_for(hook_resolution()))
        self.assertFalse(record.compatibility.complete)
        self.assertEqual(
            record.compatibility.unknown,
            ("semantic_feature_identity", "delivery_mechanism", "policy_revision"),
        )


# ----------------------------------------------------------- the forbidden set


class ForbiddenValueTests(ManifestUpdateTestCase):
    """Each attack builds the report that would leak, and asserts it does not.

    Every one carries a positive control. An assertion that some string is
    absent passes trivially when the string was never reachable, so each test
    also shows the same field carrying the value it is allowed to carry.
    """

    def test_an_obfuscated_descriptor_is_never_claimed_as_a_fingerprint(self):
        """Attack: a `named` fingerprint pointing at `LX/0DnT;`.

        The manifest can be written that way and the index will resolve it, so
        nothing upstream refuses. If the record then claimed STABLE_NAMED_TYPE,
        the next port would rank "look the name up" first, find the name present
        in 439 on an unrelated class, and patch it. That patch assembles, passes
        every static assertion, and does nothing — the 430 settings hook again.
        """
        obfuscated = only(
            records_for(hook_resolution(found_by="named", descriptor=HOST))
        )
        self.assertNotIn(STABLE_NAMED_TYPE, obfuscated.signals)
        self.assertNotIn(FORBIDDEN_SIGNAL, obfuscated.signals)
        self.assertEqual(obfuscated.signals, (STRUCTURAL_SHAPE,))
        self.assertNotIn(HOST, identity(obfuscated))
        self.assertIn("names a different class", obfuscated.technique)
        # Positive control: the same code path with a name a human wrote does
        # claim the signal and does name the type, so the assertions above are
        # about the descriptor and not about the branch never running.
        stable = only(
            records_for(
                hook_resolution(
                    CONTEXT,
                    found_by="named",
                    descriptor=SHELL,
                    resolution=site(SHELL, hook_id=CONTEXT, smali_path=SHELL_PATH),
                    candidates=(CandidateReport(SHELL, SHELL_PATH, "named", resolved=True),),
                )
            )
        )
        self.assertIn(STABLE_NAMED_TYPE, stable.signals)
        self.assertIn(SHELL, identity(stable))

    def test_a_descriptor_bound_by_the_anchor_never_reaches_an_identity_field(self):
        """Attack: the type capture on `tigon_url_block`, which really did move.

        430 bound `LX/05ez;` and 439 binds `LX/03AS;` for the same hook. Either
        one recorded as identity is a join key that returns the wrong class.
        """
        record = only(
            records_for(hook_resolution(resolution=site(bindings={"cls": "LX/03AS;"})))
        )
        self.assertNotIn("LX/03AS;", identity(record))
        # Positive control: it IS recorded, in the version-stamped chain, where
        # a per-version observation belongs. Dropping it silently would be a
        # different bug wearing this test's pass.
        self.assertIn("cls=LX/03AS;", record.chain[1].finding)

    def test_a_register_never_reaches_an_identity_field(self):
        """Attack: `set_app_context`, whose entire 430->439 delta is v0 -> v4.

        A register in the technique changes the evidence fingerprint every port,
        so the reuse predicate refuses every stored record forever: a memory that
        never helps, which is the mirror of a memory that lies.
        """
        record = only(
            records_for(hook_resolution(resolution=site(bindings={"app": "p4", "r": "v0"})))
        )
        self.assertNotIn("p4", identity(record))
        self.assertNotIn("v0", identity(record))
        self.assertIn("app=p4", record.chain[1].finding)  # positive control
        self.assertIn("r=v0", record.chain[1].finding)

    def test_an_obfuscated_member_name_never_reaches_an_identity_field(self):
        record = only(
            records_for(hook_resolution(resolution=site(bindings={"fld": "Ac0"})))
        )
        self.assertNotIn("Ac0", identity(record))
        self.assertIn("fld=Ac0", record.chain[1].finding)  # positive control

    def test_a_resource_id_is_withheld_from_the_whole_record(self):
        """Attack: an `<id:any>` capture that binds `0x7f0812ab`.

        Of 11,737 drawable names present in both 430 and 439, 103 kept their id.
        A recorded id is a fact with a 99.1% chance of being false by the next
        port, and unlike a register it is not even useful as an observation:
        the name is the handle, the id is re-resolved from the target index.
        """
        record = only(
            records_for(
                hook_resolution(
                    resolution=site(bindings={"id": "0x7f0812ab", "app": "v4"})
                )
            )
        )
        stored = everything(record)
        self.assertNotIn("0x7f0812ab", stored)
        self.assertIn(REDACTED_RESOURCE_ID, stored)
        # Positive control: the redaction removed the id, not the bindings. A
        # helper that returned "" would pass the assertion above and lose the
        # register that made the record worth writing.
        self.assertIn("app=v4", record.chain[1].finding)

    def test_no_emitted_record_contains_an_absolute_path(self):
        """Attack: the decode root, which every real report carries.

        `resolve_manifest` stores `str(decode.resolve())`, so the absolute path
        is one attribute away at all times. `decisions.Step` refuses one in a
        chain; nothing refuses one in `Resolution.smali_path`, so this module
        does.
        """
        source = report(hook_resolution())
        self.assertIn(DECODE, source.decode)  # positive control: it was reachable
        record = only(resolution_records(source, VERSION, STAMP))
        stored = everything(record)
        self.assertNotIn(DECODE, stored)
        self.assertNotIn('"/', stored.replace('":', ""))
        self.assertFalse(record.smali_path.startswith("/"))
        for step in record.chain:
            with self.subTest(step=step.action):
                self.assertFalse(step.file.startswith("/"))

    def test_an_absolute_smali_path_is_refused_rather_than_recorded(self):
        absolute = f"{DECODE}/{PATH}"
        with self.assertRaises(DecisionError) as caught:
            records_for(hook_resolution(resolution=site(smali_path=absolute)))
        message = str(caught.exception)
        self.assertIn(REELS, message)
        self.assertIn("is absolute", message)
        self.assertIn("one machine's workspace", message)

    def test_a_decode_path_passed_as_the_version_is_refused(self):
        """The other way the absolute path gets in: as the key.

        `update_memory(report, report.decode, ...)` is a plausible call — the
        report has exactly one version-shaped-looking field — and it would file
        every record under a key no other machine can ask for.
        """
        with self.assertRaises(DecisionError) as caught:
            records_for(hook_resolution(), version=DECODE)
        self.assertIn("looks like a path, not a version label", str(caught.exception))

    def test_nothing_is_written_into_the_field_the_proposer_is_shown(self):
        """`intent_constraints` is the blind half of the manifest.

        `Hook.constraints` describes the answer's shape and is deliberately
        withheld from a proposer; `intent_constraints` is what it may see. A
        finding written into the visible half would leak the answer into every
        later measurement of the proposer, and nothing would fail.
        """
        record = only(records_for(hook_resolution()))
        self.assertNotIn("intent_constraints", everything(record))
        self.assertNotIn("intent_constraints", inspect.getsource(manifest_update_module))

    def test_the_whitelist_of_stable_types_defaults_to_refusing(self):
        for descriptor in (
            "LX/0DnT;",
            "LX/05t2;",
            "La/b/C;",
            "LA08;",
            "Lcom/instagram/X;",
            "Lcom/instagram/app/A08;",
            "InstagramAppShell",
            "",
        ):
            with self.subTest(descriptor=descriptor):
                self.assertFalse(is_stable_named_type(descriptor))
        for descriptor in (
            SHELL,
            "Lcom/instagram/api/tigon/TigonServiceLayer;",
            "Lcom/instagram/profile/fragment/UserDetailFragment;",
        ):
            with self.subTest(descriptor=descriptor):
                self.assertTrue(is_stable_named_type(descriptor))

    def test_redact_leaves_a_mobileconfig_flag_alone(self):
        # 0x7f is the application package byte of a resource id. The settings
        # miss turns on `0x81099a000034a6`, which is not one and is worth keeping.
        self.assertEqual(redact("0x81099a000034a6"), "0x81099a000034a6")
        self.assertEqual(redact("v4"), "v4")
        self.assertEqual(redact("0x7f0812ab"), REDACTED_RESOURCE_ID)


# ------------------------------------------------------- what earns no record


class NoRecordTests(ManifestUpdateTestCase):
    """An outcome that did not resolve has learned nothing to hand on.

    Absence is never a pass anywhere else in this pipeline and it is not one
    here: a record for an escalated hook would file its reason string as an
    answer, and the next port would replay it.
    """

    def test_a_needs_agent_hook_produces_no_record(self):
        escalated = hook_resolution(
            "install_settings_long_click",
            Outcome.NEEDS_AGENT,
            found_by="by_agent",
            evidence={"proposed": []},
            resolution=None,
        )
        self.assertEqual(records_for(escalated), ())

    def test_every_escalating_outcome_produces_no_record(self):
        for outcome in Outcome:
            if outcome in (Outcome.RESOLVED, Outcome.ALREADY_APPLIED):
                continue
            with self.subTest(outcome=outcome.value):
                self.assertEqual(records_for(hook_resolution(outcome=outcome)), ())

    def test_already_applied_produces_no_record(self):
        """A re-run over a decode this pipeline patched has learned nothing new.

        `resolve._classify` builds no resolution for it, so there is no path and
        no binding to record, and the run that first applied the patch already
        wrote this record. A second would put two answers under one key.
        """
        applied = hook_resolution(outcome=Outcome.ALREADY_APPLIED, resolution=None)
        self.assertEqual(records_for(applied), ())

    def test_a_resolved_outcome_carrying_an_unresolved_resolution_produces_no_record(self):
        # Not a state `resolve` can reach; recording it would file the failure
        # reason as the answer.
        contradictory = hook_resolution(resolution=site(resolved=False))
        self.assertEqual(records_for(contradictory), ())

    def test_an_empty_report_produces_no_records(self):
        self.assertEqual(resolution_records(report(), VERSION, STAMP), ())

    def test_only_the_resolved_hooks_of_a_mixed_report_are_recorded(self):
        records = records_for(
            hook_resolution(REELS),
            hook_resolution("install_settings_long_click", Outcome.NEEDS_AGENT, resolution=None),
            hook_resolution(CONTEXT, outcome=Outcome.UNRESOLVED),
        )
        self.assertEqual([record.hook_id for record in records], [REELS])


# ------------------------------------------------------------------ the guards


class GuardTests(ManifestUpdateTestCase):
    """Malformed input is refused by name rather than recorded quietly."""

    def test_a_blank_version_is_refused(self):
        with self.assertRaises(DecisionError) as caught:
            records_for(hook_resolution(), version="  ")
        self.assertIn("half a key", str(caught.exception))

    def test_a_dict_shaped_report_is_refused_with_the_reason(self):
        with self.assertRaises(DecisionError) as caught:
            resolution_records(report(hook_resolution()).to_dict(), VERSION, STAMP)
        self.assertIn("never serialises them", str(caught.exception))

    def test_a_caller_supplied_evidence_fingerprint_is_refused(self):
        with self.assertRaises(DecisionError) as caught:
            records_for(
                hook_resolution(),
                compatibility=Compatibility(evidence_fingerprint="whatever-I-say"),
            )
        self.assertIn("derived here", str(caught.exception))

    def test_a_bare_mapping_of_compatibility_is_refused(self):
        with self.assertRaises(DecisionError) as caught:
            records_for(hook_resolution(), compatibility={"policy_revision": "r1"})
        self.assertIn("misspelled dimension", str(caught.exception))

    def test_a_report_whose_two_halves_name_different_classes_is_refused(self):
        mismatched = hook_resolution(descriptor=HOST, resolution=site("LX/05t2;"))
        with self.assertRaises(DecisionError) as caught:
            records_for(mismatched)
        self.assertIn("disagree", str(caught.exception))

    def test_a_resolution_with_no_path_is_refused(self):
        with self.assertRaises(DecisionError) as caught:
            records_for(hook_resolution(resolution=site(smali_path="")))
        self.assertIn("no smali path", str(caught.exception))


# ------------------------------------------------------------------ the writer


class WriterTests(ManifestUpdateTestCase):
    """The file is the memory, so what comes back must be what went in."""

    def test_records_round_trip_through_the_file(self):
        written = update_memory(report(hook_resolution()), VERSION, STAMP, path=self.path)
        reloaded = DecisionMemory.load(self.path)
        self.assertIn(written[0], reloaded.resolutions)
        recovered = reloaded.resolutions_for(REELS, VERSION)[0]
        self.assertEqual(recovered, written[0])
        self.assertEqual(recovered.chain, written[0].chain)
        self.assertEqual(recovered.signals, written[0].signals)
        self.assertEqual(recovered.compatibility, written[0].compatibility)

    def test_the_first_write_materialises_the_seed_before_appending(self):
        """The file must not look authoritative while omitting what already hurt.

        `decisions.main` warns "no decision memory" and falls back to the in-code
        seed. A first run that wrote only today's answers would silence that
        warning while losing the confirmed dead 430 settings hook, the shipped
        no-op substitution, and the measured survival rates.
        """
        self.assertFalse(self.path.exists())
        update_memory(report(hook_resolution()), VERSION, STAMP, path=self.path)
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), len(seed_records()) + 1)
        memory = DecisionMemory.load(self.path)
        self.assertIn("install_settings_long_click", memory.hooks)
        self.assertEqual(memory.precedence("430", "439")[0], "drawable_name")

    def test_a_second_run_appends_without_re_seeding(self):
        update_memory(report(hook_resolution()), VERSION, STAMP, path=self.path)
        first = len(self.path.read_text(encoding="utf-8").splitlines())
        update_memory(report(hook_resolution()), "440", STAMP, path=self.path)
        second = len(self.path.read_text(encoding="utf-8").splitlines())
        self.assertEqual(second, first + 1)
        memory = DecisionMemory.load(self.path)
        self.assertEqual(
            [record.version for record in memory.resolutions_for(REELS)], [VERSION, "440"]
        )

    def test_the_file_is_byte_identical_across_two_runs_of_one_report(self):
        one, two = self.tmp / "a" / "decisions.jsonl", self.tmp / "b" / "decisions.jsonl"
        update_memory(report(hook_resolution()), VERSION, STAMP, path=one)
        update_memory(report(hook_resolution()), VERSION, STAMP, path=two)
        self.assertEqual(one.read_bytes(), two.read_bytes())

    def test_each_record_occupies_exactly_one_line(self):
        update_memory(report(hook_resolution()), VERSION, STAMP, path=self.path)
        line = self.path.read_text(encoding="utf-8").splitlines()[-1]
        envelope = json.loads(line)
        self.assertEqual(envelope["kind"], "resolution")
        self.assertEqual(envelope["record"]["hook_id"], REELS)

    def test_the_parent_directory_is_created_on_demand(self):
        self.assertFalse(self.path.parent.exists())
        update_memory(report(hook_resolution()), VERSION, STAMP, path=self.path)
        self.assertTrue(self.path.exists())

    def test_an_incomplete_report_still_records_what_did_resolve(self):
        """The mechanical five have something to hand on whatever the ui two did.

        Gating the write on `report.complete` would mean the run in which a hook
        first became mechanical is the run that records nothing.
        """
        source = report(
            hook_resolution(REELS),
            hook_resolution("install_settings_long_click", Outcome.NEEDS_AGENT, resolution=None),
        )
        self.assertFalse(source.complete)
        written = update_memory(source, VERSION, STAMP, path=self.path)
        self.assertEqual([record.hook_id for record in written], [REELS])

    def test_nothing_is_written_when_the_report_earned_nothing(self):
        written = update_memory(report(), VERSION, STAMP, path=self.path)
        self.assertEqual(written, ())
        self.assertEqual(
            len(self.path.read_text(encoding="utf-8").splitlines()), len(seed_records())
        )

    def test_the_writer_defaults_to_the_committed_memory_path(self):
        # Asserted against the signature rather than by calling it: the default
        # is a real path in this repo and a test must not append to it.
        self.assertIs(
            inspect.signature(update_memory).parameters["path"].default, DEFAULT_MEMORY_PATH
        )
        self.assertIs(
            inspect.signature(open_memory).parameters["path"].default, DEFAULT_MEMORY_PATH
        )

    def test_an_unreadable_line_is_named_before_anything_is_appended(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{"schema_version": 1, "kind": "resolution"}\n', encoding="utf-8")
        with self.assertRaises(DecisionError) as caught:
            update_memory(report(hook_resolution()), VERSION, STAMP, path=self.path)
        self.assertIn(f"{self.path}:1", str(caught.exception))


# ------------------------------------------------------------------- mutations


class MutationTests(ManifestUpdateTestCase):
    """The same guards, re-attacked from the direction a rewrite would take.

    A positive test proves a guard exists. These construct the input the
    plausible mutation waves through, and assert the outcome that mutation could
    not produce.
    """

    def test_treating_every_named_host_as_stable_would_rank_a_recycled_name_first(self):
        """Mutation: drop the whitelist and trust `found_by == "named"`.

        `is_stable_named_type` is three regex checks; deleting it makes every
        named fingerprint claim STABLE_NAMED_TYPE, which `decisions.precedence`
        measured at 89.3% survival. The next port would put "look the descriptor
        up" at the top of its precedence list. The name is there in 439, on a
        different class, and the port would ship a patch on a stranger.
        """
        leaked = only(records_for(hook_resolution(found_by="named", descriptor=HOST)))
        self.assertNotIn(STABLE_NAMED_TYPE, leaked.signals)
        self.assertNotIn(HOST, leaked.technique)
        # The descriptor is still stored, exactly once, behind the wrapper: the
        # mutation to reject is over-claiming it, not forgetting it.
        self.assertEqual(everything(leaked).count(HOST), 1)

    def test_putting_the_measurements_in_the_technique_would_make_reuse_impossible(self):
        """Mutation: build the technique from the evidence instead of the manifest.

        It reads as an improvement — more detail in the reusable field — and it
        silently ends reuse: `Compatibility.evidence_fingerprint` hashes the
        technique, the class counts move every version, so no stored record is
        ever compatible with any later run again. A memory that never helps.
        """
        few = records_for(hook_resolution())
        many = records_for(
            hook_resolution(
                evidence={
                    "literals": list(LITERALS),
                    "classes_per_literal": {LITERALS[0]: 9, LITERALS[1]: 7, LITERALS[2]: 6},
                    "co_located": 2,
                }
            )
        )
        self.assertEqual(few[0].technique, many[0].technique)
        self.assertEqual(
            few[0].compatibility.evidence_fingerprint,
            many[0].compatibility.evidence_fingerprint,
        )
        # Positive control: the numbers did change, in the chain, where they belong.
        self.assertNotEqual(few[0].chain[0].finding, many[0].chain[0].finding)

    def test_recording_an_escalation_would_file_a_reason_string_as_an_answer(self):
        """Mutation: record every hook, not only the resolved ones.

        `NEEDS_AGENT` carries a descriptor — `resolve._classify` sets one for the
        `requires_proposal` case, from a candidate whose anchor matched but whose
        payload is a shape. Recording it would hand the next port a host that was
        explicitly refused as an answer this port.
        """
        refused = hook_resolution(
            "install_settings_long_click",
            Outcome.NEEDS_AGENT,
            descriptor=HOST,
            resolution=site(HOST, hook_id="install_settings_long_click"),
        )
        self.assertIsNotNone(refused.descriptor)  # the leak was available
        self.assertEqual(records_for(refused), ())

    def test_a_dedup_by_key_would_hide_memory_contradicting_itself(self):
        """Mutation: skip a record whose (hook_id, version) is already on file.

        It looks like hygiene. What it actually does is pick a winner between two
        answers for one key, silently and by arrival order — the exact tie
        `conflicts_for` refuses to break, because two answers means a human has
        to look.
        """
        update_memory(report(hook_resolution()), VERSION, STAMP, path=self.path)
        update_memory(
            report(hook_resolution(resolution=site(smali_path="smali_classes9/X/09zz.smali"))),
            VERSION,
            STAMP,
            path=self.path,
        )
        memory = DecisionMemory.load(self.path)
        self.assertEqual(len(memory.resolutions_for(REELS, VERSION)), 2)
        self.assertEqual(len(memory.conflicts_for(REELS, VERSION)), 2)

    def test_stamping_inside_the_module_would_break_the_replay_it_is_written_for(self):
        """Mutation: default `recorded_at` to the current time.

        Every replayed record would differ from the one on disk in exactly one
        field, so the file would grow a near-duplicate per replay and
        `conflicts_for` would report a contradiction the pipeline invented.
        """
        first = only(records_for(hook_resolution()))
        second = only(records_for(hook_resolution()))
        self.assertEqual(first.recorded_at, second.recorded_at)
        self.assertEqual(everything(first), everything(second))
        self.assertNotIn("now(", inspect.getsource(manifest_update_module))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

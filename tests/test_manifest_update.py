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

import contextlib
import inspect
import io
import json
import shutil
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

from dfinsta_pipeline import agent_cost as agent_cost_module
from dfinsta_pipeline.agent_cost import (
    AGENT_ROUTES,
    NEED_CAPTURE,
    NEED_HOST,
    NEED_PATCH,
    NEED_UNSPECIFIED,
    ROUTE_AGENT_PROPOSAL,
    ROUTE_AGENT_SUPPLIER,
    ROUTE_ALREADY_APPLIED,
    ROUTE_DETERMINISTIC_SUPPLIER,
    ROUTE_MECHANICAL,
    ROUTE_NOT_RESOLVED,
    SELECTED_EXPLICIT,
    SELECTED_LATEST,
    UNSTAMPED,
    VERDICT_FALLING,
    VERDICT_FLAT,
    VERDICT_RISING,
    VERDICT_UNTESTABLE,
    WITHHELD_DESCRIPTOR,
    WITHHELD_PATH,
    CostLedger,
    CostRun,
    HookCost,
    Selectivity,
    SupplierAttempt,
    cost_report,
    hook_costs,
    record_run,
    render,
    scrub,
    update_ledger,
)
from dfinsta_pipeline.capture_supply import (
    AGENT,
    STAGE_DRAWABLE_ABSENT,
    STAGE_NO_PROPOSAL,
    STAGE_PRECONDITION_TYPE_ABSENT,
    Supplied,
    SupplyOutcome,
    decline,
    run_supply_chain,
)
from dfinsta_pipeline.hook_manifest import (
    CaptureSupply,
    ManifestError,
    SuppliedCapture,
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
    supplies: tuple = (),
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
        supplies=supplies,
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


# =============================================================================
#  agent_cost: the measurement half of stage 10
# =============================================================================
#
# `pipeline_flowchart.md` says agent invocations per port "should fall with every
# version ported. A pipeline whose agent count is flat is not learning." The
# number was measured nowhere, so the claim could not be wrong. Everything below
# is written from what would have to be true for it to be *falsifiable*:
#
#   1. every hook produces a cost record, including the escalations — the
#      opposite of `resolution_records`, where an escalation must produce nothing;
#   2. a decline is recorded with its machine-readable stage, and a failure is
#      never recorded at all, because it is an exception and the run dies;
#   3. a narrowing selectivity margin is visible in the query output BEFORE it
#      reaches 1 -> 1 and then 0;
#   4. the same four forbidden values, attacked with the real strings the real
#      430 and 439 supplier evidence contains.

GUARD = "profile_action_bar_self_guard"
ACTIONBAR = "Lcom/instagram/profile/actionbar/ProfileActionBar;"
SETTINGS = "install_settings_long_click"

# The self-profile model type on 439. `LX/077N;` on 430 — same hook, different
# class, which is the whole reason a descriptor may not be stored here.
SELF_TYPE = "LX/0Dxw;"
DRAWABLE_ID = "0x7f082538"

SUPPLY = CaptureSupply(
    provides=(
        SuppliedCapture("model", "reg", "model_register"),
        SuppliedCapture("self_type", "type", "self_profile_type"),
    ),
    suppliers=(GUARD, AGENT),
    params=(("self_drawable", "instagram_menu_outline_24"), ("requires_type", ACTIONBAR)),
)

#: Verbatim from a real run of `capture_supply.profile_action_bar_self_guard`
#: against `work/439-explore/stock-439`. It contains a resource id and an
#: obfuscated descriptor, which is why the fixture is the real text.
GUARD_EVIDENCE = (
    f"precondition: {ACTIONBAR} is present",
    f"drawable instagram_menu_outline_24 resolves to {DRAWABLE_ID} in this version",
    "instance-of by register in the matched method: v1=11 type(s)",
    "dispatch register v1 tested against 11 subtypes",
    "v1 is bound by the anchor at this site",
    f"1 of 11 subtypes load instagram_menu_outline_24 ({DRAWABLE_ID}): {SELF_TYPE}",
)


def guard_answered(hits: int = 1, candidates: int = 11) -> Supplied:
    evidence = list(GUARD_EVIDENCE[:-1])
    evidence.append(
        f"{hits} of {candidates} subtypes load instagram_menu_outline_24 "
        f"({DRAWABLE_ID}): {SELF_TYPE}"
    )
    return Supplied(
        GUARD,
        {"model_register": "v1", "self_profile_type": SELF_TYPE},
        evidence=tuple(evidence),
    )


def guard_declined(stage: str = STAGE_PRECONDITION_TYPE_ABSENT) -> Supplied:
    return decline(
        GUARD,
        stage,
        f"{ACTIONBAR} does not exist in this version. This rule describes the "
        "ProfileActionBar design introduced around 430",
        GUARD_EVIDENCE[:1],
    )


def agent_answered() -> Supplied:
    return Supplied(
        AGENT,
        {"model_register": "v1", "self_profile_type": SELF_TYPE},
        evidence=("from a validated capture proposal",),
    )


def supplied(*attempts: Supplied, winner: str = "") -> SupplyOutcome:
    values = {"model": "v1", "self_type": SELF_TYPE} if winner else {}
    return SupplyOutcome(SUPPLY, values, tuple(attempts), winner)


def settings_hook(*supplies: SupplyOutcome, **kwargs) -> HookResolution:
    kwargs.setdefault("found_by", "by_agent")
    kwargs.setdefault("evidence", {"proposed": [HOST]})
    if kwargs.get("outcome", Outcome.RESOLVED) is Outcome.RESOLVED:
        kwargs.setdefault("resolution", site(HOST, hook_id=SETTINGS))
    return hook_resolution(SETTINGS, supplies=supplies, **kwargs)


def costs_for(*items: HookResolution, version: str = VERSION, stamp: str = STAMP):
    return hook_costs(report(*items), version, stamp)


def one_cost(*items: HookResolution, **kwargs) -> HookCost:
    costs = costs_for(*items, **kwargs)
    assert len(costs) == 1, f"expected one cost record, got {len(costs)}"
    return costs[0]


def stored(cost: HookCost) -> str:
    return canonical_json(cost.to_dict())


def ledger_of(*rows) -> CostLedger:
    """A ledger built from (version, *hook resolutions) rows, in port order."""
    ledger = CostLedger()
    for version, *items in rows:
        for cost in costs_for(*items, version=version):
            ledger.record(cost)
    return ledger


def runs_ledger(*rows) -> CostLedger:
    """A ledger built from (version, stamp, *hook resolutions) rows, in append order.

    Distinct stamps are what make two rows two runs, and that is not a convention
    invented here: `record_run` takes one caller-supplied `recorded_at` and stamps
    every record of one attempt with it, so the run identifier is already in the
    data and no schema change was needed to read it back.
    """
    ledger = CostLedger()
    for version, stamp, *items in rows:
        for cost in costs_for(*items, version=version, stamp=stamp):
            ledger.record(cost)
    return ledger


#: The committed ledger, which now holds genuine two-run data: one 439 port that
#: stopped at resolve and the re-run that finished. Copied before use, never read
#: in place — it is real recorded evidence and a test may not be why it changed.
REAL_LEDGER = Path(__file__).resolve().parent.parent / "manifest" / "agent_cost.jsonl"

#: The two 439 attempts on file, oldest first. Safe to pin: the ledger is
#: append-only, so runs 1 and 2 can never become different runs — a third port
#: appends run 3 and leaves these two exactly where they are.
REAL_RUN_ONE = "2026-08-02T16:12:40.616633+00:00"
REAL_RUN_TWO = "2026-08-02T16:42:05.463572+00:00"


class AgentCostTestCase(ManifestUpdateTestCase):
    pass


# ---------------------------------------------------------------------- purity


class CostPurityTests(AgentCostTestCase):
    """Same discipline as decision memory: Temporal replays this stage too."""

    def test_two_calls_on_one_report_are_byte_identical(self):
        first = costs_for(hook_resolution(), settings_hook(supplied(guard_answered(), winner=GUARD)))
        second = costs_for(hook_resolution(), settings_hook(supplied(guard_answered(), winner=GUARD)))
        self.assertEqual([stored(cost) for cost in first], [stored(cost) for cost in second])

    def test_the_timestamp_is_the_callers_and_nothing_else_moves(self):
        first = one_cost(hook_resolution())
        second = one_cost(hook_resolution(), stamp="1999-01-01")
        self.assertEqual(first.recorded_at, STAMP)
        self.assertEqual(second.recorded_at, "1999-01-01")
        self.assertEqual(
            {**first.to_dict(), "recorded_at": ""}, {**second.to_dict(), "recorded_at": ""}
        )

    def test_the_module_contains_no_clock_and_no_environment(self):
        source = inspect.getsource(agent_cost_module)
        for forbidden in ("datetime", "time.time", "monotonic", "os.environ", "getenv", "now("):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_the_pure_function_takes_no_path_and_touches_no_file(self):
        parameters = inspect.signature(hook_costs).parameters
        self.assertEqual(tuple(parameters), ("report", "version", "recorded_at"))
        self.assertNotIn("path", parameters)

        def refuse(*args, **kwargs):
            raise AssertionError("the pure function touched the filesystem")

        with mock.patch("builtins.open", refuse), mock.patch.object(
            Path, "read_text", refuse
        ), mock.patch.object(Path, "exists", refuse):
            self.assertEqual(len(costs_for(hook_resolution())), 1)

    def test_a_report_keeps_its_order(self):
        costs = costs_for(hook_resolution(CONTEXT, found_by="named", descriptor=SHELL), hook_resolution(REELS))
        self.assertEqual([cost.hook_id for cost in costs], [CONTEXT, REELS])

    def test_a_decode_path_passed_as_the_version_is_refused_here_too(self):
        with self.assertRaises(DecisionError) as caught:
            costs_for(hook_resolution(), version=DECODE)
        self.assertIn("looks like a path, not a version label", str(caught.exception))


# ---------------------------------------------------------------- the five routes


class RouteTests(AgentCostTestCase):
    """What each hook cost, by the route that produced it.

    The five the pipeline can take, plus `already_applied`, which is none of them
    and must not be counted as any of them.
    """

    def test_an_anchor_only_hook_is_mechanical_and_owes_no_agent(self):
        cost = one_cost(hook_resolution())
        self.assertEqual(cost.route, ROUTE_MECHANICAL)
        self.assertEqual(cost.agent_for, ())
        self.assertFalse(cost.needed_agent)

    def test_a_deterministic_supplier_is_its_own_route_not_merely_mechanical(self):
        """The distinction the early warning depends on.

        A hook resolved by a rule that could stop applying is not in the same
        state as one resolved by its anchor alone, and collapsing them would hide
        every future decline behind an unchanged count.
        """
        cost = one_cost(settings_hook(supplied(guard_answered(), winner=GUARD), found_by="named", descriptor=SHELL))
        self.assertEqual(cost.route, ROUTE_DETERMINISTIC_SUPPLIER)
        self.assertEqual(cost.agent_for, ())

    def test_an_agent_supplier_is_an_agent_invocation_and_says_which_captures(self):
        cost = one_cost(
            settings_hook(
                supplied(guard_declined(), agent_answered(), winner=AGENT),
                found_by="named",
                descriptor=SHELL,
            )
        )
        self.assertEqual(cost.route, ROUTE_AGENT_SUPPLIER)
        self.assertEqual(cost.agent_for, (f"{NEED_CAPTURE}:model", f"{NEED_CAPTURE}:self_type"))
        self.assertTrue(cost.needed_agent)

    def test_an_agent_proposed_host_is_an_agent_invocation_for_a_host(self):
        cost = one_cost(settings_hook())
        self.assertEqual(cost.route, ROUTE_AGENT_PROPOSAL)
        self.assertEqual(cost.agent_for, (NEED_HOST,))

    def test_a_hook_that_did_not_resolve_records_a_cost_and_no_resolution(self):
        """The two halves of stage 10, on one hook, deliberately disagreeing.

        Decision memory must record nothing for an escalation — absence is never
        a pass, and a reason string filed as an answer is replayed next port. The
        cost ledger must record it, because the escalation IS the cost. A build
        that recorded neither is the state this pipeline was in.
        """
        escalated = settings_hook(outcome=Outcome.NEEDS_AGENT, resolution=None, evidence={"proposed": []})
        self.assertEqual(records_for(escalated), ())
        cost = one_cost(escalated)
        self.assertEqual(cost.route, ROUTE_NOT_RESOLVED)
        self.assertEqual(cost.outcome, Outcome.NEEDS_AGENT.value)

    def test_a_needs_agent_with_no_host_proposed_asks_for_a_host(self):
        cost = one_cost(
            settings_hook(
                outcome=Outcome.NEEDS_AGENT,
                resolution=None,
                evidence={"proposed": []},
                searches=(HostSearch("by_agent", (), {"proposed": []}, reason="no host proposed"),),
                candidates=(),
            )
        )
        self.assertEqual(cost.agent_for, (NEED_HOST,))

    def test_a_needs_agent_whose_anchor_matched_asks_for_the_whole_patch(self):
        """`requires_proposal`: the site is known and the payload is a shape.

        A different cost from a missing host — a host can be retired by a
        manifest fingerprint, a whole patch cannot — so they may not collapse.
        """
        cost = one_cost(settings_hook(outcome=Outcome.NEEDS_AGENT, resolution=None))
        self.assertEqual(cost.agent_for, (NEED_PATCH,))

    def test_a_needs_agent_with_unfilled_captures_names_the_captures(self):
        cost = one_cost(
            settings_hook(
                supplied(guard_declined(), decline(AGENT, STAGE_NO_PROPOSAL, "no proposal")),
                outcome=Outcome.NEEDS_AGENT,
                resolution=None,
            )
        )
        self.assertEqual(cost.agent_for, (f"{NEED_CAPTURE}:model", f"{NEED_CAPTURE}:self_type"))

    def test_a_blocked_outcome_is_not_counted_as_an_agent_invocation(self):
        """AMBIGUOUS and NOT_FOUND stop the port; they do not run an agent.

        `pipeline_flowchart.md` defines NEEDS_AGENT as the only way an agent runs,
        so counting the others would inflate the very number this exists to track
        and make a blocked port read as an expensive one.
        """
        for outcome in (Outcome.AMBIGUOUS, Outcome.UNRESOLVED, Outcome.NOT_FOUND, Outcome.CONFLICT):
            with self.subTest(outcome=outcome.value):
                cost = one_cost(hook_resolution(outcome=outcome))
                self.assertEqual(cost.route, ROUTE_NOT_RESOLVED)
                self.assertFalse(cost.needed_agent)
                self.assertIn("blocked port rather than an agent invocation", cost.note)

    def test_already_applied_is_its_own_route_and_not_a_mechanical_win(self):
        """Mutation bait: fold `already_applied` into `mechanical`.

        A second run over one decode would then report every hook newly
        mechanised, and the agent count would fall without anything having been
        learned — the exact false positive the metric exists to avoid.
        """
        cost = one_cost(hook_resolution(outcome=Outcome.ALREADY_APPLIED, resolution=None))
        self.assertEqual(cost.route, ROUTE_ALREADY_APPLIED)
        self.assertNotIn(ROUTE_ALREADY_APPLIED, AGENT_ROUTES)
        self.assertIn("learned nothing about what it would cost", cost.note)

    def test_a_route_claiming_no_agent_may_not_carry_an_agent_need(self):
        with self.assertRaises(DecisionError) as caught:
            HookCost(REELS, VERSION, ROUTE_MECHANICAL, "resolved", agent_for=(NEED_HOST,))
        self.assertIn("is not mechanical", str(caught.exception))

    def test_an_agent_route_must_say_what_the_agent_was_for(self):
        with self.assertRaises(DecisionError) as caught:
            HookCost(REELS, VERSION, ROUTE_AGENT_PROPOSAL, "resolved")
        self.assertIn("nothing says what for", str(caught.exception))

    def test_an_unrecognised_route_is_refused_rather_than_dropped_from_the_count(self):
        with self.assertRaises(DecisionError) as caught:
            HookCost(REELS, VERSION, "mechanicaal", "resolved")
        self.assertIn("is not one of", str(caught.exception))


# ------------------------------------------------- supplier attempts and declines


class SupplierAttemptTests(AgentCostTestCase):
    """The early warning. A decline is a finding; a failure is a fault.

    `capture_supply` is explicit that the first is a returned value with a
    machine-readable stage and the second is an exception. Only the first can
    reach this ledger, and it has to arrive with the stage attached or the ledger
    records only "an agent ran" — which is indistinguishable from "this version is
    genuinely new", and is exactly the blindness being fixed.
    """

    def test_a_decline_is_recorded_with_its_machine_readable_stage(self):
        cost = one_cost(
            settings_hook(
                supplied(guard_declined(STAGE_DRAWABLE_ABSENT), agent_answered(), winner=AGENT),
                found_by="named",
                descriptor=SHELL,
            )
        )
        self.assertEqual([item.supplier for item in cost.attempts], [GUARD, AGENT])
        rejected = cost.attempts[0]
        self.assertFalse(rejected.answered)
        self.assertEqual(rejected.stage, STAGE_DRAWABLE_ABSENT)
        self.assertTrue(rejected.deterministic)
        self.assertEqual(cost.deterministic_declines, (rejected,))
        # And the winner is not mistaken for one.
        self.assertTrue(cost.attempts[1].answered)
        self.assertEqual(cost.attempts[1].stage, "")
        self.assertFalse(cost.attempts[1].deterministic)

    def test_a_decline_without_a_stage_cannot_be_recorded_at_all(self):
        with self.assertRaises(DecisionError) as caught:
            SupplierAttempt(GUARD, ("model",), answered=False, reason="it did not work")
        self.assertIn("must carry the stage it stopped at", str(caught.exception))

    def test_a_winner_may_not_carry_a_decline_stage(self):
        with self.assertRaises(DecisionError) as caught:
            SupplierAttempt(GUARD, ("model",), answered=True, stage=STAGE_DRAWABLE_ABSENT)
        self.assertIn("rotting rule", str(caught.exception))

    def test_a_failure_is_an_exception_and_so_reaches_no_record(self):
        """The distinction drawn at the boundary that actually draws it.

        `run_supply_chain` catches nothing: a supplier asked the wrong roles, or
        an unreadable index, raises out of the run. So a fault cannot be recorded
        as a finding about the target — the port dies instead, which is the
        correct outcome and the reason a stage string always means the target.
        """

        class Request:
            supply = SUPPLY

        def boom(request):
            raise ManifestError("the index is unreadable — a fault, not a finding")

        with self.assertRaises(ManifestError):
            run_supply_chain(Request(), {GUARD: boom, AGENT: lambda request: agent_answered()})

        outcome = run_supply_chain(
            Request(),
            {GUARD: lambda request: guard_declined(), AGENT: lambda request: agent_answered()},
        )
        cost = one_cost(settings_hook(outcome, found_by="named", descriptor=SHELL))
        self.assertEqual([item.stage for item in cost.attempts], [STAGE_PRECONDITION_TYPE_ABSENT, ""])

    def test_the_deterministic_flag_is_derived_from_the_supplier_name(self):
        """A stored flag could disagree with the name; the whole query turns on it."""
        self.assertTrue(SupplierAttempt(GUARD, (), answered=True).deterministic)
        self.assertFalse(SupplierAttempt(AGENT, (), answered=True).deterministic)
        self.assertNotIn("deterministic=", inspect.getsource(agent_cost_module))


# ---------------------------------------------------------- measured selectivity


class SelectivityTests(AgentCostTestCase):
    """The numbers a run computes and then throws away, kept as numbers.

    `_selectivity` in `manifest_update` already writes them into a chain
    `finding`, as prose, which is the right place for a human reading one record
    and useless for a trend. `resolve.py` calls out the same antipattern when it
    refuses to make a gate "parse '1/2' out of the reason prose".
    """

    def test_a_by_literal_host_records_the_widest_literal_and_the_intersection(self):
        cost = one_cost(hook_resolution())
        measurement = cost.selectivity[0]
        self.assertEqual(measurement.subject, "by_literal")
        self.assertEqual(measurement.candidates, 4)  # the least selective literal alone
        self.assertEqual(measurement.hits, 1)  # all three together
        self.assertEqual(measurement.margin, 3)
        self.assertEqual(measurement.detail, dict(LITERAL_EVIDENCE["classes_per_literal"]))

    def test_the_capture_suppliers_counts_are_recovered_as_numbers(self):
        """"10 subtypes tested, 1 loads the drawable" is the second discriminator.

        It exists only as prose inside `Supplied.evidence`, which is the seam:
        `capture_supply.Supplied` wants a typed `measured` mapping filled where
        the count is taken, the way `HostSearch.evidence` already carries
        `classes_per_literal` as a dict.
        """
        cost = one_cost(
            settings_hook(
                supplied(guard_answered(hits=1, candidates=11), winner=GUARD),
                found_by="named",
                descriptor=SHELL,
            )
        )
        measurement = cost.selectivity[0]
        self.assertEqual(measurement.subject, f"supplier:{GUARD}")
        self.assertEqual((measurement.candidates, measurement.hits), (11, 1))
        self.assertEqual(cost.attempts[0].measured, (measurement,))

    def test_an_evidence_line_that_states_no_count_records_no_measurement(self):
        """Fails closed. A guessed number would be a trend nobody measured."""
        vague = Supplied(GUARD, {"model_register": "v1"}, evidence=("it worked out",))
        cost = one_cost(settings_hook(supplied(vague, winner=GUARD), found_by="named", descriptor=SHELL))
        self.assertEqual(cost.selectivity, ())
        self.assertEqual(cost.attempts[0].evidence, ("it worked out",))

    def test_a_measurement_claiming_more_hits_than_candidates_is_refused(self):
        with self.assertRaises(DecisionError) as caught:
            Selectivity("by_literal", "x", candidates=2, hits=3)
        self.assertIn("is impossible", str(caught.exception))

    def test_the_three_dangerous_shapes_are_named_rather_than_left_to_a_reader(self):
        self.assertTrue(Selectivity("s", "m", candidates=0, hits=0).failed)
        self.assertTrue(Selectivity("s", "m", candidates=1, hits=1).vacuous)
        self.assertTrue(Selectivity("s", "m", candidates=4, hits=2).ambiguous)
        healthy = Selectivity("s", "m", candidates=4, hits=1)
        self.assertFalse(healthy.failed or healthy.vacuous or healthy.ambiguous)

    def test_the_selectivity_is_recorded_even_when_the_hook_did_not_resolve(self):
        """The version a fingerprint stops discriminating is the version it fails.

        Recording margins only for resolved hooks would lose the measurement in
        exactly the run that explains the failure.
        """
        broken = hook_resolution(
            outcome=Outcome.NOT_FOUND,
            resolution=None,
            evidence={
                "literals": list(LITERALS),
                "classes_per_literal": {LITERALS[0]: 4, LITERALS[1]: 3, LITERALS[2]: 2},
                "co_located": 0,
            },
        )
        cost = one_cost(broken)
        self.assertEqual((cost.selectivity[0].candidates, cost.selectivity[0].hits), (4, 0))
        self.assertTrue(cost.selectivity[0].failed)


# ----------------------------------------------------------- the forbidden set


class CostForbiddenValueTests(AgentCostTestCase):
    """Each attack builds the record that would leak, with a positive control.

    The strings are the real ones: `capture_supply` really does append
    ``"1 of 11 subtypes load instagram_menu_outline_24 (0x7f082538): LX/0Dxw;"``
    to its evidence on 439, so the leak is not hypothetical and the fixture is
    not a straw man.
    """

    def test_an_obfuscated_descriptor_in_supplier_evidence_is_withheld(self):
        cost = one_cost(
            settings_hook(supplied(guard_answered(), winner=GUARD), found_by="named", descriptor=SHELL)
        )
        text = stored(cost)
        self.assertNotIn(SELF_TYPE, text)
        self.assertIn(WITHHELD_DESCRIPTOR, text)
        # Positive control: the STABLE named type in the same evidence tuple is
        # kept. It is the supplier's precondition — a decline that could not name
        # it would say only that something was missing.
        self.assertIn(ACTIONBAR, text)

    def test_a_resource_id_in_supplier_evidence_is_withheld(self):
        cost = one_cost(
            settings_hook(supplied(guard_answered(), winner=GUARD), found_by="named", descriptor=SHELL)
        )
        text = stored(cost)
        self.assertNotIn(DRAWABLE_ID, text)
        self.assertIn(REDACTED_RESOURCE_ID, text)
        # Positive control: the drawable NAME survives. 98.8% of names survive a
        # version step and 0.9% of ids do, so the name is the handle.
        self.assertIn("instagram_menu_outline_24", text)

    def test_an_obfuscated_host_never_reaches_the_cost_record_at_all(self):
        """A cost is a fact about the port. It needs no class and stores none."""
        cost = one_cost(hook_resolution())
        self.assertNotIn(HOST, stored(cost))
        self.assertNotIn("smali_path", stored(cost))
        self.assertNotIn(PATH, stored(cost))

    def test_an_absolute_path_quoted_by_a_decline_is_withheld(self):
        """Attack: `STAGE_CANDIDATE_UNREADABLE` quotes an OSError.

        Its message carries `decode / path`, which is absolute, so nobody has to
        write a machine path down for one to be stored.
        """
        unreadable = decline(
            GUARD,
            "candidate_unreadable",
            f"the index names it but it cannot be read ([Errno 2] {DECODE}/smali/X/a.smali)",
        )
        cost = one_cost(
            settings_hook(
                supplied(unreadable, agent_answered(), winner=AGENT), found_by="named", descriptor=SHELL
            )
        )
        text = stored(cost)
        self.assertNotIn(DECODE, text)
        self.assertIn(WITHHELD_PATH, text)
        # Positive control: the stage survived the scrub, which is the only part
        # of that decline the early warning needs.
        self.assertIn("candidate_unreadable", text)

    def test_an_api_path_literal_is_kept_even_though_it_is_path_shaped(self):
        """The mirror failure: scrubbing so hard the measurement is lost.

        `/api/v1/clips/` and `/home/arnav/work/` are the same shape. The literal
        is the strongest fingerprint measured (93.9% survival), and a detail key
        that came back as `<absolute-path-withheld>` would collapse every literal
        in the map onto one key.
        """
        rooted = hook_resolution(
            evidence={
                "literals": ["/api/v1/clips/discover/", "/api/v1/clips/home/"],
                "classes_per_literal": {"/api/v1/clips/discover/": 4, "/api/v1/clips/home/": 3},
                "co_located": 1,
            }
        )
        cost = one_cost(rooted)
        self.assertEqual(sorted(cost.selectivity[0].detail), ["/api/v1/clips/discover/", "/api/v1/clips/home/"])
        self.assertNotIn(WITHHELD_PATH, stored(cost))

    def test_a_hand_built_record_carrying_a_leak_is_refused_not_stored(self):
        """Scrub-then-refuse: the builder cleans, the record checks.

        A later caller constructing a HookCost directly cannot skip the first
        half, the same way `decisions.Step` refuses an absolute path rather than
        trusting whoever built it.
        """
        for value, expected in (
            (f"resolved via {SELF_TYPE}", "obfuscated descriptor"),
            (f"the drawable is {DRAWABLE_ID}", "resource id"),
            (f"see {DECODE}/x.smali", "absolute path"),
        ):
            with self.subTest(value=value):
                with self.assertRaises(DecisionError) as caught:
                    HookCost(REELS, VERSION, ROUTE_MECHANICAL, "resolved", note=value)
                self.assertIn(expected, str(caught.exception))
        # Positive control: the same field accepts what it is for.
        self.assertEqual(
            HookCost(REELS, VERSION, ROUTE_MECHANICAL, "resolved", note=f"{ACTIONBAR} is present").note,
            f"{ACTIONBAR} is present",
        )

    def test_scrub_keeps_a_mobileconfig_flag_and_a_relative_smali_path(self):
        self.assertEqual(scrub("0x81099a000034a6"), "0x81099a000034a6")
        self.assertEqual(scrub("smali_classes6/X/0DnT.smali"), "smali_classes6/X/0DnT.smali")
        self.assertEqual(scrub("v1=11 type(s)"), "v1=11 type(s)")
        self.assertEqual(scrub(ACTIONBAR), ACTIONBAR)
        self.assertEqual(scrub("LX/0Dxw;"), WITHHELD_DESCRIPTOR)

    def test_nothing_is_written_into_the_field_the_proposer_is_shown(self):
        cost = one_cost(hook_resolution())
        self.assertNotIn("intent_constraints", stored(cost))
        self.assertNotIn("intent_constraints", inspect.getsource(agent_cost_module))


# --------------------------------------------------------------------- the query


class CostQueryTests(AgentCostTestCase):
    """The deliverable: the claim, per version, against the version before it."""

    def test_it_counts_agent_invocations_and_says_what_each_was_for(self):
        ledger = ledger_of(
            (
                VERSION,
                hook_resolution(REELS),
                settings_hook(),  # agent host
                settings_hook(outcome=Outcome.NEEDS_AGENT, resolution=None),  # whole patch
            )
        )
        report_out = cost_report(ledger, VERSION)
        self.assertEqual(report_out["now"]["agent_invocations"], 2)
        self.assertEqual(report_out["now"]["by_need"], {NEED_HOST: 1, NEED_CAPTURE: 0, NEED_PATCH: 1, NEED_UNSPECIFIED: 0})
        self.assertEqual(
            [(entry["hook_id"], entry["needed_for"]) for entry in report_out["agent_hooks"]],
            [(SETTINGS, [NEED_HOST]), (SETTINGS, [NEED_PATCH])],
        )

    def test_one_version_alone_makes_the_claim_untestable_and_says_so(self):
        """The honest verdict, and the one the pipeline has had until now.

        "Agent invocations should fall" is a claim about a sequence, so a single
        port cannot satisfy it — and must not be reported as if it had.
        """
        ledger = ledger_of((VERSION, hook_resolution(), settings_hook()))
        report_out = cost_report(ledger, VERSION)
        self.assertEqual(report_out["verdict"], VERDICT_UNTESTABLE)
        self.assertIsNone(report_out["delta_agent_invocations"])
        self.assertIn("claim about a sequence", "\n".join(render(report_out)))

    def test_a_flat_count_is_reported_as_flat_and_not_as_success(self):
        ledger = ledger_of(
            ("430", hook_resolution(), settings_hook()),
            ("439", hook_resolution(), settings_hook()),
        )
        report_out = cost_report(ledger, "439")
        self.assertEqual(report_out["previous_version"], "430")
        self.assertEqual(report_out["delta_agent_invocations"], 0)
        self.assertEqual(report_out["verdict"], VERDICT_FLAT)
        self.assertIn("FLAT against 430", "\n".join(render(report_out)))
        self.assertIn("is not learning", "\n".join(render(report_out)))

    def test_a_hook_that_became_mechanical_shows_as_falling_and_is_named(self):
        ledger = ledger_of(
            ("430", hook_resolution(), settings_hook()),
            ("439", hook_resolution(), settings_hook(found_by="by_literal")),
        )
        report_out = cost_report(ledger, "439")
        self.assertEqual(report_out["verdict"], VERDICT_FALLING)
        self.assertEqual(report_out["delta_agent_invocations"], -1)
        self.assertEqual(report_out["retired"], [SETTINGS])
        self.assertIn("falling — 1 fewer than 430", "\n".join(render(report_out)))

    def test_a_new_agent_cost_shows_as_rising_and_is_named(self):
        ledger = ledger_of(
            ("430", hook_resolution(), hook_resolution(CONTEXT, found_by="named", descriptor=SHELL)),
            ("439", hook_resolution(), hook_resolution(CONTEXT, found_by="by_agent", descriptor=SHELL)),
        )
        report_out = cost_report(ledger, "439")
        self.assertEqual(report_out["verdict"], VERDICT_RISING)
        self.assertEqual(report_out["newly_costly"], [CONTEXT])

    def test_a_narrowing_selectivity_margin_is_visible_across_two_versions(self):
        """The requirement in one test: see it at 3 -> 1, not at 0.

        Both versions resolve, both ports succeed, and nothing else in the
        pipeline reports any difference between them. The discriminator lost 7 of
        its 9 excluded candidates.
        """

        def reels(widest):
            return hook_resolution(
                evidence={
                    "literals": list(LITERALS),
                    "classes_per_literal": {LITERALS[0]: widest, LITERALS[1]: 3, LITERALS[2]: 2},
                    "co_located": 1,
                }
            )

        ledger = ledger_of(("430", reels(10)), ("439", reels(3)))
        report_out = cost_report(ledger, "439")
        entry = report_out["selectivity"][0]
        self.assertEqual((entry["candidates"], entry["hits"]), (3, 1))
        self.assertEqual((entry["previous"]["candidates"], entry["previous"]["hits"]), (10, 1))
        self.assertEqual(entry["trend"], "NARROWING")
        text = "\n".join(render(report_out))
        self.assertIn("3 -> 1", text)
        self.assertIn("(430: 10 -> 1)", text)
        self.assertIn("NARROWING", text)

    def test_a_margin_that_reaches_one_to_one_is_called_vacuous_before_it_fails(self):
        def reels(widest, co_located=1):
            return hook_resolution(
                evidence={
                    "literals": [LITERALS[0]],
                    "classes_per_literal": {LITERALS[0]: widest},
                    "co_located": co_located,
                }
            )

        ledger = ledger_of(("430", reels(3)), ("439", reels(1)), ("440", reels(1, 0)))
        self.assertEqual(cost_report(ledger, "439")["selectivity"][0]["trend"], "VACUOUS")
        self.assertEqual(cost_report(ledger, "440")["selectivity"][0]["trend"], "FAILED")

    def test_a_deterministic_rule_that_stopped_answering_is_reported_as_rot(self):
        """The signal the whole module exists for.

        Both ports succeed. Both produce a rendered patch. The only difference is
        which supplier answered, and without this the visible symptom is "an agent
        ran" — indistinguishable from "this version is genuinely new".
        """
        healthy = settings_hook(supplied(guard_answered(), winner=GUARD), found_by="named", descriptor=SHELL)
        rotted = settings_hook(
            supplied(guard_declined(STAGE_DRAWABLE_ABSENT), agent_answered(), winner=AGENT),
            found_by="named",
            descriptor=SHELL,
        )
        ledger = ledger_of(("430", healthy), ("439", rotted))

        before = cost_report(ledger, "430")
        self.assertEqual(before["now"]["agent_invocations"], 0)
        self.assertEqual(before["rotting"], [])

        after = cost_report(ledger, "439")
        self.assertEqual(len(after["rotting"]), 1)
        entry = after["rotting"][0]
        self.assertEqual(entry["supplier"], GUARD)
        self.assertEqual(entry["stage"], STAGE_DRAWABLE_ABSENT)
        self.assertTrue(entry["answered_previously"])
        self.assertEqual(entry["fell_through_to"], "an agent")
        text = "\n".join(render(after))
        self.assertIn(f"declined at '{STAGE_DRAWABLE_ABSENT}'", text)
        self.assertIn("it ANSWERED on 430", text)
        self.assertIn("rule rotting, not a new version", text)

    def test_a_rule_that_ran_and_held_is_not_reported_as_one_that_never_ran(self):
        """"No declines" is true of a rule that held and of one nobody asked.

        Those are different states and only the first is evidence the rule still
        works, so the healthy report names the rules that are holding rather than
        saying nothing broke.
        """
        held = cost_report(
            ledger_of((VERSION, settings_hook(supplied(guard_answered(), winner=GUARD), found_by="named", descriptor=SHELL))),
            VERSION,
        )
        self.assertEqual(held["holding"], [{"hook_id": SETTINGS, "supplier": GUARD}])
        self.assertIn(f"1 held — {GUARD} for {SETTINGS}", "\n".join(render(held)))

        never = cost_report(ledger_of((VERSION, hook_resolution())), VERSION)
        self.assertEqual(never["holding"], [])
        self.assertIn("none ran. Not 'none broke'", "\n".join(render(never)))

    def test_a_rule_that_stopped_being_tried_at_all_is_also_reported(self):
        """A decline is visible; a supplier the manifest stopped asking is not.

        Both end with an agent answering, so both look identical from outside.
        """
        healthy = settings_hook(supplied(guard_answered(), winner=GUARD), found_by="named", descriptor=SHELL)
        dropped = settings_hook(
            supplied(agent_answered(), winner=AGENT), found_by="named", descriptor=SHELL
        )
        after = cost_report(ledger_of(("430", healthy), ("439", dropped)), "439")
        self.assertEqual([entry["stage"] for entry in after["rotting"]], ["not_tried"])
        self.assertIn("did not run at all in this one", after["rotting"][0]["reason"])

    def test_an_unmeasured_version_reports_nothing_rather_than_zero_cost(self):
        report_out = cost_report(ledger_of((VERSION, hook_resolution())), "999")
        self.assertFalse(report_out["recorded"])
        self.assertIn("not 'nothing cost anything'", "\n".join(render(report_out)).lower())

    def test_an_explicit_previous_version_overrides_the_preceding_one(self):
        ledger = ledger_of(
            ("340", hook_resolution(), settings_hook(), settings_hook(outcome=Outcome.NEEDS_AGENT, resolution=None)),
            ("430", hook_resolution(), settings_hook()),
            ("439", hook_resolution(), settings_hook()),
        )
        self.assertEqual(cost_report(ledger, "439")["verdict"], VERDICT_FLAT)
        self.assertEqual(cost_report(ledger, "439", previous="340")["verdict"], VERDICT_FALLING)

    def test_the_cli_prints_the_report_and_exits_nonzero_on_an_unmeasured_version(self):
        path = self.tmp / "manifest" / "agent_cost.jsonl"
        update_ledger(report(hook_resolution(), settings_hook()), VERSION, STAMP, path=path)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = agent_cost_module.main(["report", VERSION, "--ledger", str(path)])
        self.assertEqual(code, 0)
        self.assertIn("AGENT COST — 439", buffer.getvalue())
        self.assertIn("agent invocations: 1", buffer.getvalue())
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(agent_cost_module.main(["report", "999", "--ledger", str(path)]), 1)


# ----------------------------------------------------------------- one run, not a version


class CostRunTests(AgentCostTestCase):
    """A report is about one PORT, and two attempts at a version are not two ports.

    Found by the first real unattended port. 439 was run once, stopped at resolve,
    and re-run; `cost_report` aggregated every record for the version and reported
    7 hooks as 14, 2 agent invocations as 4, and every selectivity margin twice.

    The ledger was right and is untouched: it is append-only, both attempts
    genuinely cost what they cost, and dropping the failed one on write would be
    deciding which run was the real one. Every test here is about the *reading*.

    The failure mode is worse than an inflated number. It is a number that moves
    with how many times somebody retried, so "agent invocations per port" flatters
    or damns a port according to a fact about the operator — and that is the one
    number `pipeline_flowchart.md` asks the project to judge itself by.
    """

    def test_two_runs_of_one_version_report_one_run_and_not_their_sum(self):
        ledger = runs_ledger(
            ("439", "T1", hook_resolution(REELS), settings_hook()),
            ("439", "T2", hook_resolution(REELS), settings_hook()),
        )
        # The ledger still holds both attempts. It must: this is the append-only
        # record of what was spent, and the fix is not allowed to be a deletion.
        self.assertEqual(len(ledger.costs_for("439")), 4)

        report_out = cost_report(ledger, "439")
        self.assertEqual(report_out["now"]["hooks"], 2)
        self.assertEqual(report_out["now"]["agent_invocations"], 1)
        self.assertEqual(report_out["now"]["routes"][ROUTE_AGENT_PROPOSAL], 1)
        self.assertEqual(report_out["now"]["routes"][ROUTE_MECHANICAL], 1)
        self.assertEqual([entry["hook_id"] for entry in report_out["agent_hooks"]], [SETTINGS])
        # One margin, measured once — the same fingerprint listed twice reads as
        # two independent measurements agreeing.
        self.assertEqual(len(report_out["selectivity"]), 1)
        self.assertIn("agent invocations: 1", "\n".join(render(report_out)))

    def test_the_report_says_how_many_runs_the_ledger_holds_for_the_version(self):
        """The alternative to folding the other runs in is not hiding them."""
        ledger = runs_ledger(
            ("439", "T1", hook_resolution(REELS), settings_hook()),
            ("439", "T2", hook_resolution(REELS), settings_hook()),
        )
        report_out = cost_report(ledger, "439")
        self.assertEqual(
            report_out["run"],
            {
                "ordinal": 2,
                "of": 2,
                "recorded_at": "T2",
                "hooks": 2,
                "agent_invocations": 1,
                "selected": SELECTED_LATEST,
            },
        )
        self.assertEqual([item["ordinal"] for item in report_out["runs"]], [1, 2])
        self.assertEqual([item["recorded_at"] for item in report_out["runs"]], ["T1", "T2"])

        text = "\n".join(render(report_out))
        self.assertIn("run 2 of 2 for 439, recorded T2", text)
        self.assertIn("1 other run(s) for 439 are in this ledger", text)
        self.assertIn("NOT ONE of their records is counted below", text)
        self.assertIn("run 1   T1", text)

    def test_a_version_with_a_single_run_behaves_exactly_as_before(self):
        ledger = ledger_of((VERSION, hook_resolution(), settings_hook()))
        report_out = cost_report(ledger, VERSION)
        self.assertEqual(report_out["now"]["hooks"], 2)
        self.assertEqual(report_out["now"]["agent_invocations"], 1)
        self.assertEqual(report_out["run"]["ordinal"], 1)
        self.assertEqual(report_out["run"]["of"], 1)
        text = "\n".join(render(report_out))
        self.assertIn("run 1 of 1 for 439", text)
        self.assertIn("the only run this ledger holds", text)
        self.assertNotIn("other run(s)", text)

    def test_an_unstamped_ledger_is_one_run_rather_than_none(self):
        """Hand-built records carry no stamp. That is one run, and it says so."""
        ledger = CostLedger()
        ledger.record(HookCost(REELS, VERSION, ROUTE_MECHANICAL, "resolved"))
        report_out = cost_report(ledger, VERSION)
        self.assertEqual(report_out["run"]["of"], 1)
        self.assertEqual(report_out["run"]["recorded_at"], "")
        self.assertIn(f"recorded {UNSTAMPED}", "\n".join(render(report_out)))

    def test_previous_compares_the_latest_run_of_each_version(self):
        ledger = runs_ledger(
            ("430", "A1", hook_resolution(), settings_hook(), settings_hook()),
            ("430", "A2", hook_resolution(), settings_hook()),
            ("439", "B1", hook_resolution(), settings_hook()),
        )
        report_out = cost_report(ledger, "439")
        self.assertEqual(report_out["previous_version"], "430")
        self.assertEqual(report_out["previous_run"]["ordinal"], 2)
        self.assertEqual(report_out["previous_run"]["of"], 2)
        self.assertEqual(report_out["previous"]["agent_invocations"], 1)
        self.assertEqual(report_out["previous"]["hooks"], 2)
        self.assertIn("(was 1 on 430, its latest of 2 run(s))", "\n".join(render(report_out)))

    def test_the_verdict_comes_from_the_compared_runs_and_not_from_totals(self):
        """A retry must not be able to move the verdict in either direction.

        Summed, the first ledger reads FALLING (430 cost 2, 439 cost 1) and the
        pipeline looks like it learned something between ports; the second reads
        RISING (439 cost 2, 430 cost 1) and looks like a regression. Both are the
        same fact: somebody ran a port twice. Run against run, both are FLAT —
        which is the honest answer and the one the flowchart calls not learning.
        """
        retried_before = runs_ledger(
            ("430", "A1", hook_resolution(), settings_hook()),
            ("430", "A2", hook_resolution(), settings_hook()),
            ("439", "B1", hook_resolution(), settings_hook()),
        )
        self.assertEqual(
            sum(1 for cost in retried_before.costs_for("430") if cost.needed_agent), 2
        )
        report_out = cost_report(retried_before, "439")
        self.assertEqual(report_out["delta_agent_invocations"], 0)
        self.assertEqual(report_out["verdict"], VERDICT_FLAT)
        text = "\n".join(render(report_out))
        self.assertIn("FLAT against 430", text)
        self.assertNotIn("falling", text)

        retried_after = runs_ledger(
            ("430", "A1", hook_resolution(), settings_hook()),
            ("439", "B1", hook_resolution(), settings_hook()),
            ("439", "B2", hook_resolution(), settings_hook()),
        )
        self.assertEqual(cost_report(retried_after, "439")["verdict"], VERDICT_FLAT)
        self.assertNotIn("RISING", "\n".join(render(cost_report(retried_after, "439"))))

    def test_a_real_fall_between_ports_is_still_reported_as_falling(self):
        """The positive control: run-scoping must not make every verdict FLAT.

        A guard that only ever reports "no change" would pass every test above and
        report a pipeline that had genuinely stopped needing an agent as one that
        had not learned anything.
        """
        ledger = runs_ledger(
            ("430", "A1", hook_resolution(), settings_hook()),
            ("430", "A2", hook_resolution(), settings_hook()),
            ("439", "B1", hook_resolution(), settings_hook(found_by="by_literal")),
        )
        report_out = cost_report(ledger, "439")
        self.assertEqual(report_out["verdict"], VERDICT_FALLING)
        self.assertEqual(report_out["delta_agent_invocations"], -1)
        self.assertEqual(report_out["retired"], [SETTINGS])

    def test_an_earlier_run_with_more_hooks_does_not_leak_into_the_later_report(self):
        """The attack: a first attempt that got FURTHER than the one that shipped.

        Attempt one resolved three hooks and paid an agent for one of them;
        the re-run covered two and paid nothing. Every list in the report is built
        by comprehension over the records, so a single unscoped one puts a hook
        that this port never touched into this port's cost — and it is the agent
        count, the number the whole module exists to make falsifiable, that it
        inflates.
        """
        settings_with_guard = settings_hook(
            supplied(guard_answered(), winner=GUARD), found_by="named", descriptor=SHELL
        )
        ledger = runs_ledger(
            (
                "439",
                "T1",
                hook_resolution(REELS),
                hook_resolution(CONTEXT, found_by="named", descriptor=SHELL),
                settings_with_guard,
            ),
            (
                "439",
                "T2",
                hook_resolution(REELS),
                hook_resolution(CONTEXT, found_by="named", descriptor=SHELL),
            ),
        )
        self.assertEqual(len(ledger.costs_for("439")), 5)

        report_out = cost_report(ledger, "439")
        self.assertEqual(report_out["now"]["hooks"], 2)
        self.assertEqual(report_out["now"]["agent_invocations"], 0)
        self.assertEqual(report_out["agent_hooks"], [])
        self.assertEqual(report_out["now"]["by_need"][NEED_CAPTURE], 0)
        # The guard ran in the first attempt only: reporting it as holding now
        # would be evidence about a rule taken from a run that is not this one.
        self.assertEqual(report_out["holding"], [])
        self.assertEqual({entry["hook_id"] for entry in report_out["selectivity"]}, {REELS})
        text = "\n".join(render(report_out))
        self.assertNotIn(SETTINGS, text)
        self.assertNotIn(GUARD, text)
        self.assertIn("none — every hook resolved without one", text)

    def test_the_latest_run_is_reported_even_when_an_earlier_one_was_cheaper(self):
        """Latest, not best — and the choice is in the output rather than implied.

        Reporting a version's cheapest attempt would make this number one that
        cannot rise, and a number that cannot rise is a press release rather than
        a measurement. So the expensive latest run is the one counted, and the
        cheaper attempt is named next to it so nobody has to take that on trust.
        """
        ledger = runs_ledger(
            ("439", "T1", hook_resolution(), settings_hook(found_by="by_literal")),
            ("439", "T2", hook_resolution(), settings_hook()),
        )
        report_out = cost_report(ledger, "439")
        self.assertEqual(report_out["now"]["agent_invocations"], 1)
        self.assertEqual(report_out["runs"][0]["agent_invocations"], 0)
        text = "\n".join(render(report_out))
        self.assertIn("Reporting the LATEST run whether or not it was the best one", text)
        self.assertIn("run 1 cost fewer agent invocations (0)", text)
        self.assertIn("press release", text)

    def test_an_earlier_run_can_be_reported_deliberately_and_says_that_it_was(self):
        ledger = runs_ledger(
            ("439", "T1", hook_resolution(), settings_hook()),
            ("439", "T2", hook_resolution()),
        )
        report_out = cost_report(ledger, "439", run=1)
        self.assertEqual(report_out["run"]["ordinal"], 1)
        self.assertEqual(report_out["run"]["selected"], SELECTED_EXPLICIT)
        self.assertEqual(report_out["now"]["agent_invocations"], 1)
        self.assertIn("because it was asked for", "\n".join(render(report_out)))
        # By stamp as well as by ordinal, since the stamp is what the ledger holds.
        self.assertEqual(cost_report(ledger, "439", run="T1")["run"]["ordinal"], 1)
        self.assertEqual(cost_report(ledger, "439", run=2)["now"]["agent_invocations"], 0)

    def test_a_run_selector_that_matches_nothing_is_refused_rather_than_defaulted(self):
        """A silent fallback would label a report as a run it was not computed from."""
        ledger = runs_ledger(("439", "T1", hook_resolution()))
        for selector in (2, 0, -1, "T9"):
            with self.subTest(selector=selector), self.assertRaises(DecisionError):
                cost_report(ledger, "439", run=selector)

    def test_the_runs_of_a_version_are_ordered_by_append_and_never_by_timestamp(self):
        """Sorting the stamps would compare '+05:30' against 'Z'.

        The file is append-only, so its order IS the order the runs happened, and
        deriving 'latest' from string order would hand the word to the wrong run.
        """
        ledger = runs_ledger(
            ("439", "2026-08-02T22:00:00+05:30", hook_resolution()),
            ("439", "2026-08-02T17:00:00+00:00", hook_resolution()),
        )
        self.assertEqual(
            [run.recorded_at for run in ledger.runs_for("439")],
            ["2026-08-02T22:00:00+05:30", "2026-08-02T17:00:00+00:00"],
        )
        self.assertEqual(ledger.latest_run("439").recorded_at, "2026-08-02T17:00:00+00:00")
        self.assertEqual(sorted(["2026-08-02T22:00:00+05:30", "2026-08-02T17:00:00+00:00"])[-1],
                         "2026-08-02T22:00:00+05:30")  # what sorting would have chosen

    def test_two_versions_stamped_alike_are_two_runs_and_not_one(self):
        """The run key is (version, recorded_at), because the stamp is a caller's
        value rather than a clock reading and two ports can carry the same one."""
        ledger = runs_ledger(
            ("430", STAMP, hook_resolution(), settings_hook()),
            ("439", STAMP, hook_resolution()),
        )
        self.assertEqual(len(ledger.runs_for("430")), 1)
        self.assertEqual(len(ledger.runs_for("439")), 1)
        self.assertEqual(cost_report(ledger, "439")["now"]["hooks"], 1)
        self.assertEqual(cost_report(ledger, "439")["previous"]["agent_invocations"], 1)


class RealLedgerTests(AgentCostTestCase):
    """Against `manifest/agent_cost.jsonl` — the two-run data that exposed this.

    Copied into a temp directory rather than read in place: it is real recorded
    evidence of two real 439 attempts, and a test run may not be the reason it
    changes.
    """

    def setUp(self):
        super().setUp()
        self.assertTrue(
            REAL_LEDGER.exists(),
            f"{REAL_LEDGER} is committed recorded data and this test is about it; a "
            "skip here would be a test that cannot fail",
        )
        self.copy = self.tmp / "manifest" / "agent_cost.jsonl"
        self.copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REAL_LEDGER, self.copy)
        self.ledger = agent_cost_module.open_ledger(self.copy)

    def test_the_real_ledger_holds_two_439_runs_distinguished_by_their_stamp(self):
        """The claim the whole fix rests on, checked against the file itself."""
        runs = self.ledger.runs_for("439")
        self.assertGreaterEqual(len(runs), 2)
        self.assertTrue(all(isinstance(run, CostRun) for run in runs))
        self.assertEqual({run.of for run in runs}, {len(runs)})
        self.assertEqual([run.ordinal for run in runs], list(range(1, len(runs) + 1)))
        self.assertEqual([run.recorded_at for run in runs[:2]], [REAL_RUN_ONE, REAL_RUN_TWO])
        self.assertEqual([len(run.costs) for run in runs[:2]], [7, 7])
        self.assertEqual(sum(len(run.costs) for run in runs), len(self.ledger.costs_for("439")))
        for run in runs:
            self.assertEqual({cost.recorded_at for cost in run.costs}, {run.recorded_at})

    def test_the_real_report_counts_seven_hooks_and_two_agent_invocations(self):
        """What the defect printed: 14 hooks, 4 agent invocations, 7 hooks listed twice."""
        report_out = cost_report(self.ledger, "439", run=2)
        self.assertEqual(report_out["now"]["hooks"], 7)
        self.assertEqual(report_out["now"]["agent_invocations"], 2)
        self.assertEqual(
            report_out["now"]["routes"],
            {
                ROUTE_NOT_RESOLVED: 0,
                ROUTE_AGENT_PROPOSAL: 2,
                ROUTE_AGENT_SUPPLIER: 0,
                ROUTE_DETERMINISTIC_SUPPLIER: 0,
                ROUTE_MECHANICAL: 5,
                ROUTE_ALREADY_APPLIED: 0,
            },
        )
        listed = [entry["hook_id"] for entry in report_out["agent_hooks"]]
        self.assertEqual(len(listed), len(set(listed)))
        self.assertEqual(
            listed, ["install_settings_long_click", "install_settings_long_click_actionbar"]
        )
        # Every margin measured once. The defect listed each of them twice, which
        # reads as two runs agreeing about a fingerprint.
        subjects = [(entry["hook_id"], entry["subject"]) for entry in report_out["selectivity"]]
        self.assertEqual(len(subjects), len(set(subjects)))
        self.assertLess(report_out["now"]["hooks"], len(self.ledger.costs_for("439")))

    def test_the_failed_attempt_is_still_on_file_and_reportable(self):
        """It cost what it cost. The fix is a change of reading, not a deletion."""
        first = cost_report(self.ledger, "439", run=1)
        self.assertEqual(first["now"]["hooks"], 7)
        self.assertEqual(first["now"]["routes"][ROUTE_NOT_RESOLVED], 1)
        self.assertEqual(
            [entry["outcome"] for entry in first["agent_hooks"]], ["resolved", "needs_agent"]
        )
        # ...and it is the difference between the two attempts, which the summed
        # report could not show: the re-run resolved what the first one escalated.
        latest = cost_report(self.ledger, "439")
        self.assertEqual(latest["now"]["routes"][ROUTE_NOT_RESOLVED], 0)

    def test_the_cli_reports_one_run_and_lists_the_other(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = agent_cost_module.main(["report", "439", "--ledger", str(self.copy)])
        text = buffer.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("(7 hook(s) resolved against this decode)", text)
        self.assertIn("agent invocations: 2", text)
        self.assertIn(f"run 2 of 2 for 439, recorded {REAL_RUN_TWO}", text)
        self.assertIn(REAL_RUN_ONE, text)
        self.assertNotIn("agent invocations: 4", text)

    def test_the_versions_subcommand_shows_each_attempt_and_counts_one(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = agent_cost_module.main(["versions", "--ledger", str(self.copy)])
        text = buffer.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("7 hook(s)", text)
        self.assertNotIn("14 hook(s)", text)
        self.assertIn("[latest of 2 run(s)]", text)
        self.assertIn(f"run 1   {REAL_RUN_ONE}", text)
        self.assertIn(f"run 2   {REAL_RUN_TWO}", text)

    def test_the_cli_refuses_a_run_that_does_not_exist(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = agent_cost_module.main(["report", "439", "--run", "99", "--ledger", str(self.copy)])
        self.assertEqual(code, 2)
        self.assertIn("run 99 does not exist", err.getvalue())
        self.assertEqual(out.getvalue(), "")


# -------------------------------------------------------------------- the ledger


class CostLedgerTests(AgentCostTestCase):
    def setUp(self):
        super().setUp()
        self.ledger_path = self.tmp / "manifest" / "agent_cost.jsonl"

    def test_records_round_trip_through_the_file(self):
        written = update_ledger(
            report(hook_resolution(), settings_hook(supplied(guard_answered(), winner=GUARD))),
            VERSION,
            STAMP,
            path=self.ledger_path,
        )
        reloaded = CostLedger.load(self.ledger_path)
        self.assertEqual(list(reloaded.costs), list(written))
        self.assertEqual(reloaded.costs[1].attempts, written[1].attempts)
        self.assertEqual(reloaded.costs[1].selectivity, written[1].selectivity)

    def test_the_file_is_byte_identical_across_two_runs_of_one_report(self):
        one, two = self.tmp / "a" / "cost.jsonl", self.tmp / "b" / "cost.jsonl"
        source = report(hook_resolution(), settings_hook(supplied(guard_answered(), winner=GUARD)))
        update_ledger(source, VERSION, STAMP, path=one)
        update_ledger(source, VERSION, STAMP, path=two)
        self.assertEqual(one.read_bytes(), two.read_bytes())

    def test_it_appends_and_never_deduplicates(self):
        update_ledger(report(hook_resolution()), VERSION, STAMP, path=self.ledger_path)
        update_ledger(report(hook_resolution()), VERSION, STAMP, path=self.ledger_path)
        self.assertEqual(len(CostLedger.load(self.ledger_path).costs_for(VERSION)), 2)

    def test_versions_come_back_in_port_order_not_sorted(self):
        """Sorting would put '1000' before '439', and 'the previous port' is a
        fact about sequence rather than about string order."""
        ledger = ledger_of(("439", hook_resolution()), ("1000", hook_resolution()))
        self.assertEqual(ledger.versions, ("439", "1000"))
        self.assertEqual(ledger.previous_version("1000"), "439")
        self.assertIsNone(ledger.previous_version("439"))

    def test_an_unreadable_line_is_named_before_anything_is_appended(self):
        self.ledger_path.parent.mkdir(parents=True)
        self.ledger_path.write_text('{"schema_version": 1, "kind": "hook_cost"}\n', encoding="utf-8")
        with self.assertRaises(DecisionError) as caught:
            update_ledger(report(hook_resolution()), VERSION, STAMP, path=self.ledger_path)
        self.assertIn(f"{self.ledger_path}:1", str(caught.exception))

    def test_a_decision_memory_line_is_refused_rather_than_half_read(self):
        """The two files are different tables and must not be crossed."""
        self.ledger_path.parent.mkdir(parents=True)
        self.ledger_path.write_text(
            '{"schema_version": 1, "kind": "resolution", "record": {}}\n', encoding="utf-8"
        )
        with self.assertRaises(DecisionError) as caught:
            CostLedger.load(self.ledger_path)
        self.assertIn("unexpected record kind", str(caught.exception))

    def test_record_run_writes_both_halves_of_stage_ten(self):
        """One call site. A port that recorded what it learned and forgot what it
        paid is the state this whole module exists to leave behind."""
        memory_path = self.tmp / "manifest" / "decisions.jsonl"
        written = record_run(
            report(hook_resolution(), settings_hook(outcome=Outcome.NEEDS_AGENT, resolution=None)),
            VERSION,
            STAMP,
            memory_path=memory_path,
            ledger_path=self.ledger_path,
        )
        self.assertEqual([record.hook_id for record in written["resolutions"]], [REELS])
        self.assertEqual([cost.hook_id for cost in written["costs"]], [REELS, SETTINGS])
        self.assertTrue(memory_path.exists() and self.ledger_path.exists())

    def test_the_ledger_defaults_to_a_committed_path_and_is_never_touched_here(self):
        for function in (agent_cost_module.open_ledger, update_ledger):
            with self.subTest(function=function.__name__):
                self.assertIs(
                    inspect.signature(function).parameters["path"].default,
                    agent_cost_module.DEFAULT_LEDGER_PATH,
                )
        self.assertEqual(agent_cost_module.DEFAULT_LEDGER_PATH.parent, DEFAULT_MEMORY_PATH.parent)


# ------------------------------------------------------------------- mutations


class CostMutationTests(AgentCostTestCase):
    """The same guards, re-attacked from the direction a plausible rewrite takes."""

    def test_counting_only_resolved_hooks_would_report_the_expensive_port_as_free(self):
        """Mutation: mirror `resolution_records` and skip the escalations.

        It reads as consistency between the two halves of stage 10. What it does
        is delete the cost: a port where every settings hook escalated to an agent
        would record zero agent invocations and read as fully mechanised.
        """
        escalated = report(
            hook_resolution(REELS),
            settings_hook(outcome=Outcome.NEEDS_AGENT, resolution=None),
            settings_hook(outcome=Outcome.NEEDS_AGENT, resolution=None, evidence={"proposed": []}),
        )
        self.assertEqual([record.hook_id for record in resolution_records(escalated, VERSION, STAMP)], [REELS])
        costs = hook_costs(escalated, VERSION, STAMP)
        self.assertEqual(len(costs), 3)
        self.assertEqual(sum(1 for cost in costs if cost.needed_agent), 2)

    def test_treating_an_agent_supplier_as_a_supplier_would_hide_the_invocation(self):
        """Mutation: count "a supplier answered" and not WHICH supplier.

        The chain is written so the deterministic rule is tried first and the
        agent is the fallback, so "a supplier answered" is true in both cases. The
        distinction between them is the entire measurement.
        """
        deterministic = one_cost(
            settings_hook(supplied(guard_answered(), winner=GUARD), found_by="named", descriptor=SHELL)
        )
        fell_through = one_cost(
            settings_hook(
                supplied(guard_declined(), agent_answered(), winner=AGENT),
                found_by="named",
                descriptor=SHELL,
            )
        )
        self.assertEqual(deterministic.route, ROUTE_DETERMINISTIC_SUPPLIER)
        self.assertEqual(fell_through.route, ROUTE_AGENT_SUPPLIER)
        self.assertFalse(deterministic.needed_agent)
        self.assertTrue(fell_through.needed_agent)

    def test_reading_the_escalation_reason_as_prose_would_misfile_a_new_branch(self):
        """Mutation: decide what the agent is for by matching `reason` text.

        `resolve._classify` writes three different NEEDS_AGENT strings and a
        fourth would be filed as whichever it resembled. Every branch here keys on
        a field, and the unmatched case is recorded as `unspecified` rather than
        defaulted to a host.
        """
        invented = HookResolution(
            SETTINGS,
            Outcome.NEEDS_AGENT,
            reason="something new nobody has written a branch for",
            descriptor=None,
            searches=(HostSearch("named", (SHELL,), {"descriptor": SHELL}),),
            candidates=(),
        )
        cost = one_cost(invented)
        self.assertEqual(cost.agent_for, (NEED_UNSPECIFIED,))
        self.assertIn("needs a branch here", cost.note)

    def test_averaging_the_margin_would_hide_the_literal_that_is_dying(self):
        """Mutation: record the mean of `classes_per_literal` rather than the max.

        The max is the claim that co-location did the work — "the least selective
        literal alone would have left N". A mean moves when any literal moves and
        says nothing about the worst case, which is the case that fails.
        """
        cost = one_cost(hook_resolution())  # 4, 3, 2 -> co-located 1
        self.assertEqual(cost.selectivity[0].candidates, 4)
        self.assertNotEqual(cost.selectivity[0].candidates, 3)
        self.assertEqual(cost.selectivity[0].detail[LITERALS[2]], 2)

    def test_a_supplier_declining_silently_would_look_exactly_like_a_new_version(self):
        """Mutation: record only the winning supplier, dropping `attempts`.

        `SupplyOutcome.attempts` exists precisely so a gate sees the deterministic
        rule was tried. Dropping it leaves "an agent answered", which is what the
        pipeline reported before this module and is indistinguishable from a
        version that genuinely moved.
        """
        cost = one_cost(
            settings_hook(
                supplied(guard_declined(STAGE_DRAWABLE_ABSENT), agent_answered(), winner=AGENT),
                found_by="named",
                descriptor=SHELL,
            )
        )
        self.assertEqual(len(cost.attempts), 2)
        self.assertEqual(cost.deterministic_declines[0].stage, STAGE_DRAWABLE_ABSENT)
        self.assertIn(STAGE_DRAWABLE_ABSENT, stored(cost))

    def test_stamping_inside_the_module_would_break_the_replay_it_is_written_for(self):
        first = one_cost(hook_resolution())
        second = one_cost(hook_resolution())
        self.assertEqual(stored(first), stored(second))
        self.assertNotIn("now(", inspect.getsource(agent_cost_module))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

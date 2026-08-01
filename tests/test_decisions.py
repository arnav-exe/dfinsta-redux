"""Tests for decision memory — the store that hands on a technique instead of an answer.

The module under test exists because three agents independently rediscovered the
same route into the profile settings control, and because the same store, built
carelessly, would have been worse than nothing: obfuscated names are recycled, so
a descriptor-keyed cross-version lookup returns a confident wrong answer rather
than a miss. Every test here is written from one of two rules rather than from
the code's shape.

    1. Nothing may leave this store in a shape a caller can apply. A recalled
       descriptor is not a `str`, does not print as one, appears in no report,
       and comes out only against a typed re-verification.

    2. A stored decision is reusable only while its semantic feature identity,
       delivery mechanism, evidence fingerprint and policy revision remain
       compatible — and unknown is never compatible. `docs/ADK_PIPELINE_PLAN.md`
       adds the constraint that makes this hard: memory "must not permanently
       suppress reassessment", so the answer to a stale record is a route to
       re-derive it, not silence.

The tests that matter most are the ones that would still fail if someone rewrote
the module: a blank dimension refuses reuse; a miss is reported even when the
predicate calls it incompatible; two versions never merge under one descriptor;
and nothing here reads a clock.

`MutationTests` adds no coverage. It re-attacks three guards that already have
positive tests, from the direction a broken implementation would take, and each
docstring says what would reach a device if that guard were removed.
"""

import inspect
import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from io import StringIO
from pathlib import Path
from unittest import mock

from dfinsta_pipeline.contracts import canonical_json
from dfinsta_pipeline.decisions import (
    API_PATH_LITERAL,
    DEFAULT_MEMORY_PATH,
    DRAWABLE_ID,
    DRAWABLE_NAME,
    FORBIDDEN_SIGNAL,
    REUSE_DIMENSIONS,
    SCHEMA_VERSION,
    SETTINGS_COMPATIBILITY,
    SETTINGS_ROUTE,
    SETTINGS_SIGNALS,
    SETTINGS_TECHNIQUE,
    STABLE_NAMED_TYPE,
    STRUCTURAL_SHAPE,
    Compatibility,
    Context,
    DecisionError,
    DecisionMemory,
    Detector,
    Hint,
    Key,
    Miss,
    MissStatus,
    RecalledDescriptor,
    RecordKind,
    Resolution,
    Reusability,
    Reverification,
    Route,
    Step,
    SurvivalRate,
    fingerprint_of,
    main,
    precedence,
    reusable,
    seed_records,
    seeded_memory,
    stamped,
)
from dfinsta_pipeline import decisions as decisions_module


# --------------------------------------------------------------------- fixture

HOOK = "install_settings_long_click"
DESCRIPTOR = "LX/0DnT;"
STAMP = "2026-08-01T12:00:00Z"

# A fully known compatibility, so a test can blank exactly one dimension and be
# certain that dimension is the only thing under examination.
KNOWN = Compatibility(
    semantic_feature_identity="profile_options_long_press.settings_dialog",
    delivery_mechanism="ui_attach:view_long_click_listener",
    evidence_fingerprint="ef-1",
    policy_revision="r1",
)

# The keys `dfinsta_pipeline.proposals.Proposal.as_operation` produces. Nothing in
# this module may emit a mapping carrying them: that shape is directly appliable.
APPLIER_KEYS = frozenset(
    {"id", "descriptor", "mode", "anchor", "marker", "payload", "expected_anchor_count"}
)


def step(line: int = 42, file: str = "smali_classes6/X/0DnT.smali") -> Step:
    return Step(
        action="pin the control by drawable name",
        file=file,
        line=line,
        finding="the id arrives as data, so the name is the only stable handle",
    )


def resolution(
    hook_id: str = HOOK,
    version: str = "439",
    descriptor: str = DESCRIPTOR,
    *,
    compatibility: Compatibility | None = None,
    **extra: object,
) -> Resolution:
    """A well-formed resolution, with a one-step chain unless a test wants more."""
    fields: dict[str, object] = {
        "smali_path": "smali_classes6/X/0DnT.smali",
        "technique": "enter at the fragment, gate on the own-profile override",
        "chain": (step(),),
        "signals": (STABLE_NAMED_TYPE, DRAWABLE_NAME),
    }
    fields.update(extra)
    return Resolution(
        hook_id=hook_id,
        version=version,
        host=RecalledDescriptor(hook_id, version, descriptor),
        compatibility=KNOWN if compatibility is None else compatibility,
        **fields,  # type: ignore[arg-type]
    )


def miss(
    hook_id: str = HOOK,
    version: str = "430",
    status: MissStatus = MissStatus.CONFIRMED,
    detector: Detector = Detector.DEVICE_SESSION,
    **extra: object,
) -> Miss:
    fields: dict[str, object] = {
        "summary": "applied cleanly, dead at runtime: the other implementation was live"
    }
    fields.update(extra)
    return Miss(
        hook_id=hook_id,
        version=version,
        status=status,
        detector=detector,
        **fields,  # type: ignore[arg-type]
    )


def rate(signal: str, value: float, **extra: object) -> SurvivalRate:
    return SurvivalRate(
        from_version="430",
        to_version="439",
        signal=signal,
        rate=value,
        measured_by="tools/indexer/build_index.py",
        **extra,  # type: ignore[arg-type]
    )


def ack(version: str = "440") -> Reverification:
    return Reverification(
        target_version=version,
        target_decode="work/440-explore/stock-440",
        acknowledged_by="resolve.host_search",
    )


def as_bytes(value: object) -> bytes:
    """Canonical bytes for an exact comparison, not a dict-equality one."""
    return json.dumps(value, sort_keys=True).encode("utf-8")


class MemoryTestCase(unittest.TestCase):
    """A temp dir, a memory, and the two contexts every reuse test needs."""

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        self.path = self.tmp / "run" / "decisions.jsonl"

    def memory(self, *records: object, path: Path | None = None) -> DecisionMemory:
        memory = DecisionMemory(path)
        for record in records:
            memory.record(record)
        return memory

    def matching(self, version: str = "440") -> Context:
        """A context compatible with :data:`KNOWN` in all four dimensions."""
        return Context(version=version, compatibility=KNOWN)

    def blind(self, version: str = "440") -> Context:
        """A context that states nothing — the shape a lazy caller passes."""
        return Context(version=version)


# ------------------------------------------------------------------ round trip


class RoundTripTests(MemoryTestCase):
    """Every record type must survive the file, because the file is the memory.

    A record that changes on reload would mean the answer at a gate depended on
    whether the previous run crashed.
    """

    def test_a_resolution_round_trips_with_every_field_intact(self):
        record = stamped(resolution(), STAMP)
        self.memory(record, path=self.path)
        reloaded = DecisionMemory.load(self.path)
        self.assertEqual(reloaded.resolutions, (record,))
        self.assertEqual(reloaded.resolutions[0].chain, record.chain)
        self.assertEqual(reloaded.resolutions[0].compatibility, KNOWN)
        self.assertEqual(reloaded.resolutions[0].recorded_at, STAMP)

    def test_a_resolutions_recalled_host_survives_the_round_trip(self):
        # The host is the one field wrapped in a type of its own; a decoder that
        # dropped the wrapper would put a bare string back into circulation.
        record = resolution()
        self.memory(record, path=self.path)
        host = DecisionMemory.load(self.path).resolutions[0].host
        self.assertIsInstance(host, RecalledDescriptor)
        self.assertEqual(host, record.host)
        self.assertEqual(host.reverify(ack()), DESCRIPTOR)

    def test_a_miss_round_trips_with_its_status_and_detector(self):
        """`suspected` surviving as `suspected` is the whole point of the field.

        Reloading a suspected miss as confirmed would let a static argument
        permanently retire a hook; reloading a confirmed one as suspected would
        let a known-dead hook ship again.
        """
        record = stamped(
            miss(status=MissStatus.SUSPECTED, detector=Detector.ADVERSARIAL_VERIFIER,
                 detected_at="2026-08-01", detail={"argument": ["never invoked"]}),
            STAMP,
        )
        self.memory(record, path=self.path)
        reloaded = DecisionMemory.load(self.path).misses[0]
        self.assertEqual(reloaded, record)
        self.assertIs(reloaded.status, MissStatus.SUSPECTED)
        self.assertIs(reloaded.detector, Detector.ADVERSARIAL_VERIFIER)
        self.assertEqual(reloaded.detected_at, "2026-08-01")
        self.assertEqual(reloaded.detail, {"argument": ["never invoked"]})

    def test_a_survival_rate_round_trips_with_its_counts(self):
        record = rate(DRAWABLE_ID, 0.009, survived=103, total=11737)
        self.memory(record, path=self.path)
        reloaded = DecisionMemory.load(self.path).survival[0]
        self.assertEqual(reloaded, record)
        self.assertEqual((reloaded.survived, reloaded.total), (103, 11737))

    def test_all_three_kinds_round_trip_together_in_one_file(self):
        records = (resolution(), miss(), rate(API_PATH_LITERAL, 0.939))
        self.memory(*records, path=self.path)
        reloaded = DecisionMemory.load(self.path)
        self.assertEqual(reloaded.resolutions + reloaded.misses + reloaded.survival, records)

    def test_each_record_occupies_exactly_one_line(self):
        # JSONL only stays greppable and diffable if nothing wraps.
        self.memory(resolution(), miss(), rate(DRAWABLE_NAME, 0.988), path=self.path)
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)
        kinds = [json.loads(line)["kind"] for line in lines]
        self.assertEqual(kinds, [k.value for k in RecordKind])
        for line in lines:
            with self.subTest(line=line[:40]):
                self.assertEqual(json.loads(line)["schema_version"], SCHEMA_VERSION)

    def test_a_missing_file_loads_as_an_empty_memory(self):
        """A first run has no file. That must not be an error, and must not be a pass.

        An empty memory answers "nothing known", which is the honest answer, and
        `recall` says so rather than returning a hint built from nothing.
        """
        memory = DecisionMemory.load(self.tmp / "never-written.jsonl")
        self.assertEqual((memory.resolutions, memory.misses, memory.survival), ((), (), ()))
        self.assertIs(memory.recall(HOOK)["known"], False)

    def test_the_parent_directory_is_created_on_demand(self):
        self.assertFalse(self.path.parent.exists())
        self.memory(resolution(), path=self.path)
        self.assertTrue(self.path.exists())

    def test_a_memory_with_no_path_writes_nothing(self):
        self.memory(resolution(), miss())
        self.assertEqual(list(self.tmp.iterdir()), [])


# ------------------------------------------------------------------ the rule


class ReuseRuleTests(MemoryTestCase):
    """The load-bearing predicate, one incompatibility at a time.

    `docs/ADK_PIPELINE_PLAN.md`: "A decision is reusable only while its semantic
    feature identity, delivery mechanism, evidence fingerprint, and policy
    revision remain compatible." Four dimensions, four ways to be wrong, plus the
    case that matters most in practice — nobody said.
    """

    def test_a_record_compatible_in_all_four_dimensions_is_reusable(self):
        # The positive case has to hold, or the module is merely a very safe way
        # of never remembering anything.
        assessment = reusable(resolution(), self.matching())
        self.assertIs(assessment.reusable, True)
        self.assertEqual(assessment.changed, ())
        self.assertEqual(assessment.unknown, ())
        self.assertEqual(assessment.blocking, ())

    def test_each_of_the_four_dimensions_refuses_reuse_when_it_changes(self):
        for dimension in REUSE_DIMENSIONS:
            with self.subTest(dimension=dimension):
                current = Context("440", replace(KNOWN, **{dimension: "something-else"}))
                assessment = reusable(resolution(), current)
                self.assertIs(assessment.reusable, False)
                # Named precisely, so the escalation says which thing moved
                # rather than "incompatible".
                self.assertEqual(assessment.changed, (dimension,))
                self.assertEqual(assessment.unknown, ())
                self.assertTrue(any(dimension in reason for reason in assessment.reasons))

    def test_each_of_the_four_dimensions_refuses_reuse_when_the_stored_side_is_blank(self):
        """An old record that never stated a dimension cannot be assessed.

        Records written before a dimension existed are exactly the ones most
        likely to be stale, so "the field is missing" must not read as "the field
        matches".
        """
        for dimension in REUSE_DIMENSIONS:
            with self.subTest(dimension=dimension):
                stored = resolution(compatibility=replace(KNOWN, **{dimension: ""}))
                assessment = reusable(stored, self.matching())
                self.assertIs(assessment.reusable, False)
                self.assertEqual(assessment.unknown, (dimension,))
                self.assertEqual(assessment.changed, ())

    def test_each_of_the_four_dimensions_refuses_reuse_when_the_current_side_is_blank(self):
        # The caller that cannot say what this run is gets nothing, however
        # complete the stored record happens to be.
        for dimension in REUSE_DIMENSIONS:
            with self.subTest(dimension=dimension):
                current = Context("440", replace(KNOWN, **{dimension: "  "}))
                assessment = reusable(resolution(), current)
                self.assertIs(assessment.reusable, False)
                self.assertEqual(assessment.unknown, (dimension,))

    def test_a_context_that_states_nothing_refuses_on_all_four(self):
        assessment = reusable(resolution(), self.blind())
        self.assertIs(assessment.reusable, False)
        self.assertEqual(assessment.unknown, REUSE_DIMENSIONS)
        self.assertEqual(assessment.blocking, REUSE_DIMENSIONS)

    def test_the_result_is_falsy_when_it_refuses(self):
        """`if reusable(record, ctx):` must not silently pass on a refusal.

        Without `__bool__` the result object is always truthy, and the most
        natural way anyone would call the predicate would wave every stale
        record through — the precise outcome it exists to prevent.
        """
        self.assertFalse(reusable(resolution(), self.blind()))
        self.assertTrue(reusable(resolution(), self.matching()))

    def test_reuse_never_depends_on_the_version_alone(self):
        """Compatibility, not recency, decides. Both directions are tested.

        A same-version record with a changed policy revision must be refused, and
        a cross-version record with all four intact must be offered — otherwise
        the store is a version cache, which is the thing it must not be.
        """
        same_version = reusable(resolution(version="440"), self.matching("440"))
        self.assertIs(same_version.reusable, True)
        stale_policy = reusable(
            resolution(version="440", compatibility=replace(KNOWN, policy_revision="r2")),
            self.matching("440"),
        )
        self.assertIs(stale_policy.reusable, False)
        across = reusable(resolution(version="430"), self.matching("439"))
        self.assertIs(across.reusable, True)

    def test_a_reusable_record_is_still_only_a_hint(self):
        # "Reusable" is permission to offer a route, never permission to apply a
        # descriptor. The reason text has to say so, because it is what a human
        # reads at the gate.
        assessment = reusable(resolution(), self.matching())
        self.assertTrue(any("re-verified" in reason for reason in assessment.reasons))

    def test_the_predicate_refuses_a_survival_rate(self):
        # A measurement has no decision in it to reuse; letting one through would
        # compare a rate's absent compatibility and call it unknown, which reads
        # like a near miss rather than a category error.
        with self.assertRaises(DecisionError):
            reusable(rate(DRAWABLE_NAME, 0.988), self.matching())

    def test_the_predicate_refuses_a_bare_version_string_as_context(self):
        with self.assertRaises(DecisionError):
            reusable(resolution(), "440")  # type: ignore[arg-type]

    def test_a_context_refuses_a_plain_mapping_of_dimensions(self):
        """A dict would let a misspelled dimension read as 'unknown' instead of failing.

        Unknown is a safe verdict, so the mistake would never be noticed: the
        run would simply stop reusing anything and nobody would learn why.
        """
        with self.assertRaises(DecisionError):
            Context("440", {"policy_revision": "r1"})  # type: ignore[arg-type]

    def test_reasons_are_reported_in_the_declared_dimension_order(self):
        # Two workers must report the same record identically, or the serialised
        # assessment is not comparable across runs.
        current = Context("440", Compatibility(policy_revision="r1"))
        assessment = reusable(resolution(), current)
        self.assertEqual(
            assessment.unknown,
            tuple(name for name in REUSE_DIMENSIONS if name != "policy_revision"),
        )


# --------------------------------------------------------- descriptor escape


class DescriptorContainmentTests(MemoryTestCase):
    """The descriptor must not be obtainable in a shape anything can apply.

    `LX/05t2;` is a 1990-line Reels builder in 430 and an unrelated 596-line
    class in 439. A descriptor that escapes into an operation produces a patch on
    the wrong class that assembles, passes static verification, and does nothing
    — the failure mode this project has shipped three times.
    """

    def test_a_recalled_descriptor_is_not_a_string(self):
        host = resolution().host
        self.assertNotIsInstance(host, str)

    def test_a_recalled_descriptor_does_not_print_as_the_descriptor(self):
        """Formatting it into a payload template must not produce a usable answer.

        `f"{host}"` is the single easiest way to defeat a wrapper type, so the
        text of `__str__` and `__repr__` is part of the guard, not cosmetics.
        """
        host = resolution().host
        for rendering in (str(host), repr(host), f"{host}", "{}".format(host)):
            with self.subTest(rendering=rendering):
                self.assertNotIn(DESCRIPTOR, rendering)
                self.assertIn("re-verified", rendering)

    def test_the_descriptor_comes_out_only_against_a_typed_acknowledgement(self):
        host = resolution().host
        for pretender in (None, True, "I will re-verify", {"target_version": "440"}):
            with self.subTest(pretender=pretender):
                with self.assertRaises(DecisionError):
                    host.reverify(pretender)  # type: ignore[arg-type]
        self.assertEqual(host.reverify(ack()), DESCRIPTOR)

    def test_an_acknowledgement_must_name_a_decode_and_a_signer(self):
        # "I acknowledge" with nothing filled in is not an acknowledgement; the
        # fields are what make it a statement someone can be held to.
        for missing in ("target_version", "target_decode", "acknowledged_by"):
            with self.subTest(missing=missing):
                fields = {
                    "target_version": "440",
                    "target_decode": "work/440",
                    "acknowledged_by": "resolve",
                    missing: "   ",
                }
                with self.assertRaises(DecisionError):
                    Reverification(**fields)

    def test_an_acknowledgement_for_another_version_is_refused(self):
        """An acknowledgement copied from the previous run acknowledges nothing.

        This is the realistic way the guard gets defeated: the constant is left
        at "439" while the run ports to 440, and the descriptor is released
        against a promise about the wrong decode.
        """
        host = resolution().host
        with self.assertRaises(DecisionError) as caught:
            host.reverify_for("440", ack("439"))
        self.assertIn("440", str(caught.exception))
        self.assertEqual(host.reverify_for("440", ack("440")), DESCRIPTOR)

    def test_a_hint_releases_the_host_only_for_its_own_target(self):
        memory = self.memory(resolution())
        hint = memory.hint(HOOK, self.matching("440"))
        self.assertIsNotNone(hint)
        with self.assertRaises(DecisionError):
            hint.reverified_host(ack("439"))
        self.assertEqual(hint.reverified_host(ack("440")), DESCRIPTOR)

    def test_a_hint_cannot_be_built_claiming_no_re_verification_is_needed(self):
        memory = self.memory(resolution())
        hint = memory.hint(HOOK, self.matching())
        self.assertIs(hint.must_reverify, True)
        with self.assertRaises(DecisionError):
            replace(hint, must_reverify=False)

    def test_a_hint_cannot_carry_a_bare_string_host(self):
        memory = self.memory(resolution())
        hint = memory.hint(HOOK, self.matching())
        with self.assertRaises(DecisionError):
            replace(hint, host=DESCRIPTOR)

    def test_no_report_from_the_query_surface_contains_the_descriptor(self):
        """The report a human or an agent reads must not carry the stale answer.

        The route is the useful part and stays; the descriptor is the part that
        misleads, and a JSON report is the obvious place a script would scrape it
        from.
        """
        memory = self.memory(resolution(), miss(version="439"))
        hint_memory = self.memory(resolution())
        reports = {
            "recall": memory.recall(HOOK, self.matching()),
            "recall_no_context": memory.recall(HOOK),
            "report": memory.report(),
            "by_key": memory.by_key(Key(HOOK, "439")),
            "route": memory.routes_for(HOOK)[0].to_dict(),
            "hint": hint_memory.hint(HOOK, self.matching()).to_dict(),
        }
        for name, report in reports.items():
            with self.subTest(report=name):
                self.assertNotIn(DESCRIPTOR, canonical_json(report))

    def test_reporting_a_conflict_does_not_become_the_way_a_descriptor_gets_out(self):
        """Two answers under one key must be reportable without printing either.

        The conflict path is the one place a report has to distinguish
        descriptors from each other, which makes it the obvious place to quote
        them. It reports fingerprints instead, so "memory holds two answers here"
        costs nothing.
        """
        memory = self.memory(
            resolution(descriptor="LX/0DnT;"), resolution(descriptor="LX/04tC;")
        )
        report = memory.recall(HOOK, self.matching())
        self.assertEqual(len(report["resolutions"][0]["conflicting_answers"]), 2)
        for text in (canonical_json(report), canonical_json(memory.by_key(Key(HOOK, "439")))):
            self.assertNotIn("LX/0DnT;", text)
            self.assertNotIn("LX/04tC;", text)

    def test_a_hosts_fingerprint_tells_two_answers_apart_without_revealing_either(self):
        first = RecalledDescriptor(HOOK, "439", "LX/0DnT;")
        second = RecalledDescriptor(HOOK, "439", "LX/04tC;")
        self.assertNotEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(first.fingerprint, RecalledDescriptor(HOOK, "440", "LX/0DnT;").fingerprint)
        self.assertNotIn("LX/", first.fingerprint)

    def test_the_stored_record_does_carry_the_descriptor_but_not_under_that_key(self):
        """Persistence must keep it — a record without it is not a record.

        The boundary is deliberate and worth pinning: the file has the
        descriptor, the query surface does not, and even the storage form avoids
        the key name `descriptor` so a stored dict cannot be splatted into an
        operation and land in the right field.
        """
        stored = resolution().to_dict()
        self.assertEqual(stored["host"]["descriptor_pending_reverification"], DESCRIPTOR)
        self.assertNotIn("descriptor", stored)
        self.assertNotIn("descriptor", stored["host"])

    def test_nothing_here_produces_an_applier_shaped_operation(self):
        """No mapping from this module may carry the applier's field names.

        `proposals.Proposal.as_operation` is the shape the applier consumes. A
        mapping from decision memory with those keys could be passed straight to
        it, which is exactly the "recall and apply" path that must not exist.
        """
        memory = self.memory(resolution())
        hint = memory.hint(HOOK, self.matching())
        mappings = [
            hint.to_dict(),
            hint.route.to_dict(),
            memory.recall(HOOK, self.matching()),
            memory.report(),
            memory.by_key(Key(HOOK, "439")),
        ]
        for mapping in mappings:
            with self.subTest(mapping=sorted(mapping)[:3]):
                self.assertFalse(APPLIER_KEYS & set(mapping))
        # And no method on the hint is named as though it produced one.
        names = [name for name in dir(hint) if not name.startswith("_")]
        self.assertFalse([name for name in names if "operation" in name or "apply" in name])

    def test_the_route_carries_the_technique_and_no_host_at_all(self):
        # The route is the primary product: it stays useful after the answer has
        # rotted, which is the difference between memory that helps and memory
        # that lies.
        route = self.memory(resolution()).routes_for(HOOK)[0]
        self.assertIsInstance(route, Route)
        self.assertEqual(route.chain, (step(),))
        self.assertNotIn("host", route.to_dict())
        self.assertNotIn(DESCRIPTOR, canonical_json(route.to_dict()))

    def test_a_resolution_refuses_a_plain_string_host(self):
        with self.assertRaises(DecisionError):
            Resolution(
                hook_id=HOOK,
                version="439",
                host=DESCRIPTOR,  # type: ignore[arg-type]
                smali_path="smali_classes6/X/0DnT.smali",
                technique="t",
                chain=(step(),),
            )


# ---------------------------------------------------------------- the keying


class KeyingTests(MemoryTestCase):
    """Records are keyed by (hook_id, version). A descriptor is not a key here.

    `LX/05t2` exists in both 430 and 439 and names a different class in each, so
    a descriptor-keyed record answers with confidence and is wrong. The whole
    point of the key is that a question about the wrong version returns nothing.
    """

    def test_the_same_descriptor_in_two_versions_stays_two_records(self):
        memory = self.memory(
            resolution(version="430", descriptor="LX/05t2;"),
            resolution(version="439", descriptor="LX/05t2;"),
        )
        self.assertEqual(len(memory.resolutions_for(HOOK)), 2)
        self.assertEqual(len(memory.resolutions_for(HOOK, "430")), 1)
        self.assertEqual(memory.resolutions_for(HOOK, "430")[0].version, "430")
        self.assertEqual(memory.resolutions_for(HOOK, "439")[0].version, "439")

    def test_a_record_whose_key_and_host_disagree_is_refused(self):
        """The key and the wrapped descriptor must name the same hook and version.

        A record keyed 439 whose host remembers 430 would answer the 439 question
        with the 430 answer, which is the confident wrong answer in its purest
        form.
        """
        with self.assertRaises(DecisionError):
            Resolution(
                hook_id=HOOK,
                version="439",
                host=RecalledDescriptor(HOOK, "430", DESCRIPTOR),
                smali_path="smali_classes6/X/0DnT.smali",
                technique="t",
                chain=(step(),),
            )
        with self.assertRaises(DecisionError):
            Resolution(
                hook_id=HOOK,
                version="439",
                host=RecalledDescriptor("another_hook", "439", DESCRIPTOR),
                smali_path="smali_classes6/X/0DnT.smali",
                technique="t",
                chain=(step(),),
            )

    def test_a_cross_version_descriptor_lookup_is_refused_by_name(self):
        memory = self.memory(resolution())
        with self.assertRaises(DecisionError) as caught:
            memory.lookup_by_descriptor("LX/05t2;")
        message = str(caught.exception)
        self.assertIn("LX/05t2", message)
        self.assertIn("recycled", message)

    def test_no_query_method_accepts_a_descriptor(self):
        """The refusal must be the only door, or it is decoration.

        A helper that quietly took `descriptor=` would reintroduce the join with
        none of the noise, so the absence is asserted over the whole public
        surface rather than trusted.
        """
        for name, method in inspect.getmembers(DecisionMemory, inspect.isfunction):
            if name.startswith("_") or name == "lookup_by_descriptor":
                continue
            with self.subTest(method=name):
                parameters = set(inspect.signature(method).parameters)
                self.assertFalse(parameters & {"descriptor", "host", "class_name"})

    def test_by_key_refuses_half_a_key(self):
        memory = self.memory(resolution())
        for wrong in (HOOK, ("install", "439"), None):
            with self.subTest(key=wrong):
                with self.assertRaises(DecisionError):
                    memory.by_key(wrong)  # type: ignore[arg-type]

    def test_by_key_returns_only_what_is_filed_under_that_exact_key(self):
        memory = self.memory(
            resolution(version="430"),
            resolution(version="439"),
            miss(version="430"),
            miss(version="439"),
        )
        under_430 = memory.by_key(Key(HOOK, "430"))
        self.assertEqual(len(under_430["resolutions"]), 1)
        self.assertEqual(len(under_430["misses"]), 1)
        self.assertEqual(under_430["resolutions"][0]["version"], "430")
        self.assertEqual(under_430["misses"][0]["version"], "430")

    def test_a_key_needs_both_halves(self):
        for hook_id, version in ((HOOK, ""), ("", "439"), ("  ", "  ")):
            with self.subTest(hook_id=hook_id, version=version):
                with self.assertRaises(DecisionError):
                    Key(hook_id, version)

    def test_records_of_different_hooks_never_merge(self):
        memory = self.memory(miss(hook_id="a", version="439"), miss(hook_id="b", version="439"))
        self.assertEqual(len(memory.misses_for("a")), 1)
        self.assertEqual(len(memory.misses_for("b")), 1)
        self.assertEqual(memory.hooks, ("a", "b"))

    def test_no_record_type_exists_for_a_cross_version_class_diff(self):
        """The store holds three tables, and a class-level diff is not one of them.

        A descriptor-keyed cross-version record is worse than nothing: it returns
        a confident wrong answer where an absent record would return a miss. The
        assertion is on the record kinds, so adding one would fail here first.
        """
        self.assertEqual(
            {kind.value for kind in RecordKind}, {"resolution", "miss", "survival"}
        )
        memory = self.memory()
        with self.assertRaises(DecisionError):
            memory.record({"from": "LX/06X7;", "to": "LX/0Di2;"})  # type: ignore[arg-type]


# ------------------------------------------------------------- append-only


class AppendOnlyTests(MemoryTestCase):
    """Nothing rewrites a line, so a crashed run keeps everything it had learned."""

    def test_recording_twice_appends_two_lines_and_touches_neither(self):
        memory = self.memory(path=self.path)
        memory.record(resolution())
        first = self.path.read_text(encoding="utf-8")
        memory.record(miss())
        second = self.path.read_text(encoding="utf-8")
        self.assertEqual(len(first.splitlines()), 1)
        self.assertEqual(len(second.splitlines()), 2)
        self.assertTrue(second.startswith(first))

    def test_a_second_record_under_the_same_key_is_appended_not_replaced(self):
        """Both answers must survive, because two answers is the interesting state.

        A store that overwrote would silently pick the most recent of two
        contradictory memories and present it with full confidence.
        """
        memory = self.memory(path=self.path)
        memory.record(resolution(descriptor="LX/0DnT;"))
        memory.record(resolution(descriptor="LX/04tC;"))
        self.assertEqual(len(self.path.read_text(encoding="utf-8").splitlines()), 2)
        self.assertEqual(len(DecisionMemory.load(self.path).resolutions_for(HOOK, "439")), 2)

    def test_two_answers_for_one_key_are_reported_as_a_conflict(self):
        memory = self.memory(
            resolution(descriptor="LX/0DnT;"), resolution(descriptor="LX/04tC;")
        )
        self.assertEqual(len(memory.conflicts_for(HOOK, "439")), 2)
        # And no hint is offered from a key memory disagrees with itself about.
        self.assertIsNone(memory.hint(HOOK, self.matching()))

    def test_one_answer_recorded_twice_is_not_a_conflict(self):
        # Re-recording the same finding is a normal re-run, not a contradiction.
        memory = self.memory(resolution(), resolution())
        self.assertEqual(memory.conflicts_for(HOOK, "439"), ())
        self.assertIsNotNone(memory.hint(HOOK, self.matching()))

    def test_records_are_frozen(self):
        # Append-only is meaningless if a caller can edit a record in place after
        # the line has been written.
        record = resolution()
        with self.assertRaises(FrozenInstanceError):
            record.version = "440"  # type: ignore[misc]

    def test_the_public_collections_are_copies(self):
        memory = self.memory(resolution())
        snapshot = memory.resolutions
        self.assertIsInstance(snapshot, tuple)
        memory.record(resolution(version="440"))
        self.assertEqual(len(snapshot), 1)


# --------------------------------------------------------------- bad storage


class MalformedLineTests(MemoryTestCase):
    """A bad line must name itself. Silent skipping loses a miss without saying so."""

    def write(self, *lines: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")

    def good(self, version: str = "439") -> str:
        return canonical_json(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "resolution",
                "record": resolution(version=version).to_dict(),
            }
        )

    def test_a_line_that_is_not_json_names_its_line_number(self):
        self.write(self.good(), "{not json", self.good("440"))
        with self.assertRaises(DecisionError) as caught:
            DecisionMemory.load(self.path)
        self.assertIn(f"{self.path}:2", str(caught.exception))

    def test_an_unknown_record_kind_names_its_line_number(self):
        """A kind nobody recognises must stop the load, not be skipped.

        Skipping would turn a future record type — or a hand-edited class diff —
        into an invisible omission, and the store would report less than it
        holds.
        """
        self.write(
            self.good(),
            canonical_json({"schema_version": SCHEMA_VERSION, "kind": "class_diff", "record": {}}),
        )
        with self.assertRaises(DecisionError) as caught:
            DecisionMemory.load(self.path)
        self.assertIn(f"{self.path}:2", str(caught.exception))

    def test_an_unsupported_schema_version_names_its_line_number(self):
        self.write(
            canonical_json(
                {"schema_version": 99, "kind": "miss", "record": miss().to_dict()}
            )
        )
        with self.assertRaises(DecisionError) as caught:
            DecisionMemory.load(self.path)
        self.assertIn(f"{self.path}:1", str(caught.exception))

    def test_a_record_missing_a_field_names_its_line_number(self):
        body = resolution().to_dict()
        del body["technique"]
        self.write(
            self.good(),
            self.good("440"),
            canonical_json({"schema_version": SCHEMA_VERSION, "kind": "resolution", "record": body}),
        )
        with self.assertRaises(DecisionError) as caught:
            DecisionMemory.load(self.path)
        self.assertIn(f"{self.path}:3", str(caught.exception))

    def test_a_stored_record_is_revalidated_not_merely_decoded(self):
        """A hand-edited file must not re-enter memory unchecked.

        `load` bypasses `record`, so every constructor guard has to fire on the
        way in too — otherwise the guards only protect the run that happened to
        create the record.
        """
        body = resolution().to_dict()
        body["chain"] = []
        self.write(
            canonical_json({"schema_version": SCHEMA_VERSION, "kind": "resolution", "record": body})
        )
        with self.assertRaises(DecisionError) as caught:
            DecisionMemory.load(self.path)
        self.assertIn(f"{self.path}:1", str(caught.exception))

    def test_blank_lines_are_skipped(self):
        self.write(self.good(), "", "   ", self.good("440"))
        self.assertEqual(len(DecisionMemory.load(self.path).resolutions), 2)


# ------------------------------------------------------------------- records


class RecordValidationTests(MemoryTestCase):
    """Each record refuses the shapes that would make it useless or misleading."""

    def test_a_resolution_without_a_chain_is_refused(self):
        """The chain is the reusable part; the descriptor is the disposable part.

        A record with only a descriptor is precisely the stale answer this store
        exists to avoid, so it cannot be created at all.
        """
        with self.assertRaises(DecisionError) as caught:
            Resolution(
                hook_id=HOOK,
                version="439",
                host=RecalledDescriptor(HOOK, "439", DESCRIPTOR),
                smali_path="p",
                technique="t",
                chain=(),
            )
        self.assertIn("chain", str(caught.exception))

    def test_a_chain_step_needs_a_real_line(self):
        # Zero is a harness sentinel, not a place to look, and a chain nobody can
        # follow is documentation rather than evidence.
        for line in (0, -1):
            with self.subTest(line=line):
                with self.assertRaises(DecisionError):
                    step(line=line)

    def test_a_chain_step_refuses_an_absolute_path(self):
        """An absolute path names one machine's workspace.

        The next run decodes to a different directory, so the citation the chain
        exists to provide would not open.
        """
        with self.assertRaises(DecisionError):
            step(file="/home/arnav/AI/dfinsta-redux/work/439-explore/stock-439/X/0DnT.smali")

    def test_a_resolution_may_not_claim_it_found_the_host_by_its_descriptor(self):
        with self.assertRaises(DecisionError) as caught:
            resolution(signals=(FORBIDDEN_SIGNAL,))
        self.assertIn(FORBIDDEN_SIGNAL, str(caught.exception))

    def test_a_miss_needs_a_typed_status_and_detector(self):
        """`suspected` and `confirmed` are different claims and must not be free text.

        A miss spelled "probably" would compare equal to nothing and quietly
        drop out of every count.
        """
        with self.assertRaises(DecisionError):
            Miss(HOOK, "439", "suspected", Detector.HUMAN, "s")  # type: ignore[arg-type]
        with self.assertRaises(DecisionError):
            Miss(HOOK, "439", MissStatus.SUSPECTED, "an agent", "s")  # type: ignore[arg-type]

    def test_a_miss_needs_a_summary(self):
        with self.assertRaises(DecisionError):
            miss(summary="   ")

    def test_only_a_device_or_a_differential_makes_a_miss_runtime_proven(self):
        """A static argument is not runtime proof, however many checks agree.

        This is the 430 lesson stated as a property: that hook passed every
        static assertion there was and was dead on the phone.
        """
        self.assertIs(miss(detector=Detector.DEVICE_SESSION).proven_at_runtime, True)
        self.assertIs(
            miss(detector=Detector.ADVERSARIAL_VERIFIER).proven_at_runtime, False
        )
        self.assertIs(miss(detector=Detector.STATIC_AUDIT).proven_at_runtime, False)
        self.assertIs(
            miss(status=MissStatus.SUSPECTED, detector=Detector.DEVICE_SESSION).proven_at_runtime,
            False,
        )


class SurvivalRateTests(MemoryTestCase):
    """Precedence is data. These pin the arithmetic that makes it arguable."""

    def test_a_rate_that_contradicts_its_own_counts_is_refused(self):
        """A typo here silently reorders the fingerprint ranking.

        The counts are the evidence and the rate is the summary; if they
        disagree, one of them is wrong and neither can be trusted to rank
        anything.
        """
        with self.assertRaises(DecisionError):
            rate(DRAWABLE_ID, 0.9, survived=103, total=11737)
        self.assertAlmostEqual(
            rate(DRAWABLE_ID, 0.009, survived=103, total=11737).rate, 0.009
        )

    def test_half_a_count_is_refused(self):
        with self.assertRaises(DecisionError):
            rate(DRAWABLE_ID, 0.009, survived=103)
        with self.assertRaises(DecisionError):
            rate(DRAWABLE_ID, 0.009, total=11737)

    def test_impossible_counts_are_refused(self):
        for survived, total in ((11738, 11737), (-1, 100), (5, 0)):
            with self.subTest(survived=survived, total=total):
                with self.assertRaises(DecisionError):
                    rate(DRAWABLE_ID, 0.5, survived=survived, total=total)

    def test_the_obfuscated_descriptor_may_not_be_measured(self):
        """Its name-level survival is near total and its meaning survival is zero.

        Storing that number would rank the one forbidden signal first in
        `precedence()`, with a real measurement behind it — folklore with a
        citation.
        """
        with self.assertRaises(DecisionError):
            rate(FORBIDDEN_SIGNAL, 1.0)

    def test_a_rate_needs_two_different_versions(self):
        with self.assertRaises(DecisionError):
            SurvivalRate("439", "439", DRAWABLE_NAME, 1.0, "indexer")

    def test_precedence_orders_by_measurement_not_by_insertion(self):
        rates = [
            rate(DRAWABLE_ID, 0.009, survived=103, total=11737),
            rate(STABLE_NAMED_TYPE, 0.893),
            rate(DRAWABLE_NAME, 0.988),
            rate(API_PATH_LITERAL, 0.939),
        ]
        self.assertEqual(
            precedence(rates),
            (DRAWABLE_NAME, API_PATH_LITERAL, STABLE_NAMED_TYPE, DRAWABLE_ID),
        )

    def test_precedence_refuses_to_mix_version_pairs(self):
        """430->439 and 439->440 are different measurements.

        Blending them produces a ranking that describes no step that was
        actually measured, which is folklore wearing a number.
        """
        mixed = [
            rate(DRAWABLE_NAME, 0.988),
            SurvivalRate("439", "440", DRAWABLE_NAME, 0.1, "indexer"),
        ]
        with self.assertRaises(DecisionError):
            precedence(mixed)

    def test_precedence_of_nothing_is_nothing(self):
        self.assertEqual(precedence([]), ())


# -------------------------------------------------------------------- recall


class RecallTests(MemoryTestCase):
    """What a human at a gate gets, and what memory refuses to hand over."""

    def test_recall_is_json_serialisable(self):
        memory = seeded_memory()
        for report in (
            memory.recall(HOOK),
            memory.recall(HOOK, self.matching()),
            memory.recall("never-heard-of-it"),
            memory.report(),
        ):
            with self.subTest(hook=report.get("hook_id")):
                json.dumps(report)
                canonical_json(report)

    def test_a_miss_is_reported_even_when_the_predicate_calls_it_incompatible(self):
        """The predicate governs reuse of an ANSWER, never suppression of a WARNING.

        The asymmetry is the safe direction in both cases: an unknown-
        compatibility resolution is not reused, and an unknown-compatibility miss
        is still shown. Hiding a miss because nobody filled in a policy revision
        would lose the highest-value record in the store.
        """
        memory = self.memory(miss(version="430"))
        report = memory.recall(HOOK, self.blind())
        self.assertEqual(len(report["misses"]), 1)
        self.assertIs(report["misses"][0]["shown_regardless_of_compatibility"], True)
        self.assertIs(
            report["misses"][0]["compatibility_with_this_run"]["reusable"], False
        )

    def test_recall_without_a_context_reuses_nothing(self):
        # A caller that supplies no context has told memory nothing about the
        # target, and the honest response is that nothing may be replayed.
        report = self.memory(resolution()).recall(HOOK)
        self.assertIs(report["hint_available"], False)
        self.assertIs(report["resolutions"][0]["reuse"]["reusable"], False)
        self.assertEqual(
            set(report["resolutions"][0]["reuse"]["unknown"]), set(REUSE_DIMENSIONS)
        )

    def test_recall_still_shows_the_route_when_nothing_is_reusable(self):
        """A stale answer must not take the technique down with it.

        "Decision memory must not permanently suppress reassessment" cuts both
        ways: a decision that can no longer be replayed must still hand on how it
        was reached, or the next run starts from nothing again — which is what
        happened three times.
        """
        report = self.memory(resolution()).recall(HOOK, self.blind())
        self.assertIs(report["hint_available"], False)
        self.assertEqual(len(report["routes"]), 1)
        self.assertEqual(len(report["routes"][0]["chain"]), 1)

    def test_no_hint_is_offered_from_a_version_that_is_known_to_have_missed(self):
        """Replaying the technique that produced a dead patch is the one thing to refuse.

        The 430 settings hook resolved cleanly and was inert; handing that exact
        resolution forward as a hint would re-ship it.
        """
        memory = self.memory(resolution(version="430"), miss(version="430"))
        self.assertIsNone(memory.hint(HOOK, self.matching()))
        report = memory.recall(HOOK, self.matching())
        self.assertIs(report["resolutions"][0]["known_miss_here"], True)
        # The reuse predicate itself still says compatible: the refusal is the
        # miss, and the report shows both so the reason is not guessed at.
        self.assertIs(report["resolutions"][0]["reuse"]["reusable"], True)

    def test_a_miss_in_one_version_does_not_suppress_another_version(self):
        """Permanent suppression is the failure the plan names explicitly.

        A hook that was inert on 430 may be perfectly live on 439, and a store
        that refused it forever would quietly drop a working feature.
        """
        memory = self.memory(miss(version="430"), resolution(version="439"))
        hint = memory.hint(HOOK, self.matching("440"))
        self.assertIsNotNone(hint)
        self.assertEqual(hint.recorded_version, "439")

    def test_a_hint_carries_the_route_and_the_assessment(self):
        hint = self.memory(resolution()).hint(HOOK, self.matching())
        self.assertIsInstance(hint, Hint)
        self.assertIsInstance(hint.reuse, Reusability)
        self.assertEqual(hint.chain, (step(),))
        self.assertIs(hint.reuse.reusable, True)
        self.assertEqual(hint.target_version, "440")
        self.assertEqual(hint.route.version, "439")

    def test_hint_needs_a_context(self):
        with self.assertRaises(DecisionError):
            self.memory(resolution()).hint(HOOK, "440")  # type: ignore[arg-type]

    def test_an_unknown_hook_is_reported_as_unknown_not_as_safe(self):
        report = seeded_memory().recall("brand_new_hook")
        self.assertIs(report["known"], False)
        self.assertEqual(report["misses"], [])
        self.assertEqual(report["routes"], [])

    def test_the_report_separates_confirmed_from_suspected(self):
        report = seeded_memory().report()
        self.assertEqual(len(report["confirmed_misses"]), 2)
        self.assertEqual(len(report["suspected_misses"]), 1)
        self.assertEqual(
            report["suspected_misses"][0]["hook_id"], "install_settings_long_click_actionbar"
        )


# ---------------------------------------------------------------- the seed


class SeedTests(MemoryTestCase):
    """The three misses this project actually paid for, and the measured rates.

    Seeded as records rather than prose so that the next run is handed them
    automatically. Each was found by a different accident — a static audit, a
    device session, an adversarial verifier — and none by a standing check.
    """

    def setUp(self):
        super().setUp()
        self.memory = seeded_memory()

    def test_the_340_substitution_miss_is_recorded_against_its_own_hook(self):
        """`minshop` vs `minishops` lived in the Shopping substitutions.

        Filed under the hook it belongs to (docs/DFINSTA_1.4.1_DELTA.md:55), not
        under the settings hook: a miss keyed to the wrong hook_id is retrieved
        by the wrong hook, which is the confident wrong answer this store exists
        to prevent.
        """
        found = self.memory.misses_for("substitute_shopping_identifiers", "340")
        self.assertEqual(len(found), 1)
        self.assertIs(found[0].status, MissStatus.CONFIRMED)
        self.assertIs(found[0].detector, Detector.STATIC_AUDIT)
        self.assertEqual(found[0].detail["checked_for"], "minshop")
        self.assertEqual(found[0].detail["identifiers_actually_contained"], "minishops")

    def test_the_430_settings_miss_names_the_mobileconfig_flag(self):
        # The flag is the reusable part of that lesson: it says why static
        # verification could not have caught it, in any version.
        found = self.memory.misses_for(HOOK, "430")
        self.assertEqual(len(found), 1)
        self.assertIs(found[0].detector, Detector.DEVICE_SESSION)
        self.assertIs(found[0].proven_at_runtime, True)
        self.assertEqual(found[0].detail["flag"], "0x81099a000034a6")

    def test_the_439_actionbar_miss_is_suspected_and_not_confirmed(self):
        """It is a static argument that has never been run on a phone.

        Recording it as confirmed would repeat exactly the mistake it describes:
        the 430 hook was certified by static reasoning and was dead. The
        decisive test is recorded with it.
        """
        found = self.memory.misses_for("install_settings_long_click_actionbar", "439")
        self.assertEqual(len(found), 1)
        self.assertIs(found[0].status, MissStatus.SUSPECTED)
        self.assertIs(found[0].detector, Detector.ADVERSARIAL_VERIFIER)
        self.assertIs(found[0].proven_at_runtime, False)
        self.assertEqual(found[0].detected_at, "2026-08-01")
        self.assertEqual(found[0].detail["decisive_test"], "a build carrying only this hook")

    def test_the_measured_430_to_439_precedence_matches_the_measurement(self):
        self.assertEqual(
            self.memory.precedence("430", "439"),
            (DRAWABLE_NAME, API_PATH_LITERAL, STABLE_NAMED_TYPE, DRAWABLE_ID),
        )

    def test_the_drawable_id_rate_carries_the_counts_behind_it(self):
        # 103 of 11,737 is the number that overturned "drawable ids are stable",
        # so the counts travel with the rate rather than the claim travelling
        # alone.
        found = [r for r in self.memory.survival if r.signal == DRAWABLE_ID]
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0].survived, found[0].total), (103, 11737))
        self.assertEqual(found[0].percent, "0.9%")

    def test_the_seeded_route_is_the_one_three_agents_rediscovered(self):
        route = self.memory.routes_for(HOOK)[0]
        self.assertEqual(route.technique, SETTINGS_TECHNIQUE)
        self.assertIn("A19()", route.technique)
        self.assertIn("DRAWABLE NAME", route.technique)
        # Both action-bar implementations, because the flag picks at runtime and
        # patching one is what made 430 inert.
        self.assertIn("0x81099a000034a6", route.technique)
        self.assertEqual(tuple(route.signals), SETTINGS_SIGNALS)
        self.assertIn(STRUCTURAL_SHAPE, route.signals)
        self.assertEqual(len(route.chain), len(SETTINGS_ROUTE))
        for entry in route.chain:
            with self.subTest(cite=entry.cite):
                self.assertTrue(entry.file.startswith("smali"))
                self.assertGreater(entry.line, 0)

    def test_the_seed_stamps_nothing(self):
        # Every seeded record is unstamped, so a checkout does not acquire a
        # timestamp from whenever it happened to be read.
        for record in seed_records():
            with self.subTest(record=type(record).__name__):
                self.assertEqual(record.recorded_at, "")

    def test_seeding_twice_produces_equal_but_separate_memories(self):
        other = seeded_memory()
        self.assertEqual(as_bytes(other.report()), as_bytes(self.memory.report()))
        other.record(miss(version="441"))
        self.assertNotEqual(len(other.misses), len(self.memory.misses))

    def test_the_seed_survives_a_write_and_reload(self):
        seeded_memory(self.path)
        reloaded = DecisionMemory.load(self.path)
        self.assertEqual(as_bytes(reloaded.report()), as_bytes(self.memory.report()))


# ----------------------------------------------------------------------- cli


class CliTests(MemoryTestCase):
    """`recall <hook_id>` answers the first question anyone asks at a gate."""

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = StringIO(), StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_recall_prints_the_misses_for_a_hook_that_has_bitten_us(self):
        code, out, _ = self.run_cli("recall", HOOK, "--memory", str(self.path))
        self.assertEqual(code, 0)
        self.assertIn("MISSES", out)
        self.assertIn("430", out)
        self.assertIn("0x81099a000034a6", out)

    def test_recall_prints_the_technique_with_a_citable_chain(self):
        _, out, _ = self.run_cli("recall", HOOK, "--memory", str(self.path))
        self.assertIn("TECHNIQUE", out)
        self.assertIn("smali_classes6/com/instagram/profile/fragment/UserDetailFragment.smali:2", out)
        self.assertIn("smali_classes6/X/0DnT.smali:42", out)

    def test_recall_never_prints_the_descriptor(self):
        """The CLI is the easiest place to copy a stale answer out of.

        A human reading it needs the route and the misses; the 439 host name is
        the one thing on the page that would be wrong for 440.
        """
        _, out, _ = self.run_cli("recall", HOOK, "--memory", str(self.path), "--version", "440")
        self.assertNotIn("LX/0DnT;", out)

    def test_recall_with_no_compatibility_reports_not_reusable(self):
        # The default at the CLI is the same as the default in the predicate:
        # a caller who states nothing gets nothing.
        _, out, _ = self.run_cli("recall", HOOK, "--memory", str(self.path), "--version", "440")
        self.assertIn("NOT reusable", out)
        self.assertIn("unknown", out)

    def test_recall_with_a_full_context_can_report_reusable(self):
        _, out, _ = self.run_cli(
            "recall",
            HOOK,
            "--memory",
            str(self.path),
            "--version",
            "440",
            "--feature-identity",
            SETTINGS_COMPATIBILITY.semantic_feature_identity,
            "--delivery",
            SETTINGS_COMPATIBILITY.delivery_mechanism,
            "--evidence-fingerprint",
            SETTINGS_COMPATIBILITY.evidence_fingerprint,
            "--policy-revision",
            SETTINGS_COMPATIBILITY.policy_revision,
        )
        self.assertIn("A hint is available", out)
        self.assertNotIn("LX/0DnT;", out)

    def test_an_unknown_hook_exits_nonzero_and_says_so(self):
        code, out, _ = self.run_cli("recall", "brand_new_hook", "--memory", str(self.path))
        self.assertEqual(code, 1)
        self.assertIn("nothing recorded", out)
        self.assertIn("not the same as it being safe", out)

    def test_the_missing_memory_file_is_announced_on_stderr(self):
        """Falling back to the seed must be visible, or a stale path reads as clean.

        A run pointed at the wrong `--memory` would otherwise report "nothing
        known" and look like a hook with no history.
        """
        _, _, err = self.run_cli("recall", HOOK, "--memory", str(self.path))
        self.assertIn("no decision memory", err)
        self.assertIn(str(self.path), err)

    def test_an_existing_file_is_read_instead_of_the_seed(self):
        self.memory(miss(hook_id="only_here", version="441"), path=self.path)
        code, out, err = self.run_cli("recall", "only_here", "--memory", str(self.path))
        self.assertEqual(code, 0)
        self.assertIn("441", out)
        self.assertEqual(err, "")

    def test_the_json_form_is_the_recall_report(self):
        _, out, _ = self.run_cli("recall", HOOK, "--memory", str(self.path), "--json")
        parsed = json.loads(out)
        self.assertEqual(parsed["hook_id"], HOOK)
        self.assertNotIn("LX/0DnT;", out)

    def test_the_default_memory_path_is_committed_not_scratch(self):
        # This record must outlive every decode, so it belongs in git next to the
        # manifest rather than in the artifact store.
        self.assertEqual(DEFAULT_MEMORY_PATH, Path("manifest/decisions.jsonl"))


# --------------------------------------------------------------- determinism


class DeterminismTests(MemoryTestCase):
    """Nothing reads a clock, a random source, or a set's iteration order.

    Every one of these runs inside a Temporal workflow or an Activity that can be
    replayed. A function whose second call differs from its first turns a replay
    into a non-deterministic-workflow error and makes memory's answer depend on
    when it was asked.
    """

    def test_no_module_level_function_differs_between_two_calls(self):
        subject_calls = {
            "fingerprint_of": lambda: fingerprint_of("technique", ["a", "b"]),
            "reusable": lambda: reusable(resolution(), self.matching()),
            "stamped": lambda: stamped(resolution(), STAMP),
            "precedence": lambda: precedence([rate(DRAWABLE_NAME, 0.988), rate(DRAWABLE_ID, 0.009)]),
            "seed_records": lambda: seed_records(),
            "seeded_memory": lambda: seeded_memory().report(),
            "main": lambda: self._cli_output(),
        }
        # If a function is added to decisions.py, this fails until it is covered.
        defined = {
            name
            for name in dir(decisions_module)
            if not name.startswith("_")
            and callable(getattr(decisions_module, name))
            and getattr(getattr(decisions_module, name), "__module__", None)
            == decisions_module.__name__
            and not isinstance(getattr(decisions_module, name), type)
        }
        self.assertEqual(defined, set(subject_calls))

        for name, call in sorted(subject_calls.items()):
            with self.subTest(function=name):
                self.assertEqual(call(), call())

    def _cli_output(self) -> tuple[int, str]:
        out = StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", StringIO()):
            code = main(["recall", HOOK, "--memory", str(self.path), "--json"])
        return code, out.getvalue()

    def test_recall_is_identical_on_a_second_call(self):
        memory = seeded_memory()
        first = memory.recall(HOOK, self.matching())
        second = memory.recall(HOOK, self.matching())
        self.assertEqual(as_bytes(first), as_bytes(second))

    def test_the_report_is_identical_on_a_second_call(self):
        memory = seeded_memory()
        self.assertEqual(as_bytes(memory.report()), as_bytes(memory.report()))

    def test_recall_does_not_mutate_the_store(self):
        memory = seeded_memory()
        before = (memory.resolutions, memory.misses, memory.survival)
        memory.recall(HOOK, self.matching())
        memory.report()
        memory.hint(HOOK, self.matching())
        self.assertEqual((memory.resolutions, memory.misses, memory.survival), before)

    def test_a_record_serialises_identically_twice(self):
        record = resolution()
        self.assertEqual(canonical_json(record.to_dict()), canonical_json(record.to_dict()))

    def test_nothing_in_the_module_reads_the_clock(self):
        """`stamped` exists precisely so the constructors do not call `datetime.now()`.

        A record that stamped itself would serialise differently on every replay
        and its line would never match the one already on disk.
        """
        self.assertEqual(resolution().recorded_at, "")
        self.assertEqual(miss().recorded_at, "")
        self.assertEqual(rate(DRAWABLE_NAME, 0.988).recorded_at, "")
        record = resolution()
        self.assertEqual(stamped(record, STAMP).recorded_at, STAMP)
        self.assertEqual(record.recorded_at, "")  # the original is untouched

    def test_stamped_refuses_a_non_record(self):
        with self.assertRaises(DecisionError):
            stamped({"hook_id": HOOK}, STAMP)  # type: ignore[arg-type]

    def test_the_hooks_list_is_sorted_not_insertion_ordered(self):
        # Built from a set, so without the sort two workers would report the same
        # store in different orders.
        memory = self.memory(miss(hook_id="z", version="1"), miss(hook_id="a", version="1"))
        self.assertEqual(memory.hooks, ("a", "z"))

    def test_the_written_file_is_byte_identical_across_two_runs(self):
        first, second = self.tmp / "a.jsonl", self.tmp / "b.jsonl"
        seeded_memory(first)
        seeded_memory(second)
        self.assertEqual(first.read_bytes(), second.read_bytes())


# ------------------------------------------------------------------- mutations


class MutationTests(MemoryTestCase):
    """Each guard, re-attacked from the direction a broken implementation takes.

    A positive test proves the guard exists. These prove it bites: every one
    constructs the input a specific plausible mutation would wave through, and
    asserts the outcome that mutation could not produce.
    """

    def test_a_reuse_default_of_true_on_unknown_would_replay_a_stale_answer(self):
        """Mutation: treat a blank dimension as "matches" instead of "unknown".

        In production this is the quietest possible failure. A record written
        before a dimension existed — or by a caller that simply did not fill it
        in — reads as fully compatible, so a 430 resolution is offered as a hint
        for 439 with nothing checked. The descriptor it names exists in 439 and
        belongs to a different class, so the port assembles, passes static
        verification, and does nothing: the 430 settings hook again, this time
        blessed by memory.
        """
        # Every single-dimension gap, from either side, must refuse.
        for dimension in REUSE_DIMENSIONS:
            for side in ("stored", "current"):
                with self.subTest(dimension=dimension, side=side):
                    blanked = replace(KNOWN, **{dimension: ""})
                    record = resolution(compatibility=blanked if side == "stored" else KNOWN)
                    current = Context("439", KNOWN if side == "stored" else blanked)
                    self.assertFalse(reusable(record, current))
        # And the store must not route around the predicate.
        memory = self.memory(resolution(compatibility=Compatibility()))
        self.assertIsNone(memory.hint(HOOK, self.matching()))
        self.assertIs(memory.recall(HOOK, self.matching())["hint_available"], False)
        # The identical record with all four stated does yield a hint, so the
        # blank fields are the only thing between this and a replayed answer.
        stated = self.memory(resolution())
        self.assertIsNotNone(stated.hint(HOOK, self.matching()))

    def test_the_descriptor_escaping_without_re_verification_would_patch_a_stale_class(self):
        """Mutation: expose the descriptor as a plain attribute or in a report.

        In production a caller does `descriptor = memory.recall(hook)[...]` and
        hands it to the applier. Obfuscated names are recycled, so the name
        resolves in the new version and names an unrelated class: `LX/05t2` is a
        1990-line Reels builder in 430 and a 596-line stranger in 439. The patch
        applies to the wrong file, the anchor is not found — or worse, is —
        and the run reports success. Every route out has to require the
        acknowledgement.
        """
        memory = self.memory(resolution())
        hint = memory.hint(HOOK, self.matching())
        surfaces = {
            "recall": memory.recall(HOOK, self.matching()),
            "recall_bare": memory.recall(HOOK),
            "report": memory.report(),
            "by_key": memory.by_key(Key(HOOK, "439")),
            "route": memory.routes_for(HOOK)[0].to_dict(),
            "hint": hint.to_dict(),
        }
        for name, surface in surfaces.items():
            with self.subTest(surface=name):
                self.assertNotIn(DESCRIPTOR, canonical_json(surface))
        # Nor by string interpolation, the other way a wrapper gets defeated.
        self.assertNotIn(DESCRIPTOR, f"{hint.host} {hint.route} {memory.resolutions[0].route}")
        # The only door needs a typed acknowledgement pinned to this target.
        with self.assertRaises(DecisionError):
            hint.reverified_host(ack("439"))
        self.assertEqual(hint.reverified_host(ack("440")), DESCRIPTOR)

    def test_keying_by_descriptor_instead_of_hook_and_version_returns_a_confident_wrong_answer(self):
        """Mutation: index records by descriptor so a host can be looked up directly.

        In production this is the worst available failure, because it is not a
        crash. Asked "where is `LX/05t2;`?" the store answers with the 430 record
        while the run is porting 439, and the answer looks authoritative. An
        absent record would have produced a miss and an escalation; a
        descriptor-keyed one produces a patch on an unrelated class. That is why
        the lookup does not exist and why the key has two halves.
        """
        memory = self.memory(
            resolution(version="430", descriptor="LX/05t2;"),
            resolution(version="439", descriptor="LX/05t2;"),
        )
        # Same descriptor, two versions: the records must not have merged.
        self.assertEqual(len(memory.resolutions_for(HOOK)), 2)
        self.assertEqual(len(memory.by_key(Key(HOOK, "430"))["resolutions"]), 1)
        # There is no descriptor-shaped question to ask.
        with self.assertRaises(DecisionError):
            memory.lookup_by_descriptor("LX/05t2;")
        with self.assertRaises(DecisionError):
            memory.by_key("LX/05t2;")  # type: ignore[arg-type]
        # And a record cannot be filed under a key its own host disagrees with,
        # which is how a 430 answer would end up retrievable as a 439 one.
        with self.assertRaises(DecisionError):
            Resolution(
                hook_id=HOOK,
                version="439",
                host=RecalledDescriptor(HOOK, "430", "LX/05t2;"),
                smali_path="smali_classes3/X/05t2.smali",
                technique="t",
                chain=(step(),),
            )


if __name__ == "__main__":
    unittest.main()

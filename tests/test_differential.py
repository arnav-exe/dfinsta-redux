"""Tests for `differential`: a port regression, told apart from a broken probe.

The module exists for a distinction that a single capture cannot make. When a
hook that worked on version N-1 shows nothing on N, either the patch went inert
— the regression this evidence kind is for — or the *probe* went blind, because
the log line it counts was renamed or the surface moved. Reporting the second as
the first holds back a working port; reporting the first as the second ships an
inert hook, which this project has already done three times.

So these tests are written from the module's rules rather than from its shape.
The ones that matter most would still fail if the code were rewritten:

* a baseline that did not pass can never yield a pass, however good the current
  result looks — with no baseline pass there is no "where" to have regressed;
* an inconclusive current result over a baseline pass is only a regression when
  the capture can be shown to have been *capable* of seeing the signal;
* identity outranks delta outranks absence, because identity is the one signal
  DFInsta emits itself and therefore the one that cannot rot; and
* a version compared against itself is refused outright, rather than entering the
  ledger as a vacuous "identical".

Inputs are built by the real builders — `evidence.probe_claim`,
`IdentityProbe.claims`, `AbsenceResult`, `probes.attribute` — because this
module's whole job is reading claims that other code wrote, and a hand-written
detail dict would only test this file's idea of the format. The two places a
detail dict *is* written by hand (a malformed count, an unrecognised shape) are
checked against a real builder's keys in the same test class.

`RecordedEvidenceTests` runs the module over this repo's own
`work/evidence-439-runtime.jsonl` — the seven runtime claims the phone produced
for the 439 port — so the format contract with `probes.py` is pinned against the
real thing rather than against a fixture. It skips when `work/` is absent.
"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Iterable, Sequence

from dfinsta_pipeline import differential
from dfinsta_pipeline.differential import (
    SHAPE_PREFERENCE,
    DifferentialError,
    ProbeShape,
    compare,
    differential_claims,
    instrument_worked,
    probe_shape,
    read_claims,
    shaped_claims,
)
from dfinsta_pipeline.evidence import (
    ALLOWED_PRODUCERS,
    EvidenceClaim,
    EvidenceKind,
    Producer,
    Verdict,
    probe_claim,
)
from dfinsta_pipeline.hook_manifest import Hook, HostFingerprint, Probe
from dfinsta_pipeline.probes import (
    AbsenceResult,
    IdentityProbe,
    StartupProbe,
    attribute,
)


# --------------------------------------------------------------------- fixture

REPO = Path(__file__).resolve().parents[1]

#: This repo's own runtime evidence: the seven claims the phone produced for the
#: 439 port. Gitignored, so every test that reads it skips when it is absent.
RECORDED_439 = REPO / "work" / "evidence-439-runtime.jsonl"

#: The phone from the device-testing protocol, and the pair of versions the next
#: port will compare. A differential is always attributed to the device that
#: produced the *current* measurement.
DEVICE = "device:P3227J000775"
BASELINE = "439"
CURRENT = "440"

#: Signals the manifest actually declares, so the fixtures read like the run.
BLOCK_SIGNAL = "java.io.IOException: Blocked by DFInsta setting"
REELS_SIGNAL = "ClipsViewerPerfLogger: onClipsItemsRequestFailed"

#: The two settings hooks share one dialog and the three Reels hooks share one
#: failure log; exactly one implementation is live at a time, which is what
#: `probes.attribute` downgrades a claim for.
REELS_GROUP = (
    "replace_reels_discover_endpoint",
    "replace_reels_homecoming_endpoint",
    "replace_reels_stream_endpoint",
)


class NullDevice:
    """A `Device` that fails on contact.

    `IdentityProbe.claims` turns a set of hook ids into claims and never speaks
    to the phone. This proves that, and turns an accidental device call in a test
    into an error rather than a hang.
    """

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"no test here may touch the device (asked for {name!r})")


def make_hook(hook_id: str, probe: Probe | None = None) -> Hook:
    """A minimal valid hook. Only the id, the active status and the probe matter."""
    marker = f"# mark::{hook_id}"
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
        probe=probe,
    )


#: The startup probe the manifest declares for `set_app_context`.
STARTUP_PROBE = Probe(
    "startup_no_fatal",
    "NoSuchMethodError|VerifyError|ClassNotFoundException",
    "app_launch",
    requires_two_directional_delta=False,
    note="not toggleable; executing at startup without a fatal is the proof",
)


def delta(
    hook_id: str,
    enabled: int,
    disabled: int,
    *,
    actor: str = DEVICE,
    signal: str = BLOCK_SIGNAL,
    surface: str = "feed_tab",
) -> EvidenceClaim:
    """A two-directional probe result, from `evidence.probe_claim` itself."""
    return probe_claim(hook_id, surface, signal, enabled, disabled, True, actor)


def identity(
    hook_id: str,
    ran: bool,
    *,
    also_ran: Sequence[str] = (),
    actor: str = DEVICE,
) -> EvidenceClaim:
    """An identity result for one hook, from `IdentityProbe.claims` itself.

    ``also_ran`` is the shape's whole point: other hooks announcing themselves is
    what proves the instrument was alive in a capture where *this* hook was
    silent.
    """
    executed = set(also_ran) | ({hook_id} if ran else set())
    hooks = [make_hook(name) for name in (hook_id, *also_ran)]
    claims = IdentityProbe(NullDevice(), actor=actor).claims(
        hooks, executed, visited=("app_launch", "feed_tab")
    )
    return next(claim for claim in claims if claim.hook_id == hook_id)


def absence(
    hook_id: str,
    verdict: Verdict = Verdict.PASSED,
    *,
    hits: int = 0,
    control_found: bool = True,
    actor: str = DEVICE,
) -> EvidenceClaim:
    """An absence result, with its detail built by the real `AbsenceResult`.

    The verdict is chosen here rather than computed by `StartupProbe.claim`,
    which needs a device; `RealBuilderShapeTests` drives the real probe end to
    end to prove this detail is the one it writes.
    """
    result = AbsenceResult(
        hits, control_found, StartupProbe.CONTROL, {"process_alive": True}
    )
    return EvidenceClaim(
        hook_id=hook_id,
        kind=EvidenceKind.RUNTIME_PROBE,
        verdict=verdict,
        producer=Producer.DEVICE,
        actor=actor,
        summary="startup absence assertion, carrying its own positive control",
        detail=result.to_dict(),
    )


def malformed_delta(hook_id: str, enabled: Any, disabled: Any) -> EvidenceClaim:
    """A delta-shaped claim whose counts are not numbers.

    Hand-written because `probe_claim` will not build one — it rejects negatives
    and takes ints — but a corrupted ledger line or a future harness sentinel
    would look exactly like this, and the module must not raise on it.
    """
    return EvidenceClaim(
        hook_id=hook_id,
        kind=EvidenceKind.RUNTIME_PROBE,
        verdict=Verdict.INCONCLUSIVE,
        producer=Producer.DEVICE,
        actor=DEVICE,
        summary="a probe result whose counts did not survive the round trip",
        detail={
            "surface": "feed_tab",
            "signal": BLOCK_SIGNAL,
            "enabled_observations": enabled,
            "disabled_observations": disabled,
            "requires_two_directional_delta": True,
        },
    )


def judge(
    hook_id: str,
    baseline: Iterable[EvidenceClaim],
    current: Iterable[EvidenceClaim],
    **kwargs: Any,
) -> EvidenceClaim:
    """The one differential claim for one hook, end to end through the module."""
    claims = differential_claims(
        baseline,
        current,
        baseline_version=kwargs.pop("baseline_version", BASELINE),
        current_version=kwargs.pop("current_version", CURRENT),
        actor=kwargs.pop("actor", DEVICE),
        hook_ids=[hook_id],
        **kwargs,
    )
    assert len(claims) == 1, f"expected one claim, got {len(claims)}"
    return claims[0]


class FakeDevice:
    """Enough of the `Device` Protocol for `StartupProbe`, answered from canned state.

    Satisfies the Protocol so no test needs hardware. `sleep` returns at once —
    a real startup probe dwells eighteen seconds.
    """

    AWAKE = "mAwake=true mDreamingLockscreen=false"
    RESUMED = (
        "  topResumedActivity=ActivityRecord{9a1f2c u0 com.instagram.android/"
        ".activity.MainTabActivity t42}"
    )
    STARTED = (
        "08-01 01:43:44.001  1029  1066 I ActivityManager: "
        "Start proc 30503:com.instagram.android/u0a318 for pre-top-activity"
    )

    def __init__(self, logcat: str = "") -> None:
        self.logcat = logcat

    def shell(self, *args: str) -> str:
        if args[:2] == ("dumpsys", "window"):
            return self.AWAKE
        if args[:2] == ("dumpsys", "activity"):
            return self.RESUMED
        if args[:1] == ("pidof",):
            return "30503\n"
        return ""

    def logcat_clear(self) -> None:
        pass

    def logcat_dump(self) -> str:
        return self.logcat

    def ui_xml(self) -> str:  # pragma: no cover - the startup probe never dumps UI
        raise AssertionError("the startup probe does not read the UI")

    def tap(self, x: int, y: int) -> None:  # pragma: no cover
        raise AssertionError("the startup probe does not tap")

    def long_press(self, x: int, y: int, duration_ms: int = 800) -> None:  # pragma: no cover
        raise AssertionError("the startup probe does not long-press")

    def sleep(self, seconds: float) -> None:
        pass


# ------------------------------------------------------------ shape detection


class ShapeDetectionTests(unittest.TestCase):
    """Which shape a claim is, read from what the claim itself recorded.

    Keyed on detail fields rather than on a stored label, because the claims this
    has to read were written before the module existed. A claim matching none of
    the three key sets must come back `None` rather than being forced into a
    shape: an unrecognised probe compared as though it were a familiar one is a
    comparison of two things nobody checked are comparable.
    """

    def test_an_identity_probe_claim_is_detected_as_identity(self):
        self.assertIs(
            probe_shape(identity("tigon_url_block", ran=True)), ProbeShape.IDENTITY
        )

    def test_an_identity_claim_for_a_hook_that_stayed_silent_is_still_identity(self):
        # The shape is a property of the instrument, not of the answer. Reading
        # a silent hook as "no shape" would drop exactly the claims that carry
        # the discrimination this module is for.
        self.assertIs(
            probe_shape(identity("tigon_url_block", ran=False)), ProbeShape.IDENTITY
        )

    def test_a_startup_absence_claim_is_detected_as_absence(self):
        self.assertIs(probe_shape(absence("set_app_context")), ProbeShape.ABSENCE)

    def test_a_two_directional_probe_claim_is_detected_as_delta(self):
        self.assertIs(probe_shape(delta("tigon_url_block", 10, 0)), ProbeShape.DELTA)

    def test_a_probe_that_waived_its_delta_is_still_the_delta_shape(self):
        # `probe_claim` records `requires_two_directional_delta: False` rather
        # than omitting the key, so a waived probe stays recognisable. Were it
        # omitted, the claim would fall through to `None` and vanish from every
        # comparison without anything saying so.
        claim = probe_claim(
            "install_settings_long_click",
            "profile_options_long_press",
            "Distraction-free settings",
            1,
            0,
            False,
            DEVICE,
            waiver_note="not toggleable; the dialog opening is the whole proof",
        )
        self.assertIs(probe_shape(claim), ProbeShape.DELTA)

    def test_a_claim_of_another_kind_has_no_shape_even_with_probe_shaped_detail(self):
        """The kind is checked before the detail, so a static claim cannot pose.

        The 430 settings hook passed every static assertion and was dead on the
        phone. A deterministic claim carrying a probe-shaped detail must not be
        compared as a runtime result.
        """
        posing = EvidenceClaim(
            hook_id="install_settings_long_click",
            kind=EvidenceKind.STATIC_VERIFIED,
            verdict=Verdict.PASSED,
            producer=Producer.DETERMINISTIC,
            actor="verify.deterministic_checks",
            summary="every static assertion held",
            detail=dict(delta("install_settings_long_click", 4, 0).detail),
        )
        self.assertIsNone(probe_shape(posing))

    def test_this_modules_own_output_is_not_a_probe_shape(self):
        # A differential ledger handed back in must compare nothing, rather than
        # comparing differentials of differentials.
        produced = judge(
            "tigon_url_block",
            [delta("tigon_url_block", 10, 0)],
            [delta("tigon_url_block", 9, 0)],
        )
        self.assertIs(produced.kind, EvidenceKind.DIFFERENTIAL)
        self.assertIsNone(probe_shape(produced))

    def test_an_unrecognised_detail_shape_has_no_shape(self):
        for detail in (
            {},
            {"screenshots": 3, "note": "a probe nobody has written yet"},
            # Hits with no positive control: the absence-without-a-control
            # failure exactly, and it must not be read as an absence result.
            {"hits": 0},
        ):
            with self.subTest(detail=detail):
                claim = EvidenceClaim(
                    hook_id="hook.future",
                    kind=EvidenceKind.RUNTIME_PROBE,
                    verdict=Verdict.PASSED,
                    producer=Producer.DEVICE,
                    actor=DEVICE,
                    summary="a probe shape this module has never seen",
                    detail=detail,
                )
                self.assertIsNone(probe_shape(claim))

    def test_the_preference_order_covers_every_shape_exactly_once(self):
        # `compare` picks from SHAPE_PREFERENCE, so a shape missing from it would
        # be detected, indexed, and then silently never compared.
        self.assertEqual(set(SHAPE_PREFERENCE), set(ProbeShape))
        self.assertEqual(len(SHAPE_PREFERENCE), len(ProbeShape))


class RealBuilderShapeTests(unittest.TestCase):
    """The builders in `probes.py`, driven for real, still land on the shape expected.

    Two of the fixtures above assemble a detail dict themselves — the absence
    detail from `AbsenceResult`, the malformed delta by hand. These tests are the
    check that those shortcuts still describe what the pipeline writes.
    """

    def test_a_startup_probe_run_end_to_end_produces_an_absence_shape(self):
        hook = make_hook("set_app_context", STARTUP_PROBE)
        device = FakeDevice(logcat=FakeDevice.STARTED)
        claim, _ = StartupProbe(device, actor=DEVICE).claim(hook)

        self.assertIs(claim.verdict, Verdict.PASSED)
        self.assertIs(probe_shape(claim), ProbeShape.ABSENCE)
        # ...and the hand-built fixture writes the same keys the real probe does.
        self.assertLessEqual(set(absence("set_app_context").detail), set(claim.detail))

    def test_the_hand_written_malformed_delta_matches_the_real_builders_keys(self):
        real = set(delta("tigon_url_block", 10, 0).detail)
        broken = malformed_delta("tigon_url_block", "many", 0)
        self.assertEqual(set(broken.detail), real)
        self.assertIs(probe_shape(broken), ProbeShape.DELTA)


# --------------------------------------------------------- instrument liveness


class InstrumentWorkedTests(unittest.TestCase):
    """Could this capture have shown the signal at all?

    False means the capture is silent in a way that says nothing about the hook,
    so a difference from the baseline must not be reported as a regression. Every
    other judgement in the module rests on this answer.
    """

    def test_identity_is_live_when_a_hook_announced_itself(self):
        claim = identity("tigon_url_block", ran=True)
        self.assertIs(instrument_worked(claim, ProbeShape.IDENTITY), True)

    def test_identity_is_live_even_when_the_hook_under_examination_stayed_silent(self):
        """The discriminating case, and the reason identity outranks the others.

        `DFInstaProbe: <hook_id>` is emitted by DFInsta's own class, so any hook
        announcing itself proves the instrument was loaded and logging in this
        capture — including, and especially, in a capture where the hook being
        judged said nothing. That is the sharpest available separation between a
        dead hook and a dead probe, and reading it as "no signal, so we cannot
        tell" would throw the separation away.
        """
        claim = identity(
            "install_settings_long_click", ran=False, also_ran=("tigon_url_block",)
        )
        self.assertIs(claim.detail["executed"], False)
        self.assertEqual(claim.detail["hooks_that_ran"], ["tigon_url_block"])
        self.assertIs(instrument_worked(claim, ProbeShape.IDENTITY), True)

    def test_identity_is_blind_when_no_hook_announced_itself_at_all(self):
        # Nothing logged by anybody: the probe class may not even be loaded, and
        # this capture says nothing about any hook.
        claim = identity("tigon_url_block", ran=False)
        self.assertEqual(claim.detail["hooks_that_ran"], [])
        self.assertIs(instrument_worked(claim, ProbeShape.IDENTITY), False)

    def test_absence_follows_its_positive_control(self):
        # An absence assertion carries its own answer already: `control_found`
        # IS "could this search have found anything", recorded for this reason.
        for found in (True, False):
            with self.subTest(control_found=found):
                claim = absence("set_app_context", control_found=found)
                self.assertIs(instrument_worked(claim, ProbeShape.ABSENCE), found)

    def test_a_delta_is_live_when_the_signal_appeared_in_either_direction(self):
        for enabled, disabled in ((10, 0), (0, 3), (4, 4)):
            with self.subTest(enabled=enabled, disabled=disabled):
                claim = delta("tigon_url_block", enabled, disabled)
                self.assertIs(instrument_worked(claim, ProbeShape.DELTA), True)

    def test_a_delta_is_blind_when_the_signal_never_appeared_at_all(self):
        """Zero on both sides is the ambiguous case, not a measurement.

        That is the Reels case exactly: `replaceReelsEndpoint` blanks the
        endpoint upstream of `throwIfBlocked`, so block-counting sees nothing
        whichever way the toggle is set.
        """
        self.assertIs(instrument_worked(delta("h", 0, 0), ProbeShape.DELTA), False)

    def test_a_non_numeric_count_reads_as_a_dead_instrument_rather_than_raising(self):
        # A malformed claim is not a working instrument, and it is certainly not
        # grounds to raise in the middle of judging a whole ledger.
        for enabled, disabled in (("many", 0), (None, 0), (0, None), ([], 0), ({}, 1)):
            with self.subTest(enabled=enabled, disabled=disabled):
                claim = malformed_delta("h", enabled, disabled)
                self.assertIs(instrument_worked(claim, ProbeShape.DELTA), False)

    def test_the_shaped_claim_property_answers_the_same_way(self):
        # `compare` reads the property, not the function; they must not drift.
        for claim, shape in (
            (identity("h", ran=True), ProbeShape.IDENTITY),
            (identity("h", ran=False), ProbeShape.IDENTITY),
            (absence("h", control_found=False), ProbeShape.ABSENCE),
            (delta("h", 0, 0), ProbeShape.DELTA),
            (delta("h", 1, 0), ProbeShape.DELTA),
        ):
            with self.subTest(shape=shape.value, verdict=claim.verdict.value):
                shaped = shaped_claims([claim])["h"][shape]
                self.assertIs(shaped.instrument_worked, instrument_worked(claim, shape))

    def test_attribution_shared_reads_what_probes_attribute_wrote(self):
        shared = attribute(
            delta("replace_reels_discover_endpoint", 3, 0, signal=REELS_SIGNAL),
            REELS_GROUP,
        )
        indexed = shaped_claims([shared])["replace_reels_discover_endpoint"]
        self.assertIs(indexed[ProbeShape.DELTA].attribution_shared, True)

        alone = shaped_claims([delta("tigon_url_block", 3, 0)])["tigon_url_block"]
        self.assertIs(alone[ProbeShape.DELTA].attribution_shared, False)


# ------------------------------------------------------------- the verdict table


class VerdictTableTests(unittest.TestCase):
    """Every row of `compare`, one named test each.

    There is deliberately no fall-through to a default pass in the module: a case
    nobody thought of must surface as an inconclusive with its reason written
    down, never as a hook advancing on silence.
    """

    def test_a_pass_on_both_versions_is_a_pass(self):
        claim = judge(
            "tigon_url_block",
            [delta("tigon_url_block", 10, 0)],
            [delta("tigon_url_block", 9, 0)],
        )
        self.assertIs(claim.verdict, Verdict.PASSED)
        self.assertEqual(claim.detail["comparison"], "held")
        self.assertEqual(claim.detail["shape"], "delta")

    def test_a_pass_that_became_a_failure_is_a_regression(self):
        # The signal moving the wrong way is a measurement, not an ambiguity, so
        # nothing about instrument liveness needs to be consulted.
        claim = judge(
            "tigon_url_block",
            [delta("tigon_url_block", 10, 0)],
            [delta("tigon_url_block", 0, 5)],
        )
        self.assertIs(claim.verdict, Verdict.FAILED)
        self.assertEqual(claim.detail["comparison"], "regressed")

    def test_a_current_result_that_lost_attribution_is_neither_a_pass_nor_a_regression(self):
        """Attribution was lost, not necessarily the behaviour.

        The Reels trio share one failure log and the two settings hooks share one
        dialog. When an observation stops being attributable, what changed is the
        observation's ownership, not the hook — and this is checked BEFORE
        instrument liveness, because the instrument here demonstrably worked and
        the blind/regressed logic would call it a regression.
        """
        shared = attribute(
            delta("replace_reels_discover_endpoint", 3, 0, signal=REELS_SIGNAL),
            REELS_GROUP,
        )
        self.assertIs(shared.verdict, Verdict.INCONCLUSIVE)
        claim = judge(
            "replace_reels_discover_endpoint",
            [delta("replace_reels_discover_endpoint", 3, 0, signal=REELS_SIGNAL)],
            [shared],
        )
        self.assertIs(claim.verdict, Verdict.INCONCLUSIVE)
        self.assertEqual(claim.detail["comparison"], "attribution_lost")
        # The instrument was live. Without the attribution branch this row would
        # read as `regressed_instrument_live`, i.e. a port regression reported
        # against a hook that may be working perfectly.
        self.assertIs(claim.detail["current_instrument_worked"], True)

    def test_a_current_result_from_a_blind_probe_is_inconclusive(self):
        """An inert hook and a probe whose signal no longer exists look identical.

        Zero in both directions is a capture in which the string never appeared.
        Calling that a regression is how a working port gets held back.
        """
        claim = judge(
            "replace_reels_stream_endpoint",
            [delta("replace_reels_stream_endpoint", 4, 0, signal=REELS_SIGNAL)],
            [delta("replace_reels_stream_endpoint", 0, 0, signal=REELS_SIGNAL)],
        )
        self.assertIs(claim.verdict, Verdict.INCONCLUSIVE)
        self.assertEqual(claim.detail["comparison"], "probe_went_blind")
        self.assertIs(claim.detail["current_instrument_worked"], False)
        # And it names what would settle it, rather than leaving a reader to
        # guess which of the two explanations to act on.
        self.assertIn("identity probe", claim.summary)

    def test_a_silent_hook_in_a_capture_that_provably_worked_is_a_regression(self):
        """The one case that may be asserted as a port regression from silence.

        Other hooks announced themselves in this capture, so the instrument was
        alive; this hook's silence is therefore its own.
        """
        hook = "install_settings_long_click"
        claim = judge(
            hook,
            [identity(hook, ran=True, also_ran=("tigon_url_block",))],
            [identity(hook, ran=False, also_ran=("tigon_url_block",))],
        )
        self.assertIs(claim.verdict, Verdict.FAILED)
        self.assertEqual(claim.detail["comparison"], "regressed_instrument_live")
        self.assertIs(claim.detail["current_instrument_worked"], True)

    def test_an_inconclusive_baseline_cannot_yield_a_pass_however_good_the_current_result(self):
        """With no baseline pass there is no "where" for a regression to be.

        Letting a current pass satisfy the differential on its own would make
        this kind a second copy of `runtime_probe`, and a gate learns nothing
        from being told the same fact twice.
        """
        claim = judge(
            "replace_reels_discover_endpoint",
            [delta("replace_reels_discover_endpoint", 0, 0, signal=REELS_SIGNAL)],
            [delta("replace_reels_discover_endpoint", 7, 0, signal=REELS_SIGNAL)],
        )
        self.assertIsNot(claim.verdict, Verdict.PASSED)
        self.assertIs(claim.verdict, Verdict.INCONCLUSIVE)
        self.assertEqual(claim.detail["comparison"], "baseline_not_a_pass")
        self.assertEqual(claim.detail["baseline_verdict"], "inconclusive")
        self.assertEqual(claim.detail["current_verdict"], "passed")

    def test_a_failed_baseline_cannot_yield_a_pass_however_good_the_current_result(self):
        # The same rule from the other bad baseline: a hook that was broken on
        # N-1 and works on N has not been shown to have held, it has been shown
        # to have been fixed, which is a different fact and not this kind's.
        claim = judge(
            "tigon_url_block",
            [delta("tigon_url_block", 0, 5)],
            [delta("tigon_url_block", 9, 0)],
        )
        self.assertIsNot(claim.verdict, Verdict.PASSED)
        self.assertIs(claim.verdict, Verdict.INCONCLUSIVE)
        self.assertEqual(claim.detail["comparison"], "baseline_not_a_pass")
        self.assertEqual(claim.detail["baseline_verdict"], "failed")

    def test_no_baseline_result_is_inconclusive_and_points_at_the_waiver(self):
        """The first port of any hook can never satisfy this kind mechanically.

        The cost is meant to be visible: `waiver` is where a human records "first
        port of this hook" with their authority on it, which keeps "compared and
        unchanged" distinct from "never compared".
        """
        claim = judge("tigon_url_block", [], [delta("tigon_url_block", 9, 0)])
        self.assertIs(claim.verdict, Verdict.INCONCLUSIVE)
        self.assertEqual(claim.detail["comparison"], "no_baseline")
        self.assertEqual(claim.detail["baseline_shapes"], [])
        self.assertIn("waive", claim.summary)

    def test_no_current_result_is_inconclusive_and_not_a_defect_in_the_hook(self):
        # A measurement that was not taken says nothing. Recording it as failed
        # would make an incomplete walkthrough look like a broken port.
        claim = judge("tigon_url_block", [delta("tigon_url_block", 9, 0)], [])
        self.assertIs(claim.verdict, Verdict.INCONCLUSIVE)
        self.assertEqual(claim.detail["comparison"], "no_current")
        self.assertEqual(claim.detail["current_shapes"], [])

    def test_results_of_different_shapes_on_the_two_versions_are_not_comparable(self):
        # Both sides measured something; neither measured the same thing. A
        # delta pass and an identity silence are not a regression, they are two
        # unrelated observations.
        hook = "install_settings_long_click"
        claim = judge(hook, [identity(hook, ran=True)], [delta(hook, 0, 0)])
        self.assertIs(claim.verdict, Verdict.INCONCLUSIVE)
        self.assertEqual(claim.detail["comparison"], "shapes_disjoint")
        self.assertEqual(claim.detail["baseline_shapes"], ["identity"])
        self.assertEqual(claim.detail["current_shapes"], ["delta"])

    def test_compare_judges_one_hook_from_the_indexed_claims_alone(self):
        # `differential_claims` is the guarded entry point and `compare` is the
        # judgement. Called directly it must give the same answer, so the table
        # above is a test of the judgement rather than of the wrapper.
        hook = "tigon_url_block"
        direct = compare(
            hook,
            shaped_claims([delta(hook, 9, 0)])[hook],
            shaped_claims([delta(hook, 0, 0)])[hook],
            baseline_version=BASELINE,
            current_version=CURRENT,
            actor=DEVICE,
        )
        through = judge(hook, [delta(hook, 9, 0)], [delta(hook, 0, 0)])
        self.assertEqual(direct, through)
        self.assertEqual(direct.detail["comparison"], "probe_went_blind")

    def test_every_row_of_the_table_records_why_it_decided(self):
        # The `comparison` key is what a gate reads; a row that produced a
        # verdict without one would be a decision with no stated reason.
        hook = "tigon_url_block"
        rows = [
            judge(hook, [delta(hook, 9, 0)], [delta(hook, 8, 0)]),
            judge(hook, [delta(hook, 9, 0)], [delta(hook, 0, 5)]),
            judge(hook, [delta(hook, 9, 0)], [delta(hook, 0, 0)]),
            judge(hook, [delta(hook, 0, 0)], [delta(hook, 9, 0)]),
            judge(hook, [], [delta(hook, 9, 0)]),
            judge(hook, [delta(hook, 9, 0)], []),
            judge(hook, [identity(hook, ran=True)], [delta(hook, 9, 0)]),
        ]
        seen = {claim.detail["comparison"] for claim in rows}
        self.assertEqual(
            seen,
            {
                "held",
                "regressed",
                "probe_went_blind",
                "baseline_not_a_pass",
                "no_baseline",
                "no_current",
                "shapes_disjoint",
            },
        )
        for claim in rows:
            with self.subTest(comparison=claim.detail["comparison"]):
                self.assertTrue(claim.summary.strip())


# ------------------------------------------------------------ shape preference


class ShapePreferenceTests(unittest.TestCase):
    """Identity outranks delta outranks absence, and the ranking has consequences.

    Identity is the only signal DFInsta emits itself, so it is the only one that
    cannot be broken by Instagram renaming a log line. When a hook carries more
    than one shape and they disagree, the ranking is what decides — so these
    tests are built on a disagreement, with a control showing what the losing
    shape would have said on its own.
    """

    HOOK = "install_settings_long_click"

    def both_shapes(self, ran_now: bool):
        """Identity and delta for one hook, on both versions.

        The delta is unchanged across the versions and passes both times; the
        identity is what carries the difference.
        """
        baseline = [
            identity(self.HOOK, ran=True, also_ran=("tigon_url_block",)),
            delta(self.HOOK, 4, 0),
        ]
        current = [
            identity(self.HOOK, ran=ran_now, also_ran=("tigon_url_block",)),
            delta(self.HOOK, 4, 0),
        ]
        return baseline, current

    def test_identity_decides_when_it_disagrees_with_the_delta(self):
        baseline, current = self.both_shapes(ran_now=False)
        claim = judge(self.HOOK, baseline, current)
        # Identity says: it announced itself on 439, stayed silent on 440, and
        # another hook proved the instrument was alive — a regression.
        self.assertIs(claim.verdict, Verdict.FAILED)
        self.assertEqual(claim.detail["shape"], "identity")
        self.assertEqual(claim.detail["comparison"], "regressed_instrument_live")
        self.assertEqual(claim.detail["also_compared"], ["delta"])

    def test_the_delta_on_its_own_would_have_said_the_hook_held(self):
        """The control for the test above.

        Without it, "identity won" could pass simply because the delta pair also
        happened to be a failure. It is not: on the delta alone this hook looks
        entirely healthy, which is what makes the ranking load-bearing.
        """
        claim = judge(self.HOOK, [delta(self.HOOK, 4, 0)], [delta(self.HOOK, 4, 0)])
        self.assertIs(claim.verdict, Verdict.PASSED)
        self.assertEqual(claim.detail["comparison"], "held")

    def test_identity_still_decides_when_it_is_the_more_forgiving_answer(self):
        # The ranking is not "prefer the worst answer". Here identity passes on
        # both versions while the delta went blind, and identity still wins.
        baseline = [
            identity(self.HOOK, ran=True, also_ran=("tigon_url_block",)),
            delta(self.HOOK, 4, 0),
        ]
        current = [
            identity(self.HOOK, ran=True, also_ran=("tigon_url_block",)),
            delta(self.HOOK, 0, 0),
        ]
        claim = judge(self.HOOK, baseline, current)
        self.assertIs(claim.verdict, Verdict.PASSED)
        self.assertEqual(claim.detail["shape"], "identity")
        self.assertEqual(claim.detail["comparison"], "held")

    def test_delta_outranks_absence(self):
        baseline = [delta(self.HOOK, 4, 0), absence(self.HOOK)]
        current = [delta(self.HOOK, 0, 5), absence(self.HOOK)]
        claim = judge(self.HOOK, baseline, current)
        self.assertEqual(claim.detail["shape"], "delta")
        self.assertIs(claim.verdict, Verdict.FAILED)
        self.assertEqual(claim.detail["also_compared"], ["absence"])

    def test_also_compared_lists_the_other_shared_shapes_in_preference_order(self):
        baseline = [
            absence(self.HOOK),
            delta(self.HOOK, 4, 0),
            identity(self.HOOK, ran=True),
        ]
        current = [
            absence(self.HOOK),
            delta(self.HOOK, 4, 0),
            identity(self.HOOK, ran=True),
        ]
        claim = judge(self.HOOK, baseline, current)
        self.assertEqual(claim.detail["shape"], "identity")
        self.assertEqual(claim.detail["also_compared"], ["delta", "absence"])

    def test_only_shapes_present_on_both_sides_are_considered(self):
        # An identity result on the baseline that the current run did not repeat
        # must not be judged against a delta; the shared set is the delta alone.
        baseline = [identity(self.HOOK, ran=True), delta(self.HOOK, 4, 0)]
        current = [delta(self.HOOK, 4, 0)]
        claim = judge(self.HOOK, baseline, current)
        self.assertEqual(claim.detail["shape"], "delta")
        self.assertEqual(claim.detail["also_compared"], [])
        self.assertEqual(claim.detail["baseline_shapes"], ["delta", "identity"])


# -------------------------------------------------------------------- refusals


class RefusalTests(unittest.TestCase):
    """Differentials that cannot mean anything are refused, not returned.

    A vacuous "identical" in the ledger reads as evidence that a comparison
    happened, which is worse than no claim at all — the same reason a probe that
    could not run raises `ProbeNotTaken` instead of returning a zero.
    """

    def claims(self) -> list[EvidenceClaim]:
        return [delta("tigon_url_block", 9, 0)]

    def test_a_version_compared_against_itself_is_refused(self):
        with self.assertRaises(DifferentialError) as caught:
            differential_claims(
                self.claims(),
                self.claims(),
                baseline_version="439",
                current_version="439",
                actor=DEVICE,
            )
        self.assertIn("against itself", str(caught.exception))

    def test_the_same_version_is_refused_however_it_is_spaced(self):
        # Versions are compared stripped, like every other identity in this
        # codebase: one trailing space must not buy a vacuous comparison.
        with self.assertRaises(DifferentialError):
            differential_claims(
                self.claims(),
                self.claims(),
                baseline_version=" 439",
                current_version="439\t",
                actor=DEVICE,
            )

    def test_two_runs_of_the_same_build_are_refused(self):
        """Labelling one run a different version does not make it one.

        Two builds of one version are byte-identical here — measured, when the
        unattended 439 run's output was compared against the hand-verified one.
        """
        with self.assertRaises(DifferentialError) as caught:
            differential_claims(
                self.claims(),
                self.claims(),
                baseline_version="439",
                current_version="440",
                actor=DEVICE,
                baseline_build="sha256:d6a6cb93e09",
                current_build="sha256:d6a6cb93e09",
            )
        self.assertIn("sha256:d6a6cb93e09", str(caught.exception))

    def test_two_different_builds_are_compared(self):
        # The control for the refusal above: the guard must discriminate rather
        # than simply refuse whenever builds are named.
        claims = differential_claims(
            self.claims(),
            self.claims(),
            baseline_version="439",
            current_version="440",
            actor=DEVICE,
            baseline_build="sha256:aaa",
            current_build="sha256:bbb",
        )
        self.assertEqual(len(claims), 1)

    def test_unnamed_builds_are_not_treated_as_equal(self):
        # The common case: nobody passed a build hash. Two Nones must not read
        # as "the same build" and refuse every ordinary comparison.
        claims = differential_claims(
            self.claims(),
            self.claims(),
            baseline_version="439",
            current_version="440",
            actor=DEVICE,
        )
        self.assertEqual(len(claims), 1)

    def test_an_unnamed_version_is_refused(self):
        """An unlabelled differential cannot be read later.

        Checked in both positions, because a guard that only looked at one would
        leave half the claims in the ledger saying "compared against ''".
        """
        for baseline_version, current_version in (
            ("", "440"),
            ("   ", "440"),
            ("439", ""),
            ("439", "\t\n"),
            ("", ""),
        ):
            with self.subTest(baseline=repr(baseline_version), current=repr(current_version)):
                with self.assertRaises(DifferentialError) as caught:
                    differential_claims(
                        self.claims(),
                        self.claims(),
                        baseline_version=baseline_version,
                        current_version=current_version,
                        actor=DEVICE,
                    )
                self.assertIn("both versions must be named", str(caught.exception))

    def test_an_empty_actor_is_refused(self):
        # A differential is a device-produced claim. Evidence with no
        # identifiable producer cannot be checked against anything.
        for actor in ("", "   ", "\t\n"):
            with self.subTest(actor=repr(actor)):
                with self.assertRaises(DifferentialError) as caught:
                    differential_claims(
                        self.claims(),
                        self.claims(),
                        baseline_version="439",
                        current_version="440",
                        actor=actor,
                    )
                self.assertIn("must name the device", str(caught.exception))


# ------------------------------------------------------------ which hooks appear


class HookIdsTests(unittest.TestCase):
    """`hook_ids` fixes which hooks are expected, so a vanished hook still reports."""

    def test_a_hook_absent_from_both_ledgers_still_gets_a_claim_when_it_is_named(self):
        """A hook that disappeared from both runs is the case a shorter report hides.

        Passing the manifest's active hooks turns "nobody measured this" into an
        explicit inconclusive instead of an absence a reader has to notice.
        """
        claims = differential_claims(
            [delta("tigon_url_block", 9, 0)],
            [delta("tigon_url_block", 8, 0)],
            baseline_version=BASELINE,
            current_version=CURRENT,
            actor=DEVICE,
            hook_ids=["tigon_url_block", "set_app_context"],
        )
        by_hook = {claim.hook_id: claim for claim in claims}
        self.assertEqual(sorted(by_hook), ["set_app_context", "tigon_url_block"])
        vanished = by_hook["set_app_context"]
        self.assertIs(vanished.verdict, Verdict.INCONCLUSIVE)
        self.assertEqual(vanished.detail["comparison"], "no_baseline")

    def test_without_hook_ids_a_hook_absent_from_both_ledgers_is_missing_entirely(self):
        # The control for the test above, and the reason `hook_ids` exists: the
        # default set is the union of what the two ledgers mention, so a hook
        # nobody measured leaves no trace at all.
        claims = differential_claims(
            [delta("tigon_url_block", 9, 0)],
            [delta("tigon_url_block", 8, 0)],
            baseline_version=BASELINE,
            current_version=CURRENT,
            actor=DEVICE,
        )
        self.assertEqual([claim.hook_id for claim in claims], ["tigon_url_block"])

    def test_the_default_set_is_the_union_of_both_ledgers(self):
        claims = differential_claims(
            [delta("only_on_the_baseline", 9, 0)],
            [delta("only_on_the_current", 8, 0)],
            baseline_version=BASELINE,
            current_version=CURRENT,
            actor=DEVICE,
        )
        self.assertEqual(
            [claim.hook_id for claim in claims],
            ["only_on_the_baseline", "only_on_the_current"],
        )
        self.assertEqual(
            [claim.detail["comparison"] for claim in claims],
            ["no_current", "no_baseline"],
        )

    def test_hook_ids_fixes_the_order_of_the_report(self):
        # Given explicitly, the caller's order is kept — a report whose rows move
        # between runs is a diff nobody can read.
        names = ["set_app_context", "tigon_url_block", "replace_reels_stream_endpoint"]
        claims = differential_claims(
            [], [], baseline_version=BASELINE, current_version=CURRENT,
            actor=DEVICE, hook_ids=names,
        )
        self.assertEqual([claim.hook_id for claim in claims], names)


# --------------------------------------------------------- the produced claims


class ProducedClaimTests(unittest.TestCase):
    """What this module writes into the ledger, checked against the taxonomy."""

    def every_row(self) -> list[EvidenceClaim]:
        """One claim per comparison outcome the module can produce."""
        hook = "tigon_url_block"
        shared = attribute(delta(hook, 3, 0, signal=REELS_SIGNAL), REELS_GROUP)
        return [
            judge(hook, [delta(hook, 9, 0)], [delta(hook, 8, 0)]),
            judge(hook, [delta(hook, 9, 0)], [delta(hook, 0, 5)]),
            judge(hook, [delta(hook, 9, 0)], [delta(hook, 0, 0)]),
            judge(hook, [delta(hook, 3, 0, signal=REELS_SIGNAL)], [shared]),
            judge(
                hook,
                [identity(hook, ran=True, also_ran=("other",))],
                [identity(hook, ran=False, also_ran=("other",))],
            ),
            judge(hook, [delta(hook, 0, 0)], [delta(hook, 9, 0)]),
            judge(hook, [], [delta(hook, 9, 0)]),
            judge(hook, [delta(hook, 9, 0)], []),
            judge(hook, [identity(hook, ran=True)], [delta(hook, 9, 0)]),
        ]

    def test_every_produced_claim_is_a_differential_from_the_device(self):
        for claim in self.every_row():
            with self.subTest(comparison=claim.detail["comparison"]):
                self.assertIs(claim.kind, EvidenceKind.DIFFERENTIAL)
                self.assertIs(claim.producer, Producer.DEVICE)
                self.assertEqual(claim.actor, DEVICE)

    def test_the_device_is_the_only_producer_the_taxonomy_allows_for_this_kind(self):
        """Pinned against the real mapping, so a taxonomy change breaks here.

        If `ALLOWED_PRODUCERS` stopped admitting the device, every claim this
        module builds would raise at a gate — far from the change that caused it.
        """
        self.assertIn(Producer.DEVICE, ALLOWED_PRODUCERS[EvidenceKind.DIFFERENTIAL])
        self.assertEqual(
            ALLOWED_PRODUCERS[EvidenceKind.DIFFERENTIAL], frozenset({Producer.DEVICE})
        )

    def test_a_deterministic_checker_could_not_have_filed_these_claims(self):
        # The other half of the same rule: a static check may not stand in for
        # the phone, which is why the differential names the device that measured.
        with self.assertRaises(ValueError):
            EvidenceClaim(
                hook_id="tigon_url_block",
                kind=EvidenceKind.DIFFERENTIAL,
                verdict=Verdict.PASSED,
                producer=Producer.DETERMINISTIC,
                actor="verify.deterministic_checks",
                summary="the two decodes look the same to me",
            )

    def test_every_produced_claim_records_both_versions_and_a_comparison(self):
        for claim in self.every_row():
            with self.subTest(comparison=claim.detail["comparison"]):
                self.assertEqual(claim.detail["baseline_version"], BASELINE)
                self.assertEqual(claim.detail["current_version"], CURRENT)
                self.assertTrue(claim.detail["comparison"])

    def test_a_produced_claim_round_trips_through_the_ledger_schema(self):
        # The claims are written to a JSONL and read back by `EvidenceLedger`;
        # a detail value that cannot be serialised would fail at write time, in
        # the middle of a run, with the measurement already taken.
        for claim in self.every_row():
            with self.subTest(comparison=claim.detail["comparison"]):
                line = json.dumps(claim.to_dict(), sort_keys=True)
                self.assertEqual(EvidenceClaim.from_dict(json.loads(line)), claim)

    def test_the_versions_are_recorded_stripped(self):
        claim = judge(
            "tigon_url_block",
            [delta("tigon_url_block", 9, 0)],
            [delta("tigon_url_block", 8, 0)],
            baseline_version="  439  ",
            current_version=" 440 ",
        )
        self.assertEqual(claim.detail["baseline_version"], "439")
        self.assertEqual(claim.detail["current_version"], "440")


# ------------------------------------------------------------------ superseding


class SupersedingTests(unittest.TestCase):
    """The ledger is append-only, so a later claim supersedes an earlier one.

    The same rule `EvidenceLedger.readiness` applies. Keeping the first would
    make a re-measured hook be judged on the measurement it replaced.
    """

    def test_the_last_claim_of_a_shape_wins(self):
        first = delta("tigon_url_block", 0, 0)  # the probe saw nothing
        second = delta("tigon_url_block", 9, 0)  # ...and the re-run saw plenty
        indexed = shaped_claims([first, second])["tigon_url_block"][ProbeShape.DELTA]
        self.assertIs(indexed.claim, second)

    def test_superseding_is_per_shape_rather_than_per_hook(self):
        # A later delta must not displace an earlier identity: they answer
        # different questions and both belong in the index.
        first = identity("tigon_url_block", ran=True)
        second = delta("tigon_url_block", 9, 0)
        indexed = shaped_claims([first, second])["tigon_url_block"]
        self.assertEqual(set(indexed), {ProbeShape.IDENTITY, ProbeShape.DELTA})
        self.assertIs(indexed[ProbeShape.IDENTITY].claim, first)
        self.assertIs(indexed[ProbeShape.DELTA].claim, second)

    def test_the_superseding_rule_decides_the_differential(self):
        """End to end: the re-run is what gets compared, not the run it replaced.

        Were the first claim kept, this hook would be reported as
        `probe_went_blind` on the strength of a measurement that was taken again
        and came back clean.
        """
        claim = judge(
            "tigon_url_block",
            [delta("tigon_url_block", 9, 0)],
            [delta("tigon_url_block", 0, 0), delta("tigon_url_block", 8, 0)],
        )
        self.assertIs(claim.verdict, Verdict.PASSED)
        self.assertEqual(claim.detail["comparison"], "held")

    def test_claims_of_other_kinds_are_ignored_rather_than_rejected(self):
        # A whole run's ledger can be handed in, which is how this is actually
        # called: read the JSONL, pass it, let the module pick out the probes.
        static = EvidenceClaim(
            hook_id="tigon_url_block",
            kind=EvidenceKind.STATIC_VERIFIED,
            verdict=Verdict.PASSED,
            producer=Producer.DETERMINISTIC,
            actor="verify.deterministic_checks",
            summary="the marker appears once, in one class",
        )
        indexed = shaped_claims([static, delta("tigon_url_block", 9, 0)])
        self.assertEqual(set(indexed), {"tigon_url_block"})
        self.assertEqual(set(indexed["tigon_url_block"]), {ProbeShape.DELTA})

    def test_an_unrecognised_probe_shape_is_dropped_from_the_index(self):
        unknown = EvidenceClaim(
            hook_id="tigon_url_block",
            kind=EvidenceKind.RUNTIME_PROBE,
            verdict=Verdict.PASSED,
            producer=Producer.DEVICE,
            actor=DEVICE,
            summary="a probe shape this module has never seen",
            detail={"screenshots": 3},
        )
        self.assertEqual(shaped_claims([unknown]), {})


# ------------------------------------------------------------------- the CLI


class CommandLineTests(unittest.TestCase):
    """`main`: the exit code a script reads, and a ledger that is never truncated."""

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)

    def write(self, name: str, claims: Iterable[EvidenceClaim]) -> Path:
        path = self.tmp / name
        path.write_text(
            "".join(json.dumps(claim.to_dict(), sort_keys=True) + "\n" for claim in claims),
            encoding="utf-8",
        )
        return path

    def run_main(self, *args: str) -> tuple[int, str]:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = differential.main(list(args))
        return code, stdout.getvalue()

    def base_args(self, baseline: Path, current: Path) -> list[str]:
        return [
            "--baseline", str(baseline),
            "--baseline-version", BASELINE,
            "--current", str(current),
            "--current-version", CURRENT,
            "--actor", DEVICE,
        ]

    def test_a_regression_exits_one(self):
        """A finding must not be reported through a zero a script would ignore."""
        baseline = self.write("baseline.jsonl", [delta("tigon_url_block", 9, 0)])
        current = self.write("current.jsonl", [delta("tigon_url_block", 0, 5)])
        code, stdout = self.run_main(*self.base_args(baseline, current))
        self.assertEqual(code, 1)
        self.assertIn("regressed", stdout)

    def test_no_regression_exits_zero(self):
        # The control: the same command over a healthy pair. Without it, an exit
        # code of 1 for everything would satisfy the test above.
        baseline = self.write("baseline.jsonl", [delta("tigon_url_block", 9, 0)])
        current = self.write("current.jsonl", [delta("tigon_url_block", 8, 0)])
        code, stdout = self.run_main(*self.base_args(baseline, current))
        self.assertEqual(code, 0)
        self.assertIn("passed", stdout)

    def test_an_inconclusive_result_is_not_reported_as_a_failure(self):
        # A blind probe is not a regression, and the exit code must not say it is.
        baseline = self.write("baseline.jsonl", [delta("tigon_url_block", 9, 0)])
        current = self.write("current.jsonl", [delta("tigon_url_block", 0, 0)])
        code, stdout = self.run_main(*self.base_args(baseline, current))
        self.assertEqual(code, 0)
        self.assertIn("probe_went_blind", stdout)

    def test_a_refusal_exits_two_rather_than_writing_a_vacuous_comparison(self):
        baseline = self.write("baseline.jsonl", [delta("tigon_url_block", 9, 0)])
        current = self.write("current.jsonl", [delta("tigon_url_block", 9, 0)])
        out = self.tmp / "ledger.jsonl"
        code, stdout = self.run_main(
            "--baseline", str(baseline),
            "--baseline-version", "439",
            "--current", str(current),
            "--current-version", "439",
            "--actor", DEVICE,
            "--out", str(out),
        )
        self.assertEqual(code, 2)
        self.assertIn("error:", stdout)
        self.assertFalse(out.exists())

    def test_the_out_file_is_appended_to_rather_than_truncated(self):
        """The ledger is append-only, and a run must not eat the run before it.

        A truncating write would silently discard every claim already earned —
        including the ones from the very baseline being compared against, if a
        caller happens to point `--out` at the same file.
        """
        baseline = self.write("baseline.jsonl", [delta("tigon_url_block", 9, 0)])
        current = self.write("current.jsonl", [delta("tigon_url_block", 8, 0)])
        out = self.tmp / "ledger.jsonl"
        earlier = json.dumps(delta("set_app_context", 1, 0).to_dict(), sort_keys=True)
        out.write_text(earlier + "\n", encoding="utf-8")

        self.run_main(*self.base_args(baseline, current), "--out", str(out))
        after_first = out.read_text(encoding="utf-8").splitlines()
        self.run_main(*self.base_args(baseline, current), "--out", str(out))
        after_second = out.read_text(encoding="utf-8").splitlines()

        self.assertEqual(after_first[0], earlier)
        self.assertEqual(len(after_first), 2)
        self.assertEqual(after_second[: len(after_first)], after_first)
        self.assertEqual(len(after_second), 3)
        # Every line is still a readable claim, so the file remains a ledger.
        written = read_claims(out)
        self.assertEqual(len(written), 3)
        self.assertEqual(
            [claim.kind for claim in written[1:]],
            [EvidenceKind.DIFFERENTIAL, EvidenceKind.DIFFERENTIAL],
        )

    def test_the_json_output_carries_the_whole_claim(self):
        baseline = self.write("baseline.jsonl", [delta("tigon_url_block", 9, 0)])
        current = self.write("current.jsonl", [delta("tigon_url_block", 8, 0)])
        code, stdout = self.run_main(*self.base_args(baseline, current), "--json")
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["kind"], "differential")
        self.assertEqual(payload[0]["detail"]["comparison"], "held")

    def test_an_unreadable_line_names_the_file_and_the_line_number(self):
        path = self.tmp / "broken.jsonl"
        path.write_text('{"schema_version": 1}\nnot json at all\n', encoding="utf-8")
        with self.assertRaises(DifferentialError) as caught:
            read_claims(path)
        self.assertIn(f"{path}:1", str(caught.exception))

    def test_blank_lines_are_skipped_rather_than_failing_the_read(self):
        path = self.tmp / "spaced.jsonl"
        line = json.dumps(delta("tigon_url_block", 9, 0).to_dict(), sort_keys=True)
        path.write_text(f"\n{line}\n\n", encoding="utf-8")
        self.assertEqual(len(read_claims(path)), 1)


# ---------------------------------------------------------- the recorded run


class RecordedEvidenceTests(unittest.TestCase):
    """The format contract with `probes.py`, checked against the real thing.

    `work/evidence-439-runtime.jsonl` holds the seven runtime claims the phone
    produced for the 439 port. Everything above builds its inputs from the
    builders; this reads what those builders actually wrote to disk during a real
    run, so a change to what `probes.py` records fails here rather than at a gate.
    """

    def setUp(self):
        if not RECORDED_439.exists():
            raise unittest.SkipTest(f"{RECORDED_439} is absent (work/ is gitignored)")
        self.claims = read_claims(RECORDED_439)

    def test_the_recorded_run_is_seven_runtime_probe_claims_from_one_phone(self):
        self.assertEqual(len(self.claims), 7)
        for claim in self.claims:
            with self.subTest(hook=claim.hook_id):
                self.assertIs(claim.kind, EvidenceKind.RUNTIME_PROBE)
                self.assertIs(claim.producer, Producer.DEVICE)

    def test_every_recorded_claim_is_recognised_as_one_of_the_three_shapes(self):
        # A `None` here would mean a real probe result silently dropping out of
        # every comparison — the failure `probe_shape` returning None protects
        # against, so it must not be the normal case for real data.
        for claim in self.claims:
            with self.subTest(hook=claim.hook_id):
                self.assertIn(probe_shape(claim), set(ProbeShape))

    def test_the_shapes_detected_are_the_ones_the_probes_wrote(self):
        expected = {
            # StartupProbe -> an absence assertion with its positive control.
            "set_app_context": ProbeShape.ABSENCE,
            # ProbeRunner.run -> two-directional logcat deltas.
            "tigon_url_block": ProbeShape.DELTA,
            "replace_reels_discover_endpoint": ProbeShape.DELTA,
            "replace_reels_homecoming_endpoint": ProbeShape.DELTA,
            "replace_reels_stream_endpoint": ProbeShape.DELTA,
            # DialogProbe -> an absence assertion over the UI dump.
            "install_settings_long_click": ProbeShape.ABSENCE,
            "install_settings_long_click_actionbar": ProbeShape.ABSENCE,
        }
        self.assertEqual(
            {claim.hook_id: probe_shape(claim) for claim in self.claims}, expected
        )

    def test_the_recorded_run_carries_no_identity_results(self):
        """The shipped 439 build has no probe instrumentation, and it shows here.

        Five of these seven claims are `inconclusive` for shared attribution —
        exactly what the identity shape exists to resolve — and none of them can
        be resolved from this ledger, because no hook was able to announce itself.
        """
        indexed = shaped_claims(self.claims)
        shapes = {shape for hook in indexed.values() for shape in hook}
        self.assertNotIn(ProbeShape.IDENTITY, shapes)
        shared = [
            claim for claim in self.claims if claim.detail.get("attribution") == "shared"
        ]
        self.assertEqual(len(shared), 5)

    def test_the_index_keeps_every_hook_and_one_shape_each(self):
        indexed = shaped_claims(self.claims)
        self.assertEqual(len(indexed), 7)
        for hook_id, shapes in indexed.items():
            with self.subTest(hook=hook_id):
                self.assertEqual(len(shapes), 1)

    def test_only_two_of_the_seven_439_hooks_could_ever_yield_a_differential_pass(self):
        """What this ledger is worth as a baseline, stated rather than assumed.

        Five of the seven ended `inconclusive` on 439, so no later version can be
        shown to have broken them — there is no pass to have regressed from. Only
        `set_app_context` and `tigon_url_block` carry a comparable result, and the
        other five need a waiver or an identity probe, not a re-run.
        """
        claims = differential_claims(
            self.claims,
            self.claims,
            baseline_version="439",
            current_version="440",
            actor=DEVICE,
        )
        by_verdict: dict[Verdict, list[str]] = {}
        for claim in claims:
            by_verdict.setdefault(claim.verdict, []).append(claim.hook_id)
        self.assertEqual(
            sorted(by_verdict[Verdict.PASSED]), ["set_app_context", "tigon_url_block"]
        )
        self.assertEqual(len(by_verdict[Verdict.INCONCLUSIVE]), 5)
        self.assertNotIn(Verdict.FAILED, by_verdict)
        for claim in claims:
            if claim.verdict is Verdict.INCONCLUSIVE:
                with self.subTest(hook=claim.hook_id):
                    self.assertEqual(claim.detail["comparison"], "baseline_not_a_pass")

    def test_the_recorded_run_compared_against_itself_is_refused(self):
        with self.assertRaises(DifferentialError):
            differential_claims(
                self.claims,
                self.claims,
                baseline_version="439",
                current_version="439",
                actor=DEVICE,
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

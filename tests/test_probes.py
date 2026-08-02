"""Tests for `probes`: the stage that tells a working hook from an inert one.

No test here touches a phone, adb, or a network. `ProbeRunner` and its three
subclasses take a `Device` — a Protocol that exists so a probe can be driven
without hardware — so every test injects :class:`FakeDevice`, which answers
`shell` from canned strings, never sleeps, and records what it was asked.
`AdbDevice` is exercised exactly once, through a subclass that intercepts
`_run`, because its `ui_xml` carries a freshness guard that no fake could
observe.

The logcat fixtures are verbatim lines from this repo's own captures under
`work/device-runner/newkey-430-contrasts/`. That is deliberate: the whole
difficulty of `count_signal` is the *shape* Instagram happens to emit, and a
fixture invented to match the counter would prove only that the counter handles
a shape that never occurs. These lines are the ones that were actually on the
device when the 430 contrast was measured.

:class:`KnownDefectTests` are `expectedFailure` on purpose. Each asserts what
this module's own docstrings say must happen and what it currently does not do,
so the suite stays green today and reports an *unexpected success* the moment a
defect is fixed and the test is due to be promoted.
"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from dfinsta_pipeline import probes
from dfinsta_pipeline.evidence import EvidenceClaim, EvidenceKind, Producer, Verdict
from dfinsta_pipeline.hook_manifest import Hook, HostFingerprint, Probe
from dfinsta_pipeline.probes import (
    PACKAGE,
    SURFACES,
    AdbDevice,
    DialogProbe,
    IdentityProbe,
    Measurement,
    ProbeNotTaken,
    ProbeRunner,
    SignalCount,
    StartupProbe,
    attribute,
    count_signal,
    shared_signals,
)

# --------------------------------------------------------------------- fixtures

#: The signal the manifest actually declares for `tigon_url_block`.
BLOCK_SIGNAL = "java.io.IOException: Blocked by DFInsta setting"

#: ...and the bare text the module docstring quotes when it explains the count.
#: Both are real; which one a manifest declares turns out to matter, see
#: `KnownDefectTests.test_an_unindented_narration_of_a_past_block_counts_as_live`.
BARE_SIGNAL = "Blocked by DFInsta setting"

#: A block that happened. `work/device-runner/newkey-430-contrasts/logcat_feed_ON.txt:3`.
LIVE_BLOCK = (
    "08-01 01:43:51.185 30503 30503 E IgFunctionalErrorEvent: "
    "java.io.IOException: Blocked by DFInsta setting"
)

#: The same block restated as a field of the event payload — same text, same
#: timestamp, same pid, tab-indented. `logcat_feed_ON.txt:13`.
FAILURE_REASON_ECHO = (
    "08-01 01:43:51.185 30503 30503 E IgFunctionalErrorEvent: "
    "\t NETWORK_FAILURE_REASON = Blocked by DFInsta setting"
)

#: A batched history flushed a phase late: this is in the *Explore off-side*
#: capture and narrates blocks from the previous phase. Trimmed in the middle —
#: the real line is 4100 characters of JSON — but the head, the indent and the
#: embedded signal are verbatim from `logcat_explore_OFF.txt:2191`.
AWARE_TRACE_FIELD = (
    "08-01 01:49:16.241  1029  1066 E IgFunctionalErrorEvent: "
    '\t aware_trace = [{"user_action":"tap","user_action_detail":"PTR",'
    '"current_module":"feed_timeline","errors":[{"type":"network",'
    '"request_name":"IgApi discover/topical_explore/","status_code":"0",'
    '"error_message":"fault_message: Blocked by DFInsta setting"}]}]'
)

#: The readable form of that same history: an indented field whose value spans
#: lines, and whose continuations logcat emits UN-indented under the same tag.
#: Verbatim `logcat_stories_OFF.txt:2200-2201` with its opening field line — an
#: OFF-side capture narrating ON-side blocks. A narration line never occurs
#: without the field that opened it, so a one-line fixture would be a shape the
#: device does not produce.
AWARE_TRACE_NARRATION = "\n".join(
    [
        "08-01 01:57:54.508  8646  8685 E IgFunctionalErrorEvent: "
        "\t aware_trace_readable = During the current app session, user tapped "
        "feed_tab to PTR on module feed_timeline, then App responded",
        "08-01 01:57:54.508  8646  8685 E IgFunctionalErrorEvent: After 14 seconds, "
        "user tapped feed_tab to PTR on module feed_timeline, then App responded with: "
        "Loading indicator shown and didn't stop,played 1 video, network issues: "
        "Network request IgStreamingApi feed/reels_tray/ failed with 0, error message: "
        "fault_message: Blocked by DFInsta setting.",
        "08-01 01:57:54.508  8646  8685 E IgFunctionalErrorEvent: After 6 seconds, "
        "user vertically scrolled list to PTR on module feed_timeline, then App "
        "responded with: vertical scroll animation started and stopped,"
        "Pull-to-refresh loading indicator shown for 1084 ms,played 1 video, network "
        "issues: Network request IgStreamingApi feed/reels_tray/ failed with 0, "
        "error message: fault_message: Blocked by DFInsta setting.",
    ]
)

#: The body of a real block payload, in order: a header line, the exception, then
#: the stack. `logcat_feed_ON.txt:2-4`. The exception is the SECOND line, which is
#: why "the first line of the payload" is not the rule.
BLOCK_PAYLOAD = "\n".join(
    [
        "08-01 01:43:51.185 30503 30503 E IgFunctionalErrorEvent: FEED_NOT_LOADING",
        LIVE_BLOCK,
        "08-01 01:43:51.185 30503 30503 E IgFunctionalErrorEvent: "
        "\tat com.dfinstagram.hooks.throwIfBlocked(dex-id-d6a6cb93e09)",
        "08-01 01:43:51.185 30503 30503 E IgFunctionalErrorEvent: "
        "\tat com.instagram.api.tigon.TigonServiceLayer.startRequest(:49)",
        FAILURE_REASON_ECHO,
    ]
)

AWAKE = "mAwake=true mDreamingLockscreen=false"
LOCKED = "mAwake=true mDreamingLockscreen=true"
RESUMED = (
    "  topResumedActivity=ActivityRecord{9a1f2c u0 com.instagram.android/"
    ".activity.MainTabActivity t42}"
)
STARTED = (
    "08-01 01:43:44.001  1029  1066 I ActivityManager: "
    "Start proc 30503:com.instagram.android/u0a318 for pre-top-activity"
)


def node(resource_id: str = "", desc: str = "", bounds: str = "[0,2124][216,2264]") -> str:
    return (
        f'<node index="1" package="{PACKAGE}" resource-id="{resource_id}" '
        f'content-desc="{desc}" long-clickable="true" bounds="{bounds}" />'
    )


def screen(*nodes: str) -> str:
    return f'<hierarchy rotation="0">{"".join(nodes)}</hierarchy>'


FEED_SCREEN = screen(node(resource_id=f"{PACKAGE}:id/feed_tab"))
PROFILE_SCREEN = screen(node(resource_id=f"{PACKAGE}:id/profile_tab"))
OPTIONS_SCREEN = screen(node(desc="Options"))


def make_hook(hook_id: str, probe: Probe | None = None, *, status: str = "active") -> Hook:
    """A minimal valid hook. Only the id, the status and the probe matter here.

    Built directly rather than loaded, so `load_manifest`'s instrumentation check
    does not constrain fixtures that are about something else entirely.
    """
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
        status=status,
    )


DELTA_PROBE = Probe("logcat_delta", BLOCK_SIGNAL, "feed_tab")
DIALOG_PROBE = Probe(
    "ui_dialog",
    "Distraction-free settings",
    "profile_options_long_press",
    requires_two_directional_delta=False,
    note="not toggleable; the dialog opening is the whole proof",
)
STARTUP_PROBE = Probe(
    "startup_no_fatal",
    "NoSuchMethodError|VerifyError|ClassNotFoundException",
    "app_launch",
    requires_two_directional_delta=False,
    note="not toggleable; executing at startup without a fatal is the proof",
)


class FakeDevice:
    """Every device operation a probe needs, answered from canned state.

    Satisfies the `Device` Protocol. `sleep` records the request and returns
    immediately — a real probe dwells 15-25 seconds per direction, so a fake that
    honoured it would make this suite unrunnable.
    """

    def __init__(
        self,
        *,
        window: str = AWAKE,
        activities: str = RESUMED,
        logcat: str = "",
        screens: list[str] | None = None,
        pid: str = "30503\n",
    ):
        self.window = window
        self.activities = activities
        self.logcat = logcat
        self.screens = list(screens or [""])
        self.pid = pid
        self.calls: list[tuple] = []
        self.slept: list[float] = []
        self.ui_dumps = 0

    # -- Device -----------------------------------------------------------

    def shell(self, *args: str) -> str:
        self.calls.append(args)
        if args[:2] == ("dumpsys", "window"):
            return self.window
        if args[:2] == ("dumpsys", "activity"):
            return self.activities
        if args[:1] == ("pidof",):
            return self.pid
        return ""

    def logcat_clear(self) -> None:
        self.calls.append(("logcat", "-c"))

    def logcat_dump(self) -> str:
        self.calls.append(("logcat", "-d"))
        return self.logcat

    def ui_xml(self) -> str:
        self.calls.append(("ui_xml",))
        index = min(self.ui_dumps, len(self.screens) - 1)
        self.ui_dumps += 1
        return self.screens[index]

    def tap(self, x: int, y: int) -> None:
        self.calls.append(("tap", x, y))

    def long_press(self, x: int, y: int, duration_ms: int = 800) -> None:
        self.calls.append(("long_press", x, y, duration_ms))

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


def measurement(
    hook: Hook,
    state: str,
    canonical: int,
    *,
    raw: int | None = None,
    usable: bool = True,
    note: str = "",
) -> Measurement:
    assert hook.probe is not None
    return Measurement(
        hook.hook_id,
        hook.probe.surface,
        hook.probe.signal,
        state,
        SignalCount(canonical if raw is None else raw, canonical),
        navigated=True,
        usable=usable,
        note=note,
    )


# ------------------------------------------------------------------- counting


class CountSignalTests(unittest.TestCase):
    """`grep -c` over-counts by roughly two. This is why."""

    def test_a_live_block_is_counted_once_per_occurrence(self):
        """The positive control: without it every test below could pass at zero.

        A counter that returned 0 for everything would satisfy both confounder
        tests, so something has to prove the counter can count.
        """
        capture = "\n".join([STARTED, LIVE_BLOCK, "unrelated chatter", LIVE_BLOCK])
        count = count_signal(capture, BLOCK_SIGNAL)
        self.assertEqual((count.raw, count.canonical, count.contaminated), (2, 2, 0))
        # And it keeps the lines it counted, so a human can check the arithmetic.
        self.assertEqual(count.lines, (LIVE_BLOCK.strip(), LIVE_BLOCK.strip()))

    def test_the_network_failure_reason_echo_does_not_double_count(self):
        """Every live block emits the same text again as a field of its payload.

        The device numbers this reproduces are the ones in the module docstring:
        `logcat_feed_ON.txt` holds four blocks and reads 8 raw / 4 canonical.
        """
        capture = "\n".join(
            line
            for pair in range(4)
            for line in (
                LIVE_BLOCK.replace("01:43:51", f"01:43:5{pair}"),
                FAILURE_REASON_ECHO.replace("01:43:51", f"01:43:5{pair}"),
            )
        )
        count = count_signal(capture, BARE_SIGNAL)
        self.assertEqual((count.raw, count.canonical), (8, 4))
        self.assertEqual(count.contaminated, 4)
        # The difference stays visible rather than being quietly subtracted.
        self.assertEqual(
            count.to_dict(), {"raw": 8, "canonical": 4, "contaminated": 4}
        )

    def test_a_re_narrated_aware_trace_field_is_not_a_live_event(self):
        """A batched history flushed at a later cold start is not a new block.

        This capture is the Explore *off* side: the trace it carries narrates the
        previous phase. Counting it would report a leak that did not happen.
        """
        count = count_signal(AWARE_TRACE_FIELD, BARE_SIGNAL)
        self.assertEqual((count.raw, count.canonical), (1, 0))
        self.assertEqual(count.lines, ())

        # A control on the discriminator itself: the same text, un-indented and
        # opening its own payload, is an event and is counted. The rule is
        # positional, not a blocklist of field names, so it must not key on
        # `aware_trace` appearing at all.
        live = count_signal(AWARE_TRACE_FIELD.replace(": \t aware", ": aware"), BARE_SIGNAL)
        self.assertEqual(live.canonical, 1)

    def test_an_unindented_narration_of_a_past_block_is_not_a_live_event(self):
        """The third contaminating form, and the one indentation alone missed.

        `aware_trace_readable` spills its value across continuation lines, and
        logcat gives each its own `TAG:` prefix — so the narration arrives as an
        un-indented message body. What places it is the payload: the field entry
        that opened it has already ended the body of that log entry.

        Until 2026-08-02 these counted as live events, and the module docstring
        claimed off-side canonical counts of 0 that it did not produce:
        `logcat_explore_OFF.txt` read 3 raw / 2 canonical and
        `logcat_stories_OFF.txt` 2 / 2.
        """
        count = count_signal(AWARE_TRACE_NARRATION, BARE_SIGNAL)
        self.assertEqual((count.raw, count.canonical), (2, 0))
        self.assertEqual(count.lines, ())

    def test_the_body_of_a_payload_is_counted_however_far_into_it_the_event_sits(self):
        """The control for the rule above, and the reason it is not "first line".

        A real block payload opens with `FEED_NOT_LOADING`; the exception is the
        second line and the stack starts the third. A rule that counted only the
        first line of an entry, or that stopped at the first line of any kind,
        would count zero live blocks and read as a permanent clean pass.
        """
        count = count_signal(BLOCK_PAYLOAD, BARE_SIGNAL)
        self.assertEqual((count.raw, count.canonical), (2, 1))
        self.assertEqual(count.lines, (LIVE_BLOCK.strip(),))

        # And a second payload is a second event, not a continuation of the first:
        # the entry key changes with the timestamp.
        pair = BLOCK_PAYLOAD + "\n" + BLOCK_PAYLOAD.replace("01:43:51.185", "01:43:54.216")
        self.assertEqual(count_signal(pair, BARE_SIGNAL).canonical, 2)


# ------------------------------------------------------- the delta, both ways


class TwoDirectionalDeltaTests(unittest.TestCase):
    """No movement in either direction is INVALID, not a pass."""

    def setUp(self):
        self.hook = make_hook("tigon_url_block", DELTA_PROBE)

    def test_a_signal_that_moves_with_the_toggle_is_a_pass(self):
        """End to end on a fake phone: launch, navigate, count, claim.

        The positive control for this whole class, and the one test that drives
        `measure` all the way through to an `EvidenceClaim`. The hook declares
        the *bare* signal so the echo is inside the raw count and the claim can
        be shown to be built from the canonical one.
        """
        hook = make_hook("tigon_url_block", Probe("logcat_delta", BARE_SIGNAL, "feed_tab"))
        device = FakeDevice(
            logcat="\n".join(
                [
                    STARTED,
                    BLOCK_PAYLOAD,
                    BLOCK_PAYLOAD.replace("01:43:51.185", "01:43:54.216"),
                ]
            ),
            screens=[FEED_SCREEN],
        )
        runner = ProbeRunner(device, actor="device:P3227J000775")

        claim, taken = runner.run(hook, disabled=measurement(hook, "disabled", 0))

        self.assertIs(claim.verdict, Verdict.PASSED)
        self.assertIs(claim.kind, EvidenceKind.RUNTIME_PROBE)
        self.assertIs(claim.producer, Producer.DEVICE)
        self.assertEqual(claim.actor, "device:P3227J000775")
        self.assertEqual(claim.detail["enabled_observations"], 2)
        self.assertEqual(claim.detail["disabled_observations"], 0)
        self.assertIs(claim.detail["requires_two_directional_delta"], True)
        self.assertEqual([item.toggle_state for item in taken], ["enabled", "disabled"])

        # The echo did not inflate the on-side — the claim carries 2, not the 4
        # a grep would have found — and the capture window was cleared BEFORE the
        # launch: the blocks fire in the first seconds of startup, so clearing
        # afterwards would discard exactly the window being measured.
        self.assertEqual(taken[0].count.raw, 4)
        self.assertEqual(taken[0].count.canonical, 2)
        order = [call for call in device.calls if call[:1] in {("am",), ("monkey",), ("logcat",)}]
        self.assertEqual(
            order,
            [
                ("am", "force-stop", PACKAGE),
                ("logcat", "-c"),
                ("monkey", "-p", PACKAGE, "-c", "android.intent.category.LAUNCHER", "1"),
                ("logcat", "-d"),
            ],
        )

    def test_no_movement_in_either_direction_is_never_a_pass(self):
        """The Reels case. Zero on and zero off means the probe cannot see this hook.

        Equal non-zero counts are the same fact and must read the same way, or
        "it fired both times" would certify a hook the toggle does not govern.
        """
        runner = ProbeRunner(FakeDevice())
        for both in (0, 3):
            with self.subTest(observations=both):
                claim, _ = runner.run(
                    self.hook,
                    enabled=measurement(self.hook, "enabled", both),
                    disabled=measurement(self.hook, "disabled", both),
                )
                self.assertIs(claim.verdict, Verdict.INCONCLUSIVE)
                self.assertIsNot(claim.verdict, Verdict.PASSED)
                self.assertIn("not a pass", claim.summary)

    def test_a_delta_probe_refuses_to_conclude_from_the_on_side_alone(self):
        """Measuring only one direction is how a blind probe reads as a pass.

        Also pins that an unrecognised surface is refused rather than silently
        measuring nothing: both are "this probe cannot answer", stated up front.
        """
        runner = ProbeRunner(FakeDevice())
        with self.assertRaises(ValueError) as caught:
            runner.run(self.hook, enabled=measurement(self.hook, "enabled", 4))
        self.assertIn("two-directional delta", str(caught.exception))

        unknown = make_hook("elsewhere", Probe("logcat_delta", BLOCK_SIGNAL, "no_such_tab"))
        self.assertNotIn("no_such_tab", SURFACES)
        with self.assertRaises(ValueError) as caught:
            runner.run(unknown, enabled=measurement(unknown, "enabled", 1))
        self.assertIn("unknown probe surface", str(caught.exception))


# ------------------------------------------------ a measurement not taken


class RefusalTests(unittest.TestCase):
    """A zero the phone never had a chance to produce is not an observation."""

    def setUp(self):
        self.hook = make_hook("tigon_url_block", DELTA_PROBE)

    def test_a_screen_the_app_cannot_appear_on_produces_a_refusal_not_a_zero(self):
        """Behind a keyguard every probe reads zero in both directions."""
        for window, expected in (
            (LOCKED, "locked"),
            ("mAwake=false mDreamingLockscreen=false", "screen is off"),
        ):
            with self.subTest(window=window):
                device = FakeDevice(window=window, screens=[FEED_SCREEN])
                result = ProbeRunner(device).measure(
                    self.hook, SURFACES["feed_tab"], "enabled"
                )

                self.assertIs(result.usable, False)
                self.assertIn("not measured", result.note)
                self.assertIn(expected, result.note)
                self.assertEqual((result.count.raw, result.count.canonical), (0, 0))
                # Nothing was started, cleared or captured. The strongest form of
                # the guarantee: there is no capture to mistake for a measurement
                # in the first place.
                self.assertEqual(device.calls, [("dumpsys", "window")])
                self.assertEqual(device.slept, [])
                self.assertIs(result.to_dict()["usable"], False)

    def test_an_unusable_measurement_never_becomes_a_claim(self):
        """Either direction. The refusal is an exception, not a verdict.

        `ProbeNotTaken` rather than a `failed` claim, because "the phone was
        locked" and "the hook is broken" are different facts and only one of them
        belongs in the ledger.
        """
        runner = ProbeRunner(FakeDevice())
        locked = measurement(
            self.hook, "enabled", 0, usable=False, note="not measured: the device is locked"
        )
        blind = measurement(
            self.hook,
            "disabled",
            0,
            usable=False,
            note="the surface's entry control was not found on screen",
        )
        good = measurement(self.hook, "disabled", 0)

        with self.assertRaises(ProbeNotTaken) as caught:
            runner.run(self.hook, enabled=locked, disabled=good)
        self.assertIn("locked", str(caught.exception))

        with self.assertRaises(ProbeNotTaken) as caught:
            runner.run(
                self.hook, enabled=measurement(self.hook, "enabled", 4), disabled=blind
            )
        self.assertIn("entry control", str(caught.exception))

    def test_a_measurement_that_cannot_show_it_reached_the_surface_is_unusable(self):
        """A wrong app on screen, or no entry control, reads as a clean zero.

        Both are the same fact — the capture is not of the thing being measured —
        and the measurement says so instead of reporting the count it took.
        """
        elsewhere = FakeDevice(
            activities="topResumedActivity=ActivityRecord{1 u0 com.android.settings/.Main}",
            screens=[FEED_SCREEN],
            logcat=STARTED,
        )
        result = ProbeRunner(elsewhere).measure(self.hook, SURFACES["feed_tab"], "enabled")
        self.assertIs(result.usable, False)
        self.assertIn("was not foreground", result.note)
        self.assertIn("com.android.settings", result.note)

        absent = FakeDevice(screens=[screen()], logcat=STARTED)
        result = ProbeRunner(absent).measure(self.hook, SURFACES["feed_tab"], "enabled")
        self.assertIs(result.navigated, False)
        self.assertIs(result.usable, False)
        self.assertIn("entry control", result.note)
        self.assertNotIn(("tap", 108, 2194), absent.calls)

    def test_a_measurement_whose_foreground_is_unreadable_is_unusable(self):
        """"Nothing could be shown to be on screen" is not a passing check.

        `if foreground and foreground != self.package` skipped the check whenever
        `dumpsys activity activities` could not be parsed, so a run where the app
        might never have appeared was recorded as a usable zero. The note it
        would have written — `{foreground or 'unknown'} was` — was unreachable,
        which is what gave the omission away.
        """
        device = FakeDevice(activities="no resumed activity here", screens=[FEED_SCREEN])
        runner = ProbeRunner(device)
        self.assertEqual(runner.foreground_package(), "")

        result = runner.measure(self.hook, SURFACES["feed_tab"], "enabled")

        self.assertIs(result.usable, False)
        self.assertIn("unknown", result.note)

    def test_a_probe_that_is_not_a_delta_refuses_on_an_unusable_screen_too(self):
        """`startup_no_fatal` and `ui_dialog` took the walk without ever asking.

        Behind a keyguard the app cannot reach the foreground, so `StartupProbe`
        recorded `failed` — a measurement that could not be taken, filed as a
        defect in the hook. Both now refuse, as `ProbeRunner.measure` and
        `IdentityProbe.measure_identities` already did.
        """
        startup = make_hook("set_app_context", STARTUP_PROBE)
        settings = make_hook("install_settings_long_click", DIALOG_PROBE)
        locked = dict(
            window=LOCKED,
            activities="topResumedActivity=... com.android.systemui/.Keyguard",
            logcat=STARTED,
        )

        device = FakeDevice(**locked)
        with self.assertRaises(ProbeNotTaken) as caught:
            StartupProbe(device).claim(startup)
        self.assertIn("locked", str(caught.exception))
        self.assertEqual(device.calls, [("dumpsys", "window")])

        device = FakeDevice(screens=[PROFILE_SCREEN, OPTIONS_SCREEN, ""], **locked)
        with self.assertRaises(ProbeNotTaken) as caught:
            DialogProbe(device).claim(settings, f"{PACKAGE}:id/profile_tab")
        self.assertIn("locked", str(caught.exception))
        self.assertEqual(device.calls, [("dumpsys", "window")])

    def test_the_cli_reports_not_measured_instead_of_a_count(self):
        """The path a human actually runs, and where the silent 0/0 happened.

        Never prints a count for a measurement that did not happen, records
        `usable: false` in the JSON, and exits non-zero so a script cannot read
        the file as a result.
        """
        with tempfile.TemporaryDirectory() as workspace:
            manifest = Path(workspace) / "hooks.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "hooks": [
                            {
                                "hook_id": "tigon_url_block",
                                "intent": "block",
                                "tier": "robust",
                                "strategy": "s",
                                "hosts": [{"kind": "named", "descriptor": "LFoo;"}],
                                "anchor": ["nop"],
                                "payload": [
                                    "    invoke-static {}, "
                                    "Lcom/dfinstagram/probe;->h_tigon_url_block()V",
                                    "    # mark::tigon_url_block",
                                ],
                                "marker": "# mark::tigon_url_block",
                                "expected_marker_count": 1,
                                "probe": {
                                    "kind": "logcat_delta",
                                    "signal": BLOCK_SIGNAL,
                                    "surface": "feed_tab",
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            out = Path(workspace) / "measurements.json"

            def cli(device):
                stdout, stderr = io.StringIO(), io.StringIO()
                with mock.patch.object(probes, "AdbDevice", lambda *args: device):
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        code = probes.main(
                            ["--manifest", str(manifest), "--state", "enabled",
                             "--out", str(out)]
                        )
                return code, stdout.getvalue(), stderr.getvalue(), json.loads(
                    out.read_text(encoding="utf-8")
                )

            refused = cli(FakeDevice(window=LOCKED))
            # The positive control: the same command on a phone that CAN be
            # measured does print counts and exits zero. Without it,
            # `assertNotIn("raw=")` would pass on a CLI that never prints counts.
            measured = cli(
                FakeDevice(logcat="\n".join([STARTED, LIVE_BLOCK]), screens=[FEED_SCREEN])
            )

        code, stdout, stderr, written = refused
        self.assertEqual(code, 1)
        self.assertIn("NOT MEASURED", stdout)
        self.assertNotIn("canonical=", stdout)
        self.assertNotIn("raw=", stdout)
        self.assertIs(written["measurements"][0]["usable"], False)
        self.assertIn("not evidence", stderr)

        code, stdout, _, written = measured
        self.assertEqual(code, 0)
        self.assertIn("raw=  1 canonical=  1", stdout)
        self.assertNotIn("NOT MEASURED", stdout)
        self.assertIs(written["measurements"][0]["usable"], True)


# ------------------------------------------------------------------ freshness


class FreshnessTests(unittest.TestCase):
    def test_a_ui_dump_is_removed_before_it_is_taken_and_read_over_stdout(self):
        """A failed `uiautomator dump` leaves the previous file in place.

        So the guard is two-part: delete first, then read through stdout rather
        than pulling — a pull of a file that was never rewritten returns the last
        screen, and the probe would decide on the wrong UI.
        """

        class RecordingAdb(AdbDevice):
            def __init__(self) -> None:
                super().__init__()
                self.commands: list[tuple[str, ...]] = []

            def _run(self, *args: str, timeout: float = 60) -> str:
                self.commands.append(args)
                if args[:2] == ("shell", "cat"):
                    return "<hierarchy>fresh</hierarchy>"
                return ""

        device = RecordingAdb()
        xml = device.ui_xml()

        self.assertEqual(xml, "<hierarchy>fresh</hierarchy>")
        self.assertEqual(
            device.commands,
            [
                ("shell", "rm", "-f", "/sdcard/window_dump.xml"),
                ("shell", "uiautomator", "dump", "/sdcard/window_dump.xml"),
                ("shell", "cat", "/sdcard/window_dump.xml"),
            ],
        )
        self.assertNotIn("pull", [command[0] for command in device.commands])

    def test_an_adb_command_that_failed_raises_rather_than_returning_nothing(self):
        """The same rule one layer down: a failed capture is not an empty one.

        If `_run` swallowed a non-zero exit, `logcat_dump` would hand back "" and
        every probe over it would read a confident zero.
        """
        failed = mock.Mock(returncode=1, stdout="", stderr="device unauthorized")
        with mock.patch("subprocess.run", return_value=failed) as run:
            with self.assertRaises(RuntimeError) as caught:
                AdbDevice(serial="P3227J000775").logcat_dump()
        self.assertIn("device unauthorized", str(caught.exception))
        self.assertEqual(
            run.call_args.args[0], ["adb", "-s", "P3227J000775", "logcat", "-d"]
        )


# ---------------------------------------------------------------- attribution


class AttributionTests(unittest.TestCase):
    """One observation two hooks could have produced credits neither."""

    def test_hooks_declaring_one_signal_on_one_surface_are_grouped(self):
        legacy = make_hook("install_settings_long_click", DIALOG_PROBE)
        actionbar = make_hook("install_settings_long_click_actionbar", DIALOG_PROBE)
        tigon = make_hook("tigon_url_block", DELTA_PROBE)
        retired = make_hook("old_settings", DIALOG_PROBE, status="retired")
        unprobed = make_hook("set_app_context")

        groups = shared_signals([legacy, actionbar, tigon, retired, unprobed])

        self.assertEqual(
            groups,
            {
                (
                    "ui_dialog",
                    "Distraction-free settings",
                    "profile_options_long_press",
                ): ["install_settings_long_click", "install_settings_long_click_actionbar"]
            },
        )
        # A retired hook must not join the group: it would downgrade a live
        # hook's evidence on the strength of a patch that is not in the build.
        self.assertNotIn("old_settings", next(iter(groups.values())))

    def test_a_shared_observation_does_not_credit_either_hook(self):
        group = ["install_settings_long_click", "install_settings_long_click_actionbar"]
        runner = DialogProbe(FakeDevice())
        passed = EvidenceClaim(
            hook_id=group[0],
            kind=EvidenceKind.RUNTIME_PROBE,
            verdict=Verdict.PASSED,
            producer=Producer.DEVICE,
            actor=runner.actor,
            summary="long-pressing Options opened the dialog",
        )

        downgraded = attribute(passed, group)

        self.assertIs(downgraded.verdict, Verdict.INCONCLUSIVE)
        self.assertEqual(downgraded.detail["attribution"], "shared")
        self.assertEqual(downgraded.detail["shared_with"], [group[1]])
        self.assertIn("cannot say which hook produced it", downgraded.summary)
        # The observation is real, so nothing about it is discarded.
        self.assertIn("opened the dialog", downgraded.summary)

        # Control: a hook nothing else could be confused with keeps its pass, or
        # attribution would downgrade every claim in the ledger.
        alone = attribute(passed, [group[0]])
        self.assertIs(alone, passed)

        # And a claim that already failed is not *upgraded* into ambiguity: only
        # a pass is in doubt, a failure is a failure whoever owns the site.
        failed = EvidenceClaim(
            hook_id=group[0],
            kind=EvidenceKind.RUNTIME_PROBE,
            verdict=Verdict.FAILED,
            producer=Producer.DEVICE,
            actor=runner.actor,
            summary="the dialog did not open",
        )
        self.assertIs(attribute(failed, group).verdict, Verdict.FAILED)


# ------------------------------------------------------------- per-hook identity


class IdentityProbeTests(unittest.TestCase):
    """`hook_executed`: did THIS hook's site run? The probe the others approximate."""

    def test_only_the_probe_tag_names_a_hook_that_ran(self):
        log = "\n".join(
            [
                STARTED,
                "08-01 01:43:51.185 30503 30503 I DFInstaProbe: tigon_url_block",
                "08-01 01:43:51.186 30503 30503 I DFInstaProbe: set_app_context",
                # A duplicate is one hook, not two: the identity is a set.
                "08-01 01:43:52.001 30503 30503 I DFInstaProbe: tigon_url_block",
                # Another tag quoting a hook id is not that hook reporting itself.
                "08-01 01:43:53.000 30503 30503 I ActivityManager: tigon_url_block",
                # An empty message names nothing.
                "08-01 01:43:54.000 30503 30503 I DFInstaProbe: ",
            ]
        )

        self.assertEqual(
            IdentityProbe(FakeDevice()).executed(log),
            {"tigon_url_block", "set_app_context"},
        )

    def test_identities_are_only_collected_from_a_run_the_app_was_up_for(self):
        """The capture is thrown away rather than read as "no hook ran".

        Note the contrast with `StartupProbe`, which takes the same walk without
        ever asking whether the screen is usable — see `KnownDefectTests`.
        """
        log = "\n".join([STARTED, "08-01 01:43:51.185 30503 30503 I DFInstaProbe: h"])
        for name, device in (
            ("locked", FakeDevice(window=LOCKED, logcat=log)),
            ("app never came up", FakeDevice(activities="topResumed=other/.A", logcat=log)),
        ):
            with self.subTest(case=name):
                executed, usable, why = IdentityProbe(device).measure_identities()
                self.assertIs(usable, False)
                self.assertEqual(executed, set())
                self.assertTrue(why)

        # The positive control: on a usable phone the same walk does report.
        device = FakeDevice(logcat=log)
        executed, usable, why = IdentityProbe(device).measure_identities(dwell_seconds=25.0)
        self.assertIs(usable, True)
        self.assertEqual((executed, why), ({"h"}, ""))
        self.assertEqual(device.slept, [2, 25.0])  # stop settle, then the dwell

    def test_a_hook_that_never_announced_itself_is_inconclusive_not_failed(self):
        """Its site may simply not have been exercised. Those are different facts.

        `failed` would make an incomplete walkthrough look like a defect; `passed`
        would be the failure this stage exists to stop.
        """
        ran = make_hook("tigon_url_block", DELTA_PROBE)
        silent = make_hook("replace_reels_homecoming_endpoint", DELTA_PROBE)
        retired = make_hook("old_settings", DIALOG_PROBE, status="retired")
        runner = IdentityProbe(FakeDevice(), actor="device:P3227J000775")

        claims = runner.claims(
            [ran, silent, retired], {"tigon_url_block"}, visited=["feed_tab", "reels_tab"]
        )

        by_id = {claim.hook_id: claim for claim in claims}
        self.assertEqual(sorted(by_id), ["replace_reels_homecoming_endpoint", "tigon_url_block"])

        quiet = by_id["replace_reels_homecoming_endpoint"]
        self.assertIs(quiet.verdict, Verdict.INCONCLUSIVE)
        self.assertIsNot(quiet.verdict, Verdict.FAILED)
        self.assertIn("these are different", quiet.summary)
        self.assertIs(quiet.detail["executed"], False)
        # What a human needs to decide whether the run should have reached it.
        self.assertEqual(quiet.detail["surfaces_visited"], ["feed_tab", "reels_tab"])
        self.assertEqual(quiet.detail["hooks_that_ran"], ["tigon_url_block"])

        # The positive control: a hook that did report is a pass, from the device.
        self.assertIs(by_id["tigon_url_block"].verdict, Verdict.PASSED)
        self.assertIs(by_id["tigon_url_block"].producer, Producer.DEVICE)
        self.assertIs(by_id["tigon_url_block"].kind, EvidenceKind.RUNTIME_PROBE)
        self.assertEqual(by_id["tigon_url_block"].actor, "device:P3227J000775")


# ------------------------------------------------- absence needs a positive control


class AbsenceControlTests(unittest.TestCase):
    """Zero hits means nothing unless the same search demonstrably CAN find something."""

    def test_no_startup_error_proves_nothing_in_a_capture_that_missed_the_launch(self):
        hook = make_hook("set_app_context", STARTUP_PROBE)
        fatal = (
            "08-01 01:43:45.100 30503 30503 E AndroidRuntime: "
            "java.lang.NoSuchMethodError: no static method Lcom/dfinstagram/hooks;"
        )
        cases = {
            # No `Start proc` line: the search could not have succeeded, so its
            # coming back empty is a statement about the capture, not the hook.
            "capture missed the launch": ("", Verdict.INCONCLUSIVE),
            "clean start": (STARTED, Verdict.PASSED),
            "linkage error at startup": ("\n".join([STARTED, fatal]), Verdict.FAILED),
        }
        for name, (log, expected) in cases.items():
            with self.subTest(case=name):
                device = FakeDevice(logcat=log)
                claim, result = StartupProbe(device).claim(hook)
                self.assertIs(claim.verdict, expected)
                self.assertIs(result.control_found, log != "")
                self.assertIs(claim.producer, Producer.DEVICE)

        self.assertIn(
            "proves nothing",
            StartupProbe(FakeDevice(logcat="")).claim(hook)[0].summary,
        )

    def test_a_dialog_that_never_opened_is_failed_but_a_failed_dump_is_not(self):
        """The 430 hook was present, statically perfect, and never opened a dialog.

        The control is that the dump is this app's UI at all: without it, "the
        title is not there" is a statement about a dump that did not work.
        """
        hook = make_hook("install_settings_long_click", DIALOG_PROBE)
        opened = screen(node(desc="Distraction-free settings"))
        unchanged = screen(node(desc="Options"))
        cases = {
            "dialog opened": ([PROFILE_SCREEN, OPTIONS_SCREEN, opened], Verdict.PASSED),
            "dialog inert": ([PROFILE_SCREEN, OPTIONS_SCREEN, unchanged], Verdict.FAILED),
            "no ui dump at all": (["", "", ""], Verdict.INCONCLUSIVE),
        }
        for name, (screens, expected) in cases.items():
            with self.subTest(case=name):
                device = FakeDevice(screens=screens)
                claim, _ = DialogProbe(device).claim(hook, f"{PACKAGE}:id/profile_tab")
                self.assertIs(claim.verdict, expected)

        # The long-press is what the whole probe rests on; it must have happened.
        device = FakeDevice(screens=[PROFILE_SCREEN, OPTIONS_SCREEN, unchanged])
        DialogProbe(device).claim(hook, f"{PACKAGE}:id/profile_tab")
        self.assertIn(
            "long_press", [call[0] for call in device.calls if isinstance(call[0], str)]
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

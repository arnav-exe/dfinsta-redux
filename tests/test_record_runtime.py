"""Tests for `record_runtime`: the bridge from device measurements to claims.

`probes.main` writes *measurements*; the evidence ledger wants *claims*. Both
times a version's runtime evidence was recorded — 439, then 440 — that bridge was
a script written for the occasion and thrown away, which is a stage that cannot
be re-run and therefore a number nobody can reproduce. These tests pin the parts
of it that a throwaway script kept getting wrong.

No test here touches a phone, adb, or a network. Every one injects
:class:`tests.test_probes.FakeDevice` — the same fake `probes` is tested with,
reused rather than reinvented so the two files cannot drift apart about what a
device does. The CLI tests patch `record_runtime.AdbDevice` with it, so `main()`
runs end to end without a wire.

The three shapes under test correspond to the three modes:

  identity   walk named surfaces, ask each hook whether it announced itself
  startup    the absence probe for `set_app_context`
  delta      one hook, one toggle state, paired with its opposite when that lands
"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from dfinsta_pipeline import record_runtime
from dfinsta_pipeline.evidence import EvidenceClaim, EvidenceKind, Producer, Verdict
from dfinsta_pipeline.hook_manifest import Probe
from dfinsta_pipeline.probes import (
    PACKAGE,
    SURFACES,
    Measurement,
    SignalCount,
    UiUnavailable,
)
from dfinsta_pipeline.record_runtime import (
    RecordError,
    append,
    delta_claim,
    identity_claims,
    startup_claim,
)
from tests.test_probes import (
    AWAKE,
    BARE_SIGNAL,
    BLOCK_PAYLOAD,
    BLOCK_SIGNAL,
    DELTA_PROBE,
    DIALOG_PROBE,
    FEED_SCREEN,
    LOCKED,
    PROFILE_SCREEN,
    STARTED,
    STARTUP_PROBE,
    FakeDevice,
    make_hook,
    node,
    screen,
)

# --------------------------------------------------------------------- fixtures

REELS_SCREEN = screen(node(resource_id=f"{PACKAGE}:id/clips_tab"))

#: `com.dfinstagram.probe` logs one line per hook per process, on first execution.
PROBE_LINE = "08-01 01:43:51.185 30503 30503 I DFInstaProbe: {hook_id}"

#: Two blocks, each with the echo its payload also emits: 4 raw, 2 canonical.
TWO_BLOCKS = "\n".join(
    [STARTED, BLOCK_PAYLOAD, BLOCK_PAYLOAD.replace("01:43:51.185", "01:43:54.216")]
)

ACTOR = "device:P3227J000775"


def announced(*hook_ids: str) -> str:
    """A capture in which exactly these hooks reported their own execution."""
    return "\n".join([STARTED] + [PROBE_LINE.format(hook_id=name) for name in hook_ids])


def claim(hook_id: str, verdict: Verdict = Verdict.PASSED) -> EvidenceClaim:
    return EvidenceClaim(
        hook_id=hook_id,
        kind=EvidenceKind.RUNTIME_PROBE,
        verdict=verdict,
        producer=Producer.DEVICE,
        actor=ACTOR,
        summary=f"{hook_id} announced its own execution",
    )


def manifest_entry(hook_id: str, probe: Probe | None = None) -> dict:
    """A hook as the manifest file stores it, instrumented so it will load.

    Built as JSON rather than from `make_hook` because `main()` reads a manifest
    from disk, and `load_manifest` refuses any active hook whose payload does not
    call its own runtime identity.
    """
    entry: dict = {
        "hook_id": hook_id,
        "intent": "i",
        "tier": "robust",
        "strategy": "s",
        "hosts": [{"kind": "named", "descriptor": "LFoo;"}],
        "anchor": ["nop"],
        "payload": [
            f"    invoke-static {{}}, Lcom/dfinstagram/probe;->h_{hook_id}()V",
            f"    # mark::{hook_id}",
        ],
        "marker": f"# mark::{hook_id}",
        "expected_marker_count": 1,
    }
    if probe is not None:
        entry["probe"] = {
            "kind": probe.kind,
            "signal": probe.signal,
            "surface": probe.surface,
            "requires_two_directional_delta": probe.requires_two_directional_delta,
            "note": probe.note,
        }
    return entry


def write_manifest(path: Path, *entries: dict) -> Path:
    path.write_text(
        json.dumps({"schema_version": 1, "hooks": list(entries)}), encoding="utf-8"
    )
    return path


def half(hook_id: str, state: str, canonical: int, *, raw: int | None = None) -> dict:
    """One toggle state as the store keeps it: `to_dict`, as it lands in the file."""
    return Measurement(
        hook_id,
        "feed_tab",
        BARE_SIGNAL,
        state,
        SignalCount(canonical if raw is None else raw, canonical),
        navigated=True,
    ).to_dict()


def run_cli(device, argv: list[str]) -> tuple[int, str, str]:
    """`main()` with the phone replaced by a fake. Nothing here reaches adb."""
    stdout, stderr = io.StringIO(), io.StringIO()
    with mock.patch.object(record_runtime, "AdbDevice", lambda *args: device):
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = record_runtime.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


# ------------------------------------------------------------------ appending


class AppendTests(unittest.TestCase):
    """The ledger's rule is "a later claim wins", so re-measuring is an append."""

    def test_append_adds_to_the_evidence_file_rather_than_replacing_it(self):
        """A truncating write would erase the history of what was seen.

        And one JSON object per line, because the file is JSONL: a pretty-printed
        object would make every reader that iterates lines wrong.
        """
        with tempfile.TemporaryDirectory() as workspace:
            path = Path(workspace) / "evidence.jsonl"

            append(path, [claim("tigon_url_block"), claim("set_app_context")])
            append(path, [claim("install_settings_long_click", Verdict.INCONCLUSIVE)])

            lines = path.read_text(encoding="utf-8").splitlines()

        # Both sets are present, in the order they were written.
        self.assertEqual(len(lines), 3)
        self.assertEqual(
            [json.loads(line)["hook_id"] for line in lines],
            ["tigon_url_block", "set_app_context", "install_settings_long_click"],
        )
        # One object per line: each parses on its own, and none spans lines.
        for line in lines:
            with self.subTest(line=line[:40]):
                self.assertIsInstance(json.loads(line), dict)
                self.assertNotIn("\n", line)
        self.assertEqual(json.loads(lines[2])["verdict"], Verdict.INCONCLUSIVE.value)


# ------------------------------------------------------------------- identity


class IdentityClaimTests(unittest.TestCase):
    """One launch, a walk, and one claim per active hook."""

    def setUp(self):
        self.ran = make_hook("tigon_url_block", DELTA_PROBE)
        self.silent = make_hook("replace_reels_homecoming_endpoint", DELTA_PROBE)
        self.hooks = [self.ran, self.silent]

    def visited(self, claims: list[EvidenceClaim]) -> list[str]:
        surfaces = {tuple(item.detail["surfaces_visited"]) for item in claims}
        self.assertEqual(len(surfaces), 1, "every claim reports the same walkthrough")
        return list(next(iter(surfaces)))

    def test_identity_claims_walk_the_named_surfaces_and_report_the_ones_reached(self):
        """`surfaces_visited` is `app_launch` plus exactly what was actually reached.

        It is what a human uses to decide whether a silent hook's site should have
        been exercised at all, so a surface that was asked for and not found must
        not appear in it — the list is evidence, not an echo of the request.
        """
        device = FakeDevice(
            logcat=announced("tigon_url_block"),
            # feed and reels are on screen; explore is not.
            screens=[FEED_SCREEN, REELS_SCREEN, screen()],
        )

        claims = identity_claims(
            device,
            self.hooks,
            ACTOR,
            visit=["feed_tab", "reels_tab", "explore_tab"],
            dwell_seconds=25.0,
        )

        self.assertEqual(
            self.visited(claims), ["app_launch", "feed_tab", "reels_tab"]
        )
        # Each named surface was actually attempted — one dump apiece — and the
        # two that were found were tapped.
        self.assertEqual(device.ui_dumps, 3)
        self.assertEqual(len([call for call in device.calls if call[0] == "tap"]), 2)
        # The capture was cleared before the launch, not after.
        order = [
            call for call in device.calls if call[:1] in {("am",), ("monkey",), ("logcat",)}
        ]
        self.assertEqual(
            order,
            [
                ("am", "force-stop", PACKAGE),
                ("logcat", "-c"),
                ("monkey", "-p", PACKAGE, "-c", "android.intent.category.LAUNCHER", "1"),
                ("logcat", "-d"),
            ],
        )

        by_id = {item.hook_id: item for item in claims}
        self.assertEqual(sorted(by_id), sorted(hook.hook_id for hook in self.hooks))
        self.assertIs(by_id["tigon_url_block"].verdict, Verdict.PASSED)
        self.assertIs(by_id["replace_reels_homecoming_endpoint"].verdict, Verdict.INCONCLUSIVE)
        self.assertEqual(by_id["tigon_url_block"].actor, ACTOR)
        self.assertIs(by_id["tigon_url_block"].producer, Producer.DEVICE)

        # The control on "walks the surfaces named in `visit`": with none named,
        # nothing is navigated and only the launch is claimed as reached.
        quiet = FakeDevice(logcat=announced("tigon_url_block"), screens=[FEED_SCREEN])
        self.assertEqual(
            self.visited(identity_claims(quiet, self.hooks, ACTOR)), ["app_launch"]
        )
        self.assertEqual(quiet.ui_dumps, 0)

    def test_an_unknown_surface_name_is_refused_and_the_known_ones_are_named(self):
        """An unrecognised surface must not silently visit nothing.

        Left to itself it would produce a full set of `inconclusive` claims whose
        `surfaces_visited` says the walk happened, which is the shape of a
        successful run — so the mistake would be invisible in the evidence.
        """
        device = FakeDevice(logcat=announced("tigon_url_block"), screens=[FEED_SCREEN])

        with self.assertRaises(RecordError) as caught:
            identity_claims(device, self.hooks, ACTOR, visit=["feed_tab", "profile_tab"])

        message = str(caught.exception)
        self.assertIn("profile_tab", message)
        for name in SURFACES:
            with self.subTest(known=name):
                self.assertIn(name, message)
        # Refused before anything was started, so there is no capture to mistake
        # for a walkthrough that happened.
        self.assertEqual(device.calls, [])

    def test_identity_claims_refuse_a_run_the_app_was_never_on_screen_for(self):
        """A locked phone and a foreign foreground are both "not measured".

        Neither may come back as claims: every hook would read `inconclusive`,
        which is indistinguishable from a walkthrough that ran and saw nothing.
        """
        locked = FakeDevice(
            window=LOCKED, logcat=announced("tigon_url_block"), screens=[FEED_SCREEN]
        )
        with self.assertRaises(RecordError) as caught:
            identity_claims(locked, self.hooks, ACTOR, visit=["feed_tab"])
        self.assertIn("not measured", str(caught.exception))
        self.assertIn("locked", str(caught.exception))
        # Nothing was force-stopped, cleared or captured.
        self.assertEqual(locked.calls, [("dumpsys", "window")])

        elsewhere = FakeDevice(
            activities="topResumedActivity=ActivityRecord{1 u0 com.android.settings/.Main}",
            logcat=announced("tigon_url_block"),
            screens=[FEED_SCREEN],
        )
        with self.assertRaises(RecordError) as caught:
            identity_claims(elsewhere, self.hooks, ACTOR, visit=["feed_tab"])
        self.assertIn("not measured", str(caught.exception))
        self.assertIn("was not foreground", str(caught.exception))
        self.assertIn("com.android.settings", str(caught.exception))

        # The control: the same walk on a phone that CAN be measured returns
        # claims, so the refusals above are about the phone and not the fixture.
        good = FakeDevice(logcat=announced("tigon_url_block"), screens=[FEED_SCREEN])
        self.assertEqual(len(identity_claims(good, self.hooks, ACTOR, visit=["feed_tab"])), 2)

    def test_a_surface_whose_control_cannot_be_read_is_skipped_not_fatal(self):
        """The case the real 440 run hit, mid-walkthrough.

        `uiautomator dump` cannot reach idle while Reels plays or a blocked feed
        retries, and that says nothing about the app. Losing the whole capture to
        it would throw away every hook that DID announce itself; the honest
        degradation is one surface missing from `surfaces_visited`, which is
        exactly what the claim's "its site may not have been reached" is about.
        """
        device = FakeDevice(
            logcat=announced("tigon_url_block"),
            screens=[
                FEED_SCREEN,
                UiUnavailable("the UI hierarchy could not be read: could not get idle state"),
                PROFILE_SCREEN,
            ],
        )

        claims = identity_claims(
            device,
            self.hooks,
            ACTOR,
            visit=["feed_tab", "reels_tab", "profile_options_long_press"],
        )

        # Claims were still produced, and the surfaces after the failure were
        # still visited.
        self.assertEqual(len(claims), 2)
        self.assertEqual(
            self.visited(claims),
            ["app_launch", "feed_tab", "profile_options_long_press"],
        )
        self.assertNotIn("reels_tab", self.visited(claims))
        self.assertEqual(device.ui_dumps, 3)
        by_id = {item.hook_id: item for item in claims}
        self.assertIs(by_id["tigon_url_block"].verdict, Verdict.PASSED)


# -------------------------------------------------------------------- startup


class StartupClaimTests(unittest.TestCase):
    """`set_app_context` executes at process start or the app does not start."""

    def test_startup_claim_refuses_a_manifest_with_no_set_app_context_hook(self):
        """The probe is about one named hook; without it there is nothing to probe.

        Guessing another hook, or returning nothing, would both report a startup
        result for a build whose startup was never measured.
        """
        device = FakeDevice(logcat=STARTED)
        hooks = [make_hook("tigon_url_block", DELTA_PROBE)]

        with self.assertRaises(RecordError) as caught:
            startup_claim(device, hooks, ACTOR)

        self.assertIn("set_app_context", str(caught.exception))
        # Refused before the app was touched.
        self.assertEqual(device.calls, [])

        # The control: with the hook present the same call measures and claims.
        hooks.append(make_hook("set_app_context", STARTUP_PROBE))
        result = startup_claim(FakeDevice(logcat=STARTED), hooks, ACTOR)
        self.assertEqual(result.hook_id, "set_app_context")
        self.assertIs(result.verdict, Verdict.PASSED)
        self.assertIs(result.kind, EvidenceKind.RUNTIME_PROBE)
        self.assertEqual(result.actor, ACTOR)


# ---------------------------------------------------------------------- delta


class DeltaStoreTests(unittest.TestCase):
    """Moving the toggle is a human action between two invocations."""

    def setUp(self):
        self.hook = make_hook(
            "tigon_url_block", Probe("logcat_delta", BARE_SIGNAL, "feed_tab")
        )

    def test_one_stored_state_is_not_yet_a_claim_and_two_states_are(self):
        """`None` means "not yet" — neither a claim nor an error.

        A run that took the on-side and guessed the off-side would be recording a
        number nobody measured, which is the failure `probe_claim` refuses at the
        schema level. So the half waits until its opposite lands.
        """
        device = FakeDevice()
        one_sided = {
            self.hook.hook_id: {"enabled": half("tigon_url_block", "enabled", 2, raw=4)}
        }

        self.assertIsNone(delta_claim([self.hook], self.hook, one_sided, ACTOR, device))
        # Not even asked: pairing does not begin until both halves exist.
        self.assertEqual(device.calls, [])

        # The other direction is the same fact.
        other = {self.hook.hook_id: {"disabled": half("tigon_url_block", "disabled", 0)}}
        self.assertIsNone(delta_claim([self.hook], self.hook, other, ACTOR, device))
        self.assertIsNone(delta_claim([self.hook], self.hook, {}, ACTOR, device))

        both = {
            self.hook.hook_id: {
                "enabled": half("tigon_url_block", "enabled", 2, raw=4),
                "disabled": half("tigon_url_block", "disabled", 0),
            }
        }
        paired = delta_claim([self.hook], self.hook, both, ACTOR, device)

        self.assertIsNotNone(paired)
        self.assertIs(paired.verdict, Verdict.PASSED)
        # Built from the canonical count, not the raw one the echo inflates.
        self.assertEqual(paired.detail["enabled_observations"], 2)
        self.assertEqual(paired.detail["disabled_observations"], 0)

    def test_a_half_finished_probe_writes_nothing_to_the_evidence_file(self):
        """"Not yet" is reported to the human, never recorded as a result.

        The file staying absent is the load-bearing part: a claim on disk is a
        claim in the ledger, and there is no verdict to be had from one direction.
        """
        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            manifest = write_manifest(
                root / "hooks.json",
                manifest_entry(
                    "tigon_url_block", Probe("logcat_delta", BARE_SIGNAL, "feed_tab")
                ),
            )
            out = root / "evidence.jsonl"
            measurements = root / "measurements.json"
            argv = [
                "delta",
                "--manifest", str(manifest),
                "--out", str(out),
                "--hook", "tigon_url_block",
                "--measurements", str(measurements),
                "--state",
            ]

            code, stdout, _ = run_cli(
                FakeDevice(logcat=TWO_BLOCKS, screens=[FEED_SCREEN]), argv + ["enabled"]
            )

            self.assertEqual(code, 0)
            self.assertIn("move the toggle", stdout)
            self.assertIs(out.exists(), False)
            store = json.loads(measurements.read_text(encoding="utf-8"))
            self.assertEqual(sorted(store["tigon_url_block"]), ["enabled"])
            self.assertEqual(store["tigon_url_block"]["enabled"]["canonical"], 2)

            # ...and when the human has moved the toggle and measured the other
            # direction, the same store becomes exactly one claim.
            code, stdout, _ = run_cli(
                FakeDevice(logcat=STARTED, screens=[FEED_SCREEN]), argv + ["disabled"]
            )

            self.assertEqual(code, 0)
            lines = out.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            written = json.loads(lines[0])

        self.assertEqual(written["hook_id"], "tigon_url_block")
        self.assertEqual(written["verdict"], Verdict.PASSED.value)
        self.assertEqual(written["detail"]["enabled_observations"], 2)
        self.assertEqual(written["detail"]["disabled_observations"], 0)

    def test_an_unusable_measurement_is_never_stored(self):
        """A zero the phone never had a chance to produce is not evidence.

        And it must not become evidence later by being kept: a stored half is
        indistinguishable from a measured one the moment its opposite lands, so
        the refusal has to happen before the write, not at pairing time.
        """
        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            manifest = write_manifest(
                root / "hooks.json",
                manifest_entry(
                    "tigon_url_block", Probe("logcat_delta", BARE_SIGNAL, "feed_tab")
                ),
            )
            out = root / "evidence.jsonl"
            measurements = root / "measurements.json"
            # One good half already on disk. The unusable one would pair with it.
            measurements.write_text(
                json.dumps(
                    {"tigon_url_block": {"enabled": half("tigon_url_block", "enabled", 2)}},
                    indent=1,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            before = measurements.read_text(encoding="utf-8")

            code, stdout, stderr = run_cli(
                FakeDevice(window=LOCKED, screens=[FEED_SCREEN]),
                [
                    "delta",
                    "--manifest", str(manifest),
                    "--out", str(out),
                    "--hook", "tigon_url_block",
                    "--measurements", str(measurements),
                    "--state", "disabled",
                ],
            )

            after = measurements.read_text(encoding="utf-8")
            written = json.loads(after)
            out_exists = out.exists()

        self.assertEqual(code, 1)
        self.assertIn("not measured", stderr)
        # The store did not gain the half, and nothing else about it moved.
        self.assertEqual(sorted(written["tigon_url_block"]), ["enabled"])
        self.assertNotIn("disabled", written["tigon_url_block"])
        self.assertEqual(after, before)
        # And no claim was made from the pairing that did not happen.
        self.assertIs(out_exists, False)
        self.assertNotIn("PASSED", stdout)

    def test_a_revived_measurement_round_trips_and_recomputes_contamination(self):
        """A stored half has to be a real half, not a dict that looks like one.

        `contaminated` is derived rather than read back: it is raw minus canonical
        and nothing else, so a hand-edited file cannot make an inflated raw count
        look clean — or a clean one look contaminated.
        """
        original = Measurement(
            "tigon_url_block",
            "feed_tab",
            BARE_SIGNAL,
            "enabled",
            SignalCount(8, 4),
            navigated=True,
            usable=True,
            note="",
        )

        revived = record_runtime._revive(original.to_dict())

        self.assertEqual(revived.to_dict(), original.to_dict())
        self.assertEqual(revived.hook_id, "tigon_url_block")
        self.assertEqual((revived.count.raw, revived.count.canonical), (8, 4))
        self.assertIs(revived.navigated, True)
        self.assertIs(revived.usable, True)

        # A stored `contaminated` that disagrees with raw minus canonical loses.
        tampered = dict(original.to_dict(), contaminated=0)
        self.assertEqual(tampered["raw"], 8)
        self.assertEqual(tampered["canonical"], 4)

        from_tampered = record_runtime._revive(tampered)

        self.assertEqual(from_tampered.count.contaminated, 4)
        self.assertEqual(from_tampered.to_dict()["contaminated"], 4)
        self.assertEqual(from_tampered.count.raw, 8)

    def test_a_signal_two_hooks_share_credits_neither_of_them(self):
        """The observation is real; which hook produced it is not established.

        Two settings hooks declare the same dialog and three Reels hooks share one
        failure log. Recording `passed` for each is how a completely inert hook
        inherits its neighbour's evidence — which has already happened here once.
        """
        shared = Probe("logcat_delta", BLOCK_SIGNAL, "feed_tab")
        mine = make_hook("install_settings_long_click", shared)
        theirs = make_hook("install_settings_long_click_actionbar", shared)
        alone = make_hook("set_app_context", DIALOG_PROBE)
        store = {
            mine.hook_id: {
                "enabled": half(mine.hook_id, "enabled", 3),
                "disabled": half(mine.hook_id, "disabled", 0),
            },
            alone.hook_id: {
                "enabled": half(alone.hook_id, "enabled", 3),
                "disabled": half(alone.hook_id, "disabled", 0),
            },
        }

        downgraded = delta_claim([mine, theirs, alone], mine, store, ACTOR, FakeDevice())

        self.assertIs(downgraded.verdict, Verdict.INCONCLUSIVE)
        self.assertIsNot(downgraded.verdict, Verdict.PASSED)
        self.assertEqual(downgraded.detail["attribution"], "shared")
        self.assertEqual(downgraded.detail["shared_with"], [theirs.hook_id])
        self.assertIn("cannot say which hook produced it", downgraded.summary)
        # The observation itself is not discarded.
        self.assertEqual(downgraded.detail["enabled_observations"], 3)

        # The control: a hook nothing else could be confused with keeps its pass,
        # or attribution would downgrade every claim in the ledger.
        kept = delta_claim([mine, theirs, alone], alone, store, ACTOR, FakeDevice())
        self.assertIs(kept.verdict, Verdict.PASSED)
        self.assertNotIn("attribution", kept.detail)


# ------------------------------------------------------------------------ cli


class CommandLineTests(unittest.TestCase):
    """What a human runs, and what a script reads afterwards."""

    def test_main_writes_nothing_and_exits_one_when_the_recording_is_refused(self):
        """A non-zero exit with an empty file, so a caller cannot read a result.

        Writing partial claims and returning 1 would leave the ledger holding
        evidence from a run that reported itself as failed.
        """
        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            manifest = write_manifest(
                root / "hooks.json",
                manifest_entry("tigon_url_block", DELTA_PROBE),
                manifest_entry("set_app_context"),
            )
            out = root / "evidence.jsonl"
            argv = ["identity", "--manifest", str(manifest), "--out", str(out),
                    "--visit", "feed_tab", "--dwell", "0"]

            code, stdout, stderr = run_cli(
                FakeDevice(window=LOCKED, screens=[FEED_SCREEN]), argv
            )

            self.assertEqual(code, 1)
            self.assertIn("error:", stderr)
            self.assertIn("locked", stderr)
            self.assertIs(out.exists(), False)
            self.assertEqual(stdout, "")

            # The control: the same command on a phone that CAN be measured exits
            # zero and appends the claims it made.
            code, stdout, stderr = run_cli(
                FakeDevice(logcat=announced("tigon_url_block"), screens=[FEED_SCREEN]),
                argv,
            )

            lines = out.read_text(encoding="utf-8").splitlines()

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(len(lines), 2)
        self.assertEqual(
            sorted(json.loads(line)["hook_id"] for line in lines),
            ["set_app_context", "tigon_url_block"],
        )
        self.assertIn("tigon_url_block", stdout)
        by_id = {json.loads(line)["hook_id"]: json.loads(line) for line in lines}
        self.assertEqual(by_id["tigon_url_block"]["verdict"], Verdict.PASSED.value)
        self.assertEqual(by_id["set_app_context"]["verdict"], Verdict.INCONCLUSIVE.value)

    def test_main_records_a_startup_claim_and_exits_zero(self):
        """The second mode end to end, so `--out` is proven for more than identity."""
        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            manifest = write_manifest(
                root / "hooks.json", manifest_entry("set_app_context", STARTUP_PROBE)
            )
            out = root / "evidence.jsonl"

            code, stdout, _ = run_cli(
                FakeDevice(logcat=STARTED, window=AWAKE),
                ["startup", "--manifest", str(manifest), "--out", str(out)],
            )

            lines = out.read_text(encoding="utf-8").splitlines()

        self.assertEqual(code, 0)
        self.assertEqual(len(lines), 1)
        written = json.loads(lines[0])
        self.assertEqual(written["hook_id"], "set_app_context")
        self.assertEqual(written["verdict"], Verdict.PASSED.value)
        self.assertEqual(written["producer"], Producer.DEVICE.value)
        self.assertIn("set_app_context", stdout)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

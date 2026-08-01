"""Stage 9: measure on the device whether a hook actually does anything.

This is the only stage that can tell a working hook from an inert one, and it is
the reason the rest of the pipeline refuses to call a build good without it.
Three DFInsta patches have shipped that passed every static assertion and did
nothing at runtime: the 340 `minshop`/`minishops` substitution that could never
match, the 430 settings hook that was perfect statically and dead because a
MobileConfig flag selected the other action-bar implementation, and a verifier
searching DEX bytes for a string form DEX does not store.

Two rules here are load-bearing and both were learned by getting them wrong.

**Counting is not grepping.** ``grep -c "Blocked by DFInsta"`` over-counts by
roughly two. Every live block emits the exception line *and* a
``NETWORK_FAILURE_REASON`` field carrying the same text, and Instagram
additionally batches an ``aware_trace`` history that re-narrates past events at a
*later* cold start — so a phase can inherit hits belonging to the previous one.
Measured on the device: 8 raw / 4 canonical with the toggle on, and on the
off-side 3 raw / 0 canonical for Explore and 2 raw / 0 for Stories. Raw counts
alone would have reported a leak that does not exist.

The discriminator is structural rather than a heuristic: the live exception is
the message body, while both contaminating forms are tab-indented field entries
inside an event payload. :func:`count_signal` returns both numbers so the
difference stays visible instead of being quietly subtracted.

**A delta must move both ways.** Zero signal with the toggle on *and* zero with
it off does not mean the hook passed; it means the probe cannot see this hook.
That is exactly what block-counting does to Reels, because `replaceReelsEndpoint`
blanks the endpoint literal upstream of `throwIfBlocked`, so the path never
matches the blocklist and no exception is ever produced. The Reels hooks
therefore declare a different signal — a `ClipsViewerPerfLogger` request failure
— and :func:`dfinsta_pipeline.evidence.probe_claim` returns ``inconclusive``, not
``passed``, whenever the two directions agree.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

from .evidence import EvidenceClaim, EvidenceLedger, probe_claim
from .hook_manifest import Hook, Probe, load_manifest

PACKAGE = "com.instagram.android"


class ProbeNotTaken(RuntimeError):
    """The measurement could not be made, so there is no result to interpret.

    Distinct from a probe that ran and saw nothing: that is a real observation
    and may be a genuine `inconclusive`. This means the phone was locked, the app
    never reached the foreground, or the surface was not on screen — and a zero
    from those conditions must not enter the ledger at all.
    """

#: A logcat line is `date time pid tid LEVEL TAG: message`. Only the message is
#: interesting, and only whether it *starts* the entry or is a field inside one.
LOGCAT_LINE = re.compile(
    r"^\s*\S+\s+\S+\s+\d+\s+\d+\s+[VDIWEFS]\s+(?P<tag>[^:]*):\s?(?P<message>.*)$"
)


@dataclass(frozen=True)
class SignalCount:
    """How many times a probe's signal appeared, and how many of those were real."""

    raw: int
    canonical: int
    lines: tuple[str, ...] = ()

    @property
    def contaminated(self) -> int:
        return self.raw - self.canonical

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "canonical": self.canonical,
            "contaminated": self.contaminated,
        }


def count_signal(logcat: str, signal: str) -> SignalCount:
    """Count a probe signal in logcat, separating live events from re-narration.

    ``canonical`` counts only lines whose *message* is the event — an un-indented
    message body. A field entry inside an event payload is tab-indented, which is
    what both contaminating forms look like:

        E IgFunctionalErrorEvent: java.io.IOException: Blocked by DFInsta setting
        E IgFunctionalErrorEvent: \t NETWORK_FAILURE_REASON = Blocked by DFInsta setting
        E IgFunctionalErrorEvent: \t aware_trace_readable = During the current app session…

    The first is a block that happened. The second is the same block restated,
    and the third belongs to a batched history flushed one phase late.
    """
    pattern = re.compile(signal)
    raw = 0
    canonical = 0
    kept: list[str] = []
    for line in logcat.splitlines():
        if not pattern.search(line):
            continue
        raw += 1
        match = LOGCAT_LINE.match(line)
        message = match.group("message") if match else line
        # An indented message is a field inside an event, not the event.
        if message[:1] in {" ", "\t"}:
            continue
        canonical += 1
        kept.append(line.strip())
    return SignalCount(raw, canonical, tuple(kept))


# ---------------------------------------------------------------------- device


class Device(Protocol):
    """The device operations a probe needs. A Protocol so tests need no phone."""

    def shell(self, *args: str) -> str: ...

    def logcat_clear(self) -> None: ...

    def logcat_dump(self) -> str: ...

    def ui_xml(self) -> str: ...

    def tap(self, x: int, y: int) -> None: ...

    def long_press(self, x: int, y: int, duration_ms: int = 800) -> None: ...

    def sleep(self, seconds: float) -> None: ...


class AdbDevice:
    """A real phone, over ADB."""

    def __init__(self, adb: str = "adb", serial: str | None = None):
        self._adb = adb
        self._serial = serial

    def _command(self, *args: str) -> list[str]:
        base = [self._adb]
        if self._serial:
            base += ["-s", self._serial]
        return base + list(args)

    def _run(self, *args: str, timeout: float = 60) -> str:
        completed = subprocess.run(
            self._command(*args),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"adb {' '.join(args)} failed ({completed.returncode}): "
                f"{completed.stderr.strip()[:400]}"
            )
        return completed.stdout

    def shell(self, *args: str) -> str:
        return self._run("shell", *args)

    def logcat_clear(self) -> None:
        self._run("logcat", "-c")

    def logcat_dump(self) -> str:
        return self._run("logcat", "-d", timeout=180)

    def ui_xml(self) -> str:
        # A failed dump leaves the PREVIOUS file in place, so a pull silently
        # returns stale data. Remove it first and read through stdout rather than
        # pulling, so there is no file to go stale.
        self.shell("rm", "-f", "/sdcard/window_dump.xml")
        self.shell("uiautomator", "dump", "/sdcard/window_dump.xml")
        return self.shell("cat", "/sdcard/window_dump.xml")

    def tap(self, x: int, y: int) -> None:
        self.shell("input", "tap", str(x), str(y))

    def long_press(self, x: int, y: int, duration_ms: int = 800) -> None:
        self.shell("input", "swipe", str(x), str(y), str(x), str(y), str(duration_ms))

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


# ------------------------------------------------------------------- measuring


@dataclass(frozen=True)
class Surface:
    """How to get the app to the place a probe measures."""

    name: str
    resource_id: str | None = None
    content_desc: str | None = None
    dwell_seconds: float = 20.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "surface": self.name,
            "resource_id": self.resource_id,
            "content_desc": self.content_desc,
            "dwell_seconds": self.dwell_seconds,
        }


#: Where each probe surface lives. Selectors come from the behaviour contract,
#: never from pixel coordinates, which are capture-specific.
SURFACES: Mapping[str, Surface] = {
    "app_launch": Surface("app_launch", dwell_seconds=15.0),
    "feed_tab": Surface("feed_tab", resource_id=f"{PACKAGE}:id/feed_tab"),
    "reels_tab": Surface("reels_tab", resource_id=f"{PACKAGE}:id/clips_tab"),
    "explore_tab": Surface("explore_tab", resource_id=f"{PACKAGE}:id/search_tab"),
    "profile_options_long_press": Surface(
        "profile_options_long_press", resource_id=f"{PACKAGE}:id/profile_tab"
    ),
}

BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def node_centre(node_xml: str) -> tuple[int, int] | None:
    match = BOUNDS.search(node_xml)
    if not match:
        return None
    left, top, right, bottom = (int(value) for value in match.groups())
    return (left + right) // 2, (top + bottom) // 2


def find_node(ui_xml: str, *, resource_id: str | None = None, content_desc: str | None = None) -> str | None:
    """The first <node> element matching a selector, as raw XML."""
    for element in re.findall(r"<node[^>]*/?>", ui_xml):
        if resource_id and f'resource-id="{resource_id}"' not in element:
            continue
        if content_desc and f'content-desc="{content_desc}"' not in element:
            continue
        return element
    return None


@dataclass
class Measurement:
    """One direction of one probe: what was done and what was seen."""

    hook_id: str
    surface: str
    signal: str
    toggle_state: str
    count: SignalCount
    navigated: bool
    #: False when the measurement could not be taken — screen locked, app not
    #: foreground, surface control absent. A zero from an unusable measurement is
    #: not the same fact as a zero from a working one, and must never be counted
    #: as evidence.
    usable: bool = True
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook_id,
            "surface": self.surface,
            "signal": self.signal,
            "toggle_state": self.toggle_state,
            "navigated": self.navigated,
            "usable": self.usable,
            "note": self.note,
            **self.count.to_dict(),
        }


class ProbeRunner:
    """Drives one probe on a device and turns the result into evidence."""

    def __init__(self, device: Device, package: str = PACKAGE, actor: str | None = None):
        self.device = device
        self.package = package
        self.actor = actor or "device"

    # -- primitives ------------------------------------------------------

    def stop(self) -> None:
        self.device.shell("am", "force-stop", self.package)
        self.device.sleep(2)

    def launch(self) -> None:
        self.device.shell(
            "monkey", "-p", self.package, "-c", "android.intent.category.LAUNCHER", "1"
        )

    def screen_is_usable(self) -> tuple[bool, str]:
        """Is the device awake and unlocked?

        Checked because it is the difference between "no blocks occurred" and
        "the app was never on screen". Behind a keyguard every probe reads zero
        in both directions, which is a probe that measured nothing — and a stage
        that reports zeros without saying why is how silence gets mistaken for
        success.
        """
        state = self.device.shell("dumpsys", "window")
        awake = "mAwake=true" in state or "screenState=SCREEN_STATE_ON" in state
        locked = "mDreamingLockscreen=true" in state or "isStatusBarKeyguard=true" in state
        if not awake:
            return False, "the screen is off"
        if locked:
            return False, "the device is locked; the app cannot reach the foreground"
        return True, ""

    def foreground_package(self) -> str:
        """The package currently resumed, so a probe can prove the app was up."""
        output = self.device.shell("dumpsys", "activity", "activities")
        match = re.search(r"topResumedActivity.*?\{[^}]*?\s([\w.]+)/", output)
        if match:
            return match.group(1)
        match = re.search(r"ResumedActivity.*?\s([\w.]+)/", output)
        return match.group(1) if match else ""

    def navigate(self, surface: Surface) -> bool:
        """Tap the surface's entry control. False when it could not be found."""
        if surface.resource_id is None and surface.content_desc is None:
            return True  # app_launch: being started IS the surface
        xml = self.device.ui_xml()
        node = find_node(
            xml, resource_id=surface.resource_id, content_desc=surface.content_desc
        )
        if node is None:
            return False
        centre = node_centre(node)
        if centre is None:
            return False
        self.device.tap(*centre)
        return True

    def measure(self, hook: Hook, surface: Surface, toggle_state: str) -> Measurement:
        """One direction: restart clean, drive to the surface, count the signal.

        Order matters. `logcat -c` happens BEFORE the launch, not after: the
        blocks this measures fire in the first seconds of startup, so clearing
        after waiting for the app to come up discards exactly the window being
        measured. That mistake reads as a clean zero.
        """
        assert hook.probe is not None
        usable, why = self.screen_is_usable()
        if not usable:
            return Measurement(
                hook.hook_id,
                surface.name,
                hook.probe.signal,
                toggle_state,
                SignalCount(0, 0),
                navigated=False,
                usable=False,
                note=f"not measured: {why}",
            )

        self.stop()
        self.device.logcat_clear()  # before the launch, deliberately
        self.launch()
        self.device.sleep(8)
        foreground = self.foreground_package()
        navigated = self.navigate(surface)
        self.device.sleep(surface.dwell_seconds)
        count = count_signal(self.device.logcat_dump(), hook.probe.signal)

        notes = []
        if foreground and foreground != self.package:
            notes.append(f"{self.package} was not foreground ({foreground or 'unknown'} was)")
        if not navigated:
            notes.append("the surface's entry control was not found on screen")
        return Measurement(
            hook.hook_id,
            surface.name,
            hook.probe.signal,
            toggle_state,
            count,
            navigated=navigated,
            usable=not notes,
            note="; ".join(notes),
        )

    # -- the probe -------------------------------------------------------

    def run(
        self,
        hook: Hook,
        enabled: Measurement | None = None,
        disabled: Measurement | None = None,
    ) -> tuple[EvidenceClaim, list[Measurement]]:
        """Turn two measurements into a runtime-probe claim.

        The measurements are passed in rather than taken here, because moving the
        toggle between them is a UI action a human may need to perform or approve.
        """
        probe = hook.probe
        if probe is None:
            raise ValueError(f"{hook.hook_id} declares no probe")
        surface = SURFACES.get(probe.surface)
        if surface is None:
            raise ValueError(
                f"{hook.hook_id}: unknown probe surface {probe.surface!r}. An unrecognised "
                "surface must not silently measure nothing."
            )
        if enabled is None:
            enabled = self.measure(hook, surface, "enabled")
        taken = [enabled]
        unusable = [item for item in taken if not item.usable]
        if disabled is not None and not disabled.usable:
            unusable.append(disabled)
        if unusable:
            # A zero the phone never had a chance to produce is not a measurement.
            raise ProbeNotTaken(
                f"{hook.hook_id}: "
                + "; ".join(f"{item.toggle_state}: {item.note}" for item in unusable)
            )
        disabled_count = 0
        if probe.requires_two_directional_delta:
            if disabled is None:
                raise ValueError(
                    f"{hook.hook_id}: this probe requires a two-directional delta, so a "
                    "measurement with the toggle OFF must be supplied. Measuring only the "
                    "on-side cannot tell a working hook from a probe that sees nothing."
                )
            taken.append(disabled)
            disabled_count = disabled.count.canonical
        claim = probe_claim(
            hook.hook_id,
            probe.surface,
            probe.signal,
            enabled.count.canonical,
            disabled_count,
            probe.requires_two_directional_delta,
            self.actor,
            waiver_note=probe.note,
        )
        return claim, taken


def record(ledger: EvidenceLedger, claim: EvidenceClaim) -> EvidenceClaim:
    return ledger.record(claim)


# ---------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, default=Path("manifest/hooks.json"))
    parser.add_argument("--hook", action="append", dest="hooks", help="hook_id to probe")
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--serial")
    parser.add_argument(
        "--state",
        choices=("enabled", "disabled"),
        required=True,
        help="which toggle state the device is CURRENTLY in. Moving the toggle is a UI "
        "action this command does not perform, so it must be told the truth.",
    )
    parser.add_argument("--out", type=Path, required=True, help="write measurements here")
    args = parser.parse_args(argv)

    hooks = {hook.hook_id: hook for hook in load_manifest(args.manifest)}
    wanted = args.hooks or [
        hook_id for hook_id, hook in hooks.items() if hook.probe is not None
    ]
    runner = ProbeRunner(
        AdbDevice(args.adb, args.serial), actor=f"device:{args.serial or 'default'}"
    )

    results = []
    for hook_id in wanted:
        hook = hooks.get(hook_id)
        if hook is None or hook.probe is None:
            print(f"skip {hook_id}: no probe declared", file=sys.stderr)
            continue
        surface = SURFACES.get(hook.probe.surface)
        if surface is None:
            print(f"skip {hook_id}: unknown surface {hook.probe.surface}", file=sys.stderr)
            continue
        measurement = runner.measure(hook, surface, args.state)
        results.append(measurement.to_dict())
        if not measurement.usable:
            # Never print a count for a measurement that did not happen: a zero
            # nobody could have produced must not look like an observation.
            print(f"{hook_id:38s} {args.state:8s} NOT MEASURED — {measurement.note}")
        else:
            print(
                f"{hook_id:38s} {args.state:8s} raw={measurement.count.raw:3d} "
                f"canonical={measurement.count.canonical:3d}"
            )

    args.out.write_text(
        json.dumps({"state": args.state, "measurements": results}, indent=2) + "\n",
        encoding="utf-8",
    )
    unusable = [item for item in results if not item["usable"]]
    if unusable:
        print(
            f"\n{len(unusable)} of {len(results)} probe(s) could not be measured. "
            "Nothing was recorded for them: a zero the phone never had a chance to "
            "produce is not evidence.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

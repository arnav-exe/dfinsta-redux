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
roughly two, and the obvious fix — ignore indented lines — is not enough either.
An `IgFunctionalErrorEvent` payload has three parts and only the first is the
event: a message body, then an indented stack, then indented ``field = value``
entries. A field whose value spans lines has its continuations logged as
*un-indented* messages under the same tag, so the third contaminating form looks
exactly like a live event until you notice where in the payload it sits.

All three carry the signal text:

* ``\t NETWORK_FAILURE_REASON = Blocked by DFInsta setting`` — the same block
  restated as a field of its own event.
* ``\t aware_trace = [{… "error_message":"fault_message: Blocked by DFInsta
  setting" …}]`` — a batched history, as JSON.
* ``After 8 seconds, user vertically scrolled … error message: fault_message:
  Blocked by DFInsta setting.`` — the readable form of that same history, spilled
  across continuation lines of ``\t aware_trace_readable =``. Instagram flushes it
  at a *later* cold start, so a phase inherits hits belonging to the previous one.

The discriminator is therefore positional, rather than a heuristic or a blocklist
of field names: a live event is a line in the **message body** of its payload —
un-indented, and before the first indented line of the same log entry, where an
entry is everything sharing one timestamp, pid, tid and tag. The stack and every
field entry are indented, so the first indent ends the body and nothing after it
can be a new event.

Measured over this repo's own captures in
``work/device-runner/newkey-430-contrasts/``, with the bare signal
``Blocked by DFInsta setting`` (raw / canonical)::

    logcat_feed_ON.txt       8 / 4      logcat_explore_OFF.txt   3 / 0
    logcat_explore_ON.txt   10 / 5      logcat_stories_OFF.txt   2 / 0
    logcat_stories_ON.txt    6 / 3

Raw counts alone would have reported an off-side leak that does not exist.
:func:`count_signal` returns both numbers so the difference stays visible instead
of being quietly subtracted.

Note what the off-side zeros do *not* rest on. The manifest declares this signal
as ``java.io.IOException: Blocked by DFInsta setting``, and that prefix already
excludes all three contaminating forms at the raw level; the structural rule is
what makes the bare form safe too. It did not, until 2026-08-02: indentation
alone let the narration through and both off-sides really read 2 canonical. The
numbers above are the first this docstring has stated that reproduce against the
captures they name.

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

from .evidence import (
    EvidenceClaim,
    EvidenceKind,
    EvidenceLedger,
    Producer,
    Verdict,
    probe_claim,
)
from .hook_manifest import Hook, Probe, load_manifest

PACKAGE = "com.instagram.android"


class ProbeNotTaken(RuntimeError):
    """The measurement could not be made, so there is no result to interpret.

    Distinct from a probe that ran and saw nothing: that is a real observation
    and may be a genuine `inconclusive`. This means the phone was locked, the app
    never reached the foreground, or the surface was not on screen — and a zero
    from those conditions must not enter the ledger at all.
    """


class UiUnavailable(ProbeNotTaken):
    """`uiautomator dump` could not produce a hierarchy.

    Its own failure mode, kept separate from "the control is not on screen",
    because the two look identical downstream and mean opposite things. UI
    Automator cannot reach idle while Reels plays or a blocked feed retries, so
    this happens routinely and says nothing whatever about the app. Reporting it
    as "the surface's entry control was not found" would be an assertion about
    the screen made from a capture of nothing.
    """


#: A logcat line is `date time pid tid LEVEL TAG: message`. The message is what
#: is interesting; the rest identifies which log entry the message belongs to,
#: because one multi-line payload arrives as many lines sharing all four.
LOGCAT_LINE = re.compile(
    r"^\s*(?P<stamp>\S+\s+\S+)\s+(?P<pid>\d+)\s+(?P<tid>\d+)\s+[VDIWEFS]\s+"
    r"(?P<tag>[^:]*):\s?(?P<message>.*)$"
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

    ``canonical`` counts only lines in the *message body* of their log entry.
    A payload runs body, then stack, then fields, and the whole of it arrives as
    consecutive lines sharing one timestamp, pid, tid and tag::

        E IgFunctionalErrorEvent: FEED_NOT_LOADING
        E IgFunctionalErrorEvent: java.io.IOException: Blocked by DFInsta setting
        E IgFunctionalErrorEvent: \tat com.dfinstagram.hooks.throwIfBlocked(…)
        E IgFunctionalErrorEvent: \t NETWORK_FAILURE_REASON = Blocked by DFInsta setting

    The second line is a block that happened; the fourth is the same block
    restated. The first indented line — here the stack — ends the body, and every
    line after it belongs to the payload however it is indented. That last part
    is what catches the third form, whose continuations are *not* indented::

        E IgFunctionalErrorEvent: \t aware_trace_readable = During the current app session…
        E IgFunctionalErrorEvent: After 8 seconds, user vertically scrolled … fault_message: Blocked by DFInsta setting.

    A line that is not a logcat line at all has no payload to sit in and is
    judged on indentation alone, so this still counts sensibly over a grep-
    filtered log or a hand-made fixture.

    The known limit: two separate events from one tag, pid and tid inside the
    same millisecond merge into one entry, and the second one's body is read as a
    continuation of the first. That under-counts, which is the safe direction —
    a lost hit weakens a delta towards `inconclusive`, whereas the over-count it
    replaces turned an off-side zero into a phantom leak and read as `failed`.
    """
    pattern = re.compile(signal)
    raw = 0
    canonical = 0
    kept: list[str] = []
    #: Log entries whose body has ended, i.e. that have had an indented line.
    in_fields: set[tuple[str, str, str, str]] = set()
    for line in logcat.splitlines():
        match = LOGCAT_LINE.match(line)
        if match is None:
            entry, message = None, line
        else:
            entry = (
                match.group("stamp"),
                match.group("pid"),
                match.group("tid"),
                match.group("tag"),
            )
            message = match.group("message")
        indented = message[:1] in {" ", "\t"}
        # Read before the write: the first indented line ENDS the body, so it is
        # not itself part of one.
        body = entry is None or entry not in in_fields
        if indented and entry is not None:
            in_fields.add(entry)
        if not pattern.search(line):
            continue
        raw += 1
        # An indented message is a field or a stack frame; an un-indented one
        # after the body has ended is a continuation of a multi-line field value.
        # Neither is an event.
        if indented or not body:
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
        try:
            self.shell("uiautomator", "dump", "/sdcard/window_dump.xml")
            return self.shell("cat", "/sdcard/window_dump.xml")
        except RuntimeError as error:
            # Routine, not exceptional: UI Automator cannot reach idle while
            # Reels plays or a blocked feed retries. Because the file was removed
            # first, the `cat` then fails too — so both failures mean the same
            # thing and neither is a fact about the app.
            raise UiUnavailable(f"the UI hierarchy could not be read: {error}") from error

    def tap(self, x: int, y: int) -> None:
        self.shell("input", "tap", str(x), str(y))

    def long_press(self, x: int, y: int, duration_ms: int = 800) -> None:
        self.shell("input", "swipe", str(x), str(y), str(x), str(y), str(duration_ms))

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


# ------------------------------------------------------------------- measuring


@dataclass(frozen=True)
class Surface:
    """How to get the app to the place a probe measures.

    ``resource_id`` and ``content_desc`` are tried **in that order, separately** —
    not as a conjunction. Instagram 440 renamed the bottom-navigation ids
    (`…:id/profile_tab` is gone; the profile tab is now inside
    `profile_tab_layout`), so every resource-id selector below silently stopped
    matching and a whole walkthrough recorded "app_launch" as the only surface it
    reached. The content descriptions — "Home", "Reels", "Profile" — survived.
    Neither is dependable alone: ids are renamed at version bumps and
    descriptions are localised, which is precisely why the fallback exists.
    """

    name: str
    resource_id: str | None = None
    content_desc: str | None = None
    dwell_seconds: float = 20.0

    def selectors(self) -> tuple[tuple[str | None, str | None], ...]:
        """Each way of finding this surface's control, most specific first."""
        out: list[tuple[str | None, str | None]] = []
        if self.resource_id is not None:
            out.append((self.resource_id, None))
        if self.content_desc is not None:
            out.append((None, self.content_desc))
        return tuple(out)

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
    "feed_tab": Surface("feed_tab", resource_id=f"{PACKAGE}:id/feed_tab", content_desc="Home"),
    "reels_tab": Surface("reels_tab", resource_id=f"{PACKAGE}:id/clips_tab", content_desc="Reels"),
    "explore_tab": Surface(
        "explore_tab",
        resource_id=f"{PACKAGE}:id/search_tab",
        content_desc="Search and explore",
    ),
    "profile_options_long_press": Surface(
        "profile_options_long_press",
        resource_id=f"{PACKAGE}:id/profile_tab",
        content_desc="Profile",
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
        """Tap the surface's entry control. False when it could not be found.

        Each of the surface's selectors is tried on its own. One dump is taken
        and reused, so the fallback costs nothing on the wire and cannot see two
        different screens.
        """
        selectors = surface.selectors()
        if not selectors:
            return True  # app_launch: being started IS the surface
        xml = self.device.ui_xml()
        for resource_id, content_desc in selectors:
            node = find_node(xml, resource_id=resource_id, content_desc=content_desc)
            if node is None:
                continue
            centre = node_centre(node)
            if centre is None:
                continue
            self.device.tap(*centre)
            return True
        return False

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
        unreadable = ""
        try:
            navigated = self.navigate(surface)
        except UiUnavailable as error:
            # Not "the control is absent" — nothing could be read at all. Kept
            # apart so the note says which, and so the measurement is unusable
            # rather than a zero attributed to an empty screen.
            navigated, unreadable = False, str(error)
        self.device.sleep(surface.dwell_seconds)
        count = count_signal(self.device.logcat_dump(), hook.probe.signal)

        notes = []
        if unreadable:
            notes.append(unreadable)
        if foreground != self.package:
            # An unreadable foreground is not a passing check. It means nothing
            # could be shown to have been on screen, which is the same fact as
            # the wrong app being there: this capture is not of what was meant to
            # be measured. Guarding on `foreground and ...` made the empty case
            # a usable zero and left the 'unknown' below unreachable.
            notes.append(f"{self.package} was not foreground ({foreground or 'unknown'} was)")
        if not navigated and not unreadable:
            # Only claim the control is absent when the screen could be read.
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


class IdentityProbe(ProbeRunner):
    """`hook_executed`: did THIS hook's injection site run?

    The probe every other kind approximates. A feature-level signal answers "is
    the feature blocked", which is not the same question and cannot attribute:
    three Reels endpoint hooks share one failure log, and two settings variants
    share one dialog, so a shared observation credits hooks that may be entirely
    inert. Both cases are real here, and the second hid a hook that is probably
    dead.

    Each payload calls a method named after its hook, which logs once per process
    on first execution. So the answer is a line naming the hook, and it does not
    depend on toggle state, on navigating to the right surface, or on the feature
    producing an observable effect at all.
    """

    #: What `com.dfinstagram.probe` logs. One line per hook per process.
    TAG = "DFInstaProbe"

    def executed(self, log: str) -> set[str]:
        """Hook ids that reported execution in this capture."""
        found: set[str] = set()
        for line in log.splitlines():
            match = LOGCAT_LINE.match(line)
            if match is None or match.group("tag").strip() != self.TAG:
                continue
            message = match.group("message").strip()
            if message:
                found.add(message)
        return found

    def measure_identities(self, dwell_seconds: float = 25.0) -> tuple[set[str], bool, str]:
        """Start clean, exercise the app, and collect which hooks announced themselves."""
        usable, why = self.screen_is_usable()
        if not usable:
            return set(), False, why
        self.stop()
        self.device.logcat_clear()
        self.launch()
        self.device.sleep(dwell_seconds)
        foreground = self.foreground_package()
        log = self.device.logcat_dump()
        if foreground != self.package:
            return set(), False, f"{self.package} did not reach the foreground"
        return self.executed(log), True, ""

    def claims(
        self, hooks: Sequence[Hook], executed: set[str], visited: Sequence[str] = ()
    ) -> list[EvidenceClaim]:
        """One claim per hook: it ran, or it did not.

        A hook that did not report is NOT automatically failed. Its site may
        legitimately not have been reached by whatever the run exercised — an
        endpoint nobody requested is not a broken patch. So an absent hook is
        `inconclusive` and names the surfaces that were visited, which is the
        information needed to decide whether the run should have reached it.
        Recording it as `failed` would make an incomplete walkthrough look like a
        defect; recording it as `passed` would be the failure this exists to stop.
        """
        out: list[EvidenceClaim] = []
        for hook in hooks:
            if hook.status != "active":
                continue
            ran = hook.hook_id in executed
            out.append(
                EvidenceClaim(
                    hook_id=hook.hook_id,
                    kind=EvidenceKind.RUNTIME_PROBE,
                    verdict=Verdict.PASSED if ran else Verdict.INCONCLUSIVE,
                    producer=Producer.DEVICE,
                    actor=self.actor,
                    summary=(
                        f"{hook.hook_id} announced its own execution"
                        if ran
                        else (
                            f"{hook.hook_id} never announced execution. Its site may not "
                            "have been reached by this walkthrough, or the patch may be "
                            "inert; these are different and this run cannot tell them apart."
                        )
                    ),
                    detail={
                        "executed": ran,
                        "surfaces_visited": list(visited),
                        "hooks_that_ran": sorted(executed),
                    },
                )
            )
        return out


def shared_signals(hooks: Iterable[Hook]) -> dict[tuple[str, str, str], list[str]]:
    """Probes that more than one hook would satisfy with the same observation.

    Both settings hooks declare the signal ``Distraction-free settings`` on the
    same surface, and Instagram picks between their two action-bar
    implementations at runtime — so exactly one of them is live on any given
    device, the dialog opening proves that *one* works, and nothing in the
    observation says which. Recording `passed` for both would credit a hook that
    may be completely inert, which is the precise failure the 430 settings hook
    already produced once.
    """
    groups: dict[tuple[str, str, str], list[str]] = {}
    for hook in hooks:
        if hook.probe is None or hook.status != "active":
            continue
        key = (hook.probe.kind, hook.probe.signal, hook.probe.surface)
        groups.setdefault(key, []).append(hook.hook_id)
    return {key: ids for key, ids in groups.items() if len(ids) > 1}


def attribute(claim: EvidenceClaim, group: Sequence[str]) -> EvidenceClaim:
    """Downgrade a claim that cannot be attributed to the hook it is filed under.

    The observation is real, so it is not discarded — but it becomes
    ``inconclusive`` for each member, because "one of these two works" is not
    evidence that this one does.
    """
    if len(group) < 2:
        return claim
    others = [name for name in group if name != claim.hook_id]
    return EvidenceClaim(
        hook_id=claim.hook_id,
        kind=claim.kind,
        verdict=Verdict.INCONCLUSIVE if claim.verdict is Verdict.PASSED else claim.verdict,
        producer=claim.producer,
        actor=claim.actor,
        summary=(
            f"{claim.summary} — but {', '.join(others)} declare the same signal on the "
            "same surface and only one implementation is live at a time, so this "
            "observation cannot say which hook produced it"
        ),
        detail={**dict(claim.detail), "attribution": "shared", "shared_with": others},
    )


# --------------------------------------------------- probes that are not deltas


@dataclass(frozen=True)
class AbsenceResult:
    """An assertion that something is NOT there, plus proof the search worked."""

    hits: int
    control_found: bool
    control: str
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def proven(self) -> bool:
        # Zero hits means nothing only when the same search demonstrably CAN
        # find something. An empty capture reports zero for every query.
        return self.control_found and self.hits == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "hits": self.hits,
            "control": self.control,
            "control_found": self.control_found,
            "proven": self.proven,
            **dict(self.detail),
        }


class StartupProbe(ProbeRunner):
    """`startup_no_fatal`: the hook runs at process start without throwing.

    Not toggleable, so there is no delta to measure — executing at all IS the
    proof, because an unresolved reference would throw before any UI appears.
    That makes it an *absence* assertion, and an absence assertion with no
    positive control is worthless: a scan of an empty capture reports zero hits
    for every query and reads as a pass. So the same capture must be shown to
    contain a marker that is certainly there.
    """

    #: Present in any logcat that actually covers this app starting.
    CONTROL = r"Start proc \d+:com\.instagram\.android"

    def measure_startup(self, hook: Hook) -> tuple[AbsenceResult, bool, str]:
        # Behind a keyguard the app cannot reach the foreground, and this probe
        # reads that as the app having failed to start — recording a measurement
        # that could not be taken as a defect in the hook. Refuse instead, the
        # way `IdentityProbe.measure_identities` and `ProbeRunner.measure` do.
        usable, why = self.screen_is_usable()
        if not usable:
            raise ProbeNotTaken(f"{hook.hook_id}: not measured: {why}")
        self.stop()
        self.device.logcat_clear()
        self.launch()
        self.device.sleep(18)
        foreground = self.foreground_package()
        alive = bool(self.device.shell("pidof", self.package).strip())
        log = self.device.logcat_dump()
        assert hook.probe is not None
        hits = count_signal(log, hook.probe.signal)
        control = re.search(self.CONTROL, log) is not None
        return (
            AbsenceResult(
                hits.canonical,
                control,
                self.CONTROL,
                {"raw_hits": hits.raw, "process_alive": alive, "foreground": foreground},
            ),
            alive and foreground == self.package,
            foreground,
        )

    def claim(self, hook: Hook) -> tuple[EvidenceClaim, AbsenceResult]:
        assert hook.probe is not None
        result, running, foreground = self.measure_startup(hook)
        if not result.control_found:
            verdict, summary = (
                Verdict.INCONCLUSIVE,
                "the logcat capture does not contain this app starting, so finding no "
                "fatal error proves nothing about it",
            )
        elif not running:
            verdict, summary = (
                Verdict.FAILED,
                f"the app did not reach the foreground ({foreground or 'nothing'} did)",
            )
        elif result.hits:
            verdict, summary = (
                Verdict.FAILED,
                f"{result.hits} startup error(s) matching {hook.probe.signal!r}",
            )
        else:
            verdict, summary = (
                Verdict.PASSED,
                "started and stayed foreground with no linkage error, in a capture that "
                "demonstrably covers this app starting",
            )
        return (
            EvidenceClaim(
                hook_id=hook.hook_id,
                kind=EvidenceKind.RUNTIME_PROBE,
                verdict=verdict,
                producer=Producer.DEVICE,
                actor=self.actor,
                summary=summary,
                detail=result.to_dict(),
            ),
            result,
        )


class DialogProbe(ProbeRunner):
    """`ui_dialog`: the settings dialog opens on a long-press.

    Not toggleable either — the dialog opens or it does not — and this is the
    hook that was runtime-inert on 430 while passing every static assertion, so
    the dialog actually appearing is the whole proof.
    """

    def measure_dialog(
        self, hook: Hook, profile_selector: str, options_desc: str = "Options"
    ) -> tuple[bool, AbsenceResult]:
        assert hook.probe is not None
        # Same omission as `StartupProbe`, and cheap to keep consistent: a locked
        # phone degrades to `inconclusive` here rather than `failed`, but "the
        # dump could not reach the control" and "the probe was never taken" are
        # different facts and only one of them is about the hook.
        usable, why = self.screen_is_usable()
        if not usable:
            raise ProbeNotTaken(f"{hook.hook_id}: not measured: {why}")
        self.stop()
        self.launch()
        self.device.sleep(12)
        profile = find_node(self.device.ui_xml(), resource_id=profile_selector)
        if profile is None:
            return False, AbsenceResult(0, False, "profile tab", {"reason": "no profile tab"})
        centre = node_centre(profile)
        assert centre is not None
        self.device.tap(*centre)
        self.device.sleep(6)

        xml = self.device.ui_xml()
        options = find_node(xml, content_desc=options_desc)
        if options is None:
            return False, AbsenceResult(
                0, False, options_desc, {"reason": "Options control not on screen"}
            )
        long_clickable = 'long-clickable="true"' in options
        centre = node_centre(options)
        assert centre is not None
        self.device.long_press(*centre, duration_ms=900)
        self.device.sleep(4)

        after = self.device.ui_xml()
        found = hook.probe.signal in after
        # The control: the dump has to actually be this app's UI, or "the title
        # is not there" is a statement about a failed dump.
        control = self.package in after
        return found, AbsenceResult(
            1 if found else 0,
            control,
            f"{self.package} in the UI dump",
            {"options_long_clickable": long_clickable},
        )

    def claim(self, hook: Hook, profile_selector: str) -> tuple[EvidenceClaim, AbsenceResult]:
        assert hook.probe is not None
        found, result = self.measure_dialog(hook, profile_selector)
        if not result.control_found:
            verdict = Verdict.INCONCLUSIVE
            summary = f"could not reach the control: {result.detail.get('reason', 'unknown')}"
        elif found:
            verdict = Verdict.PASSED
            summary = f"long-pressing Options opened a dialog containing {hook.probe.signal!r}"
        else:
            verdict = Verdict.FAILED
            summary = (
                f"long-pressing Options did not show {hook.probe.signal!r}; the patch is "
                "present but inert, which is exactly the 430 failure"
            )
        return (
            EvidenceClaim(
                hook_id=hook.hook_id,
                kind=EvidenceKind.RUNTIME_PROBE,
                verdict=verdict,
                producer=Producer.DEVICE,
                actor=self.actor,
                summary=summary,
                detail=result.to_dict(),
            ),
            result,
        )


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

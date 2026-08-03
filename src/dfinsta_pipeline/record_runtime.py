"""Stage 9's missing half: turn device measurements into ledger claims.

`probes.main` writes *measurements* — what was done and what was seen. The
evidence ledger wants *claims* — a verdict, a producer, an actor, a summary a
human can read at a gate. Nothing in this repo joined the two, so both times a
version's runtime evidence was recorded (439, then 440) the bridge was a script
written for the occasion and thrown away. That is a stage that cannot be re-run,
which means a number nobody can reproduce.

Three modes, matching the three probe shapes `probes.py` produces:

  identity   one launch, optionally visiting named surfaces, collecting the
             `DFInstaProbe: <hook_id>` lines each payload emits. One claim per
             active hook: it ran, or nothing in this capture says it did.
  startup    the absence probe for `set_app_context` — it executes at process
             start or the app does not start at all.
  delta      one hook, one toggle state. The claim cannot be made until BOTH
             states exist, because moving the toggle is a UI action a human
             performs between two invocations, so the measurements accumulate in
             a store and the pairing happens when the second one lands.

**Why the delta store is a file rather than a flag.** The two halves of a
two-directional probe are separated by a human walking to a settings dialog. A
run that took only the on-side and then guessed the off-side would be recording a
number nobody measured, which is the failure `probe_claim` already refuses at the
schema level (`ProbeNotTaken`, and "no delta in either direction is not a pass").
Persisting the halves keeps the honest sequence — measure, act, measure — and
keeps a half-finished probe visibly half-finished.

**Shared signals are attributed here, not left to the reader.** Two settings
hooks declare the same dialog and three Reels hooks share one failure log, so an
observation that fits more than one hook is downgraded to `inconclusive` for each
member by `probes.attribute`. Skipping that step is how a completely inert hook
inherits its neighbour's pass.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .evidence import EvidenceClaim
from .hook_manifest import Hook, load_manifest
from .probes import (
    SURFACES,
    AdbDevice,
    Device,
    IdentityProbe,
    Measurement,
    ProbeNotTaken,
    ProbeRunner,
    StartupProbe,
    UiUnavailable,
    attribute,
    shared_signals,
)

__all__ = [
    "RecordError",
    "append",
    "identity_claims",
    "startup_claim",
    "delta_measurement",
    "delta_claim",
    "main",
]


class RecordError(RuntimeError):
    """Raised when a recording cannot be made from what is on hand."""


def append(path: Path, claims: Sequence[EvidenceClaim]) -> None:
    """Append claims to an evidence JSONL. Never rewrites, never deduplicates.

    The ledger's superseding rule is "a later claim wins", so re-measuring is an
    append and the history of what was seen stays on disk.
    """
    with open(path, "a", encoding="utf-8") as handle:
        for claim in claims:
            handle.write(json.dumps(claim.to_dict(), sort_keys=True) + "\n")


# ------------------------------------------------------------------- identity


def identity_claims(
    device: Device,
    hooks: Sequence[Hook],
    actor: str,
    visit: Sequence[str] = (),
    dwell_seconds: float = 25.0,
) -> list[EvidenceClaim]:
    """Launch, walk the named surfaces, and ask each hook whether it ran.

    ``visit`` names entries of `probes.SURFACES`. Visiting matters more than it
    looks: `com.dfinstagram.probe` logs once per hook per *process*, so a hook
    whose site lives on the profile screen cannot announce itself in a capture
    that never left the feed — and its silence would be recorded as
    `inconclusive`, which is true but uninformative. Walking first turns as many
    of those as possible into a real answer.
    """
    probe = IdentityProbe(device, actor=actor)
    unknown = [name for name in visit if name not in SURFACES]
    if unknown:
        raise RecordError(
            f"unknown surface(s) {unknown}; an unrecognised surface must not silently "
            f"visit nothing. Known: {sorted(SURFACES)}"
        )
    usable, why = probe.screen_is_usable()
    if not usable:
        raise RecordError(f"not measured: {why}")
    probe.stop()
    device.logcat_clear()
    probe.launch()
    device.sleep(dwell_seconds)
    visited: list[str] = ["app_launch"]
    for name in visit:
        try:
            reached = probe.navigate(SURFACES[name])
        except UiUnavailable:
            # UI Automator routinely cannot idle while Reels plays or a blocked
            # feed retries. A surface that could not be reached is simply absent
            # from `visited`, which is exactly what the claim's "its site may not
            # have been reached by this walkthrough" is about — so this degrades
            # the answer rather than losing the whole capture.
            reached = False
        if reached:
            visited.append(name)
        device.sleep(SURFACES[name].dwell_seconds)
    foreground = probe.foreground_package()
    log = device.logcat_dump()
    if foreground != probe.package:
        # Same rule as everywhere else here: a capture that cannot be shown to be
        # of this app on screen is not a measurement of this app.
        raise RecordError(
            f"not measured: {probe.package} was not foreground ({foreground or 'unknown'} was)"
        )
    return probe.claims(hooks, probe.executed(log), visited=visited)


# -------------------------------------------------------------------- startup


def startup_claim(device: Device, hooks: Sequence[Hook], actor: str) -> EvidenceClaim:
    by_id = {hook.hook_id: hook for hook in hooks}
    hook = by_id.get("set_app_context")
    if hook is None:
        raise RecordError("the manifest declares no set_app_context hook to probe")
    claim, _ = StartupProbe(device, actor=actor).claim(hook)
    return claim


# ---------------------------------------------------------------------- delta


def delta_measurement(
    device: Device, hook: Hook, state: str, actor: str
) -> Measurement:
    if hook.probe is None:
        raise RecordError(f"{hook.hook_id} declares no probe")
    surface = SURFACES.get(hook.probe.surface)
    if surface is None:
        raise RecordError(f"{hook.hook_id}: unknown probe surface {hook.probe.surface!r}")
    return ProbeRunner(device, actor=actor).measure(hook, surface, state)


def _revive(data: dict) -> Measurement:
    """Rebuild a Measurement from `to_dict`, so a stored half is a real half.

    `contaminated` is derived rather than stored back: it is a property of
    `SignalCount`, and reconstructing it from the file would let a hand-edited
    number disagree with raw minus canonical.
    """
    from .probes import SignalCount

    return Measurement(
        data["hook_id"],
        data["surface"],
        data["signal"],
        data["toggle_state"],
        SignalCount(int(data["raw"]), int(data["canonical"])),
        navigated=bool(data["navigated"]),
        usable=bool(data["usable"]),
        note=data.get("note", ""),
    )


def delta_claim(
    hooks: Sequence[Hook], hook: Hook, store: dict, actor: str, device: Device
) -> EvidenceClaim | None:
    """Pair the two toggle states into one claim, or None if one is still missing.

    None means "not yet", which is different from a failure and different from a
    pass. The caller reports it as an unfinished probe rather than writing
    anything.
    """
    halves = store.get(hook.hook_id, {})
    if "enabled" not in halves or "disabled" not in halves:
        return None
    claim, _ = ProbeRunner(device, actor=actor).run(
        hook, _revive(halves["enabled"]), _revive(halves["disabled"])
    )
    groups = shared_signals(hooks)
    assert hook.probe is not None
    key = (hook.probe.kind, hook.probe.signal, hook.probe.surface)
    if key in groups:
        claim = attribute(claim, groups[key])
    return claim


# ------------------------------------------------------------------------ cli


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("identity", "startup", "delta"))
    parser.add_argument("--manifest", type=Path, default=Path("manifest/hooks.json"))
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--serial")
    parser.add_argument("--out", type=Path, required=True, help="append claims to this JSONL")
    parser.add_argument(
        "--visit",
        action="append",
        default=[],
        help=f"identity mode: surface to walk to, one of {sorted(SURFACES)}",
    )
    parser.add_argument("--dwell", type=float, default=25.0)
    parser.add_argument("--hook", help="delta mode: which hook")
    parser.add_argument("--state", choices=("enabled", "disabled"), help="delta mode")
    parser.add_argument(
        "--measurements",
        type=Path,
        help="delta mode: where the two toggle states accumulate until both exist",
    )
    args = parser.parse_args(argv)

    hooks = list(load_manifest(args.manifest))
    device = AdbDevice(args.adb, args.serial)
    actor = f"device:{args.serial or 'default'}"

    try:
        if args.mode == "identity":
            claims = identity_claims(device, hooks, actor, args.visit, args.dwell)
            append(args.out, claims)
            for claim in claims:
                print(f"{claim.hook_id:38s} {claim.verdict.value:13s} {claim.summary[:80]}")
            return 0

        if args.mode == "startup":
            claim = startup_claim(device, hooks, actor)
            append(args.out, [claim])
            print(f"{claim.hook_id:38s} {claim.verdict.value:13s} {claim.summary[:80]}")
            return 0

        if args.hook is None or args.state is None or args.measurements is None:
            parser.error("delta mode needs --hook, --state and --measurements")
        by_id = {hook.hook_id: hook for hook in hooks}
        hook = by_id.get(args.hook)
        if hook is None:
            raise RecordError(f"no hook named {args.hook!r} in {args.manifest}")

        measurement = delta_measurement(device, hook, args.state, actor)
        print(json.dumps(measurement.to_dict(), indent=1))
        if not measurement.usable:
            # Storing an unusable half would let it be paired later as though it
            # were a measurement. A zero the phone never had a chance to produce
            # is not evidence, and it must not become evidence by being kept.
            raise RecordError(f"{hook.hook_id}: {measurement.note}")

        store: dict = {}
        if args.measurements.exists():
            store = json.loads(args.measurements.read_text(encoding="utf-8"))
        store.setdefault(hook.hook_id, {})[args.state] = measurement.to_dict()
        args.measurements.write_text(json.dumps(store, indent=1, sort_keys=True) + "\n", encoding="utf-8")

        claim = delta_claim(hooks, hook, store, actor, device)
        if claim is None:
            have = sorted(store[hook.hook_id])
            print(f"\nhave {have}; move the toggle and measure the other state before a claim")
            return 0
        append(args.out, [claim])
        print(f"{claim.hook_id:38s} {claim.verdict.value:13s} {claim.summary[:80]}")
        return 0
    except (RecordError, ProbeNotTaken, OSError) as error:
        # OSError included so a bad --out (a directory that does not exist) fails
        # the same way every other refusal does — a message and exit 1 — rather
        # than a traceback. It writes nothing either way; the difference is
        # whether the operator can tell what to fix.
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

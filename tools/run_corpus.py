"""Walk a whole exploration corpus: six toggle states, forward and back-to-front.

    tools/run_corpus.py three-round-v2 440c work/observations-v3 [forward|reverse|both]

One session per toggle state — the baseline with everything off, then each of the
five toggles alone — and then the same six again in the opposite order.

**The reverse pass is the control, and it is the reason this exists rather than a
`for` loop in someone's shell history.** Leftover cache, a warming CDN, or any
drift accumulating over an afternoon would be perfectly confounded with arm order
in a single forward pass: whatever ran last would look different from whatever ran
first, and the difference would be attributed to the toggle. Running the six again
back-to-front breaks that alignment, so a real effect appears in both orders and an
artefact of position appears in one. `grouping` requires both — a state walked once
is unreadable by name.

**It skips a session whose capture already exists**, so a run interrupted at
session nine resumes rather than restarting. That matters: twelve three-round
sessions take about seventy minutes of device time.

**It stops at the first refusal rather than carrying on.** `device_session.py`
refuses when it cannot reach the toggle state it was asked for or cannot find the
bottom nav, and the sessions after that one would be walked against a phone in an
unknown state. A corpus with one silently wrong session is worse than a corpus with
nine sessions, because nothing downstream can tell which one it was.

**With one exception, and it is not a weakening of that rule.** If the session
failed and the *phone is no longer reachable*, the cause is the cable rather than
the walk: nothing touched the app, and an interrupted session leaves no capture,
so redoing it redoes exactly what was lost. That happened three times across two
walks on 2026-08-18/19 and the repair by hand was this every time. It waits a
bounded three minutes, retries **the same session once**, and refuses normally if
that fails too — moving on to the next arm is the thing the rule above exists to
prevent, and an unbounded wait would turn a phone unplugged overnight into a
corpus walked across two days.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parent.parent

#: The baseline first, then one toggle at a time. `none` is spelled out rather
#: than implied by an empty string, because `device_session.py` takes it as a
#: positional argument and an empty one is indistinguishable from a missing one.
ARMS = ("none", "disable_feed", "disable_explore", "disable_reels",
        "disable_stories", "disable_adds")


def short(arm: str) -> str:
    """`disable_feed` -> `feed`. The session id carries the state, not the key."""

    return "none" if arm == "none" else arm.replace("disable_", "")


#: How long to wait for a phone that vanished mid-walk, and how often to look.
#: Thirty seconds is what the two real dropouts took to re-enumerate; three
#: minutes is generous enough for a cable being reseated and short enough that an
#: unattended run fails the same evening.
DEVICE_WAIT_SECONDS = 180
DEVICE_POLL_SECONDS = 10


def adb() -> list[str]:
    """The same binary and serial `device_session` uses, read from it.

    Imported rather than repeated: a second copy of the serial is a second thing
    that can name the wrong phone.
    """

    sys.path.insert(0, str(REPOSITORY / "tools"))
    from device_session import ADB  # noqa: PLC0415

    return list(ADB)


def present() -> bool:
    """Is the phone reachable right now? False for every reason it might not be."""

    try:
        result = subprocess.run(
            adb() + ["get-state"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.stdout.strip() == "device"


def wait_for_device() -> bool:
    """Poll until the phone is back, or give up. True if it came back.

    `adb wait-for-device` is not used: it blocks with no bound and no output, so
    a phone left unplugged hangs the walk silently until someone notices.
    """

    print(f"  the phone is not reachable; waiting up to {DEVICE_WAIT_SECONDS}s",
          flush=True)
    for _ in range(DEVICE_WAIT_SECONDS // DEVICE_POLL_SECONDS):
        time.sleep(DEVICE_POLL_SECONDS)
        if present():
            return True
    print("  it did not come back", file=sys.stderr, flush=True)
    return False


def session(arm: str, out: Path, walk: str) -> subprocess.CompletedProcess:
    """One session, as its own process. The unit that is retried."""

    return subprocess.run(
        [sys.executable, str(REPOSITORY / "tools" / "device_session.py"),
         arm, str(out), walk],
        capture_output=True, text=True, timeout=1800,
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not 3 <= len(argv) <= 4:
        print(__doc__.splitlines()[2].strip(), file=sys.stderr)
        return 2
    walk, prefix, directory = argv[0], argv[1], Path(argv[2])
    which = argv[3] if len(argv) > 3 else "both"
    if which not in ("forward", "reverse", "both"):
        print(f"refusing: {which!r} is not forward, reverse or both", file=sys.stderr)
        return 2
    directory.mkdir(parents=True, exist_ok=True)

    passes: list[tuple[str, tuple[str, ...]]] = []
    if which in ("forward", "both"):
        passes.append(("isolate", ARMS))
    if which in ("reverse", "both"):
        passes.append(("reverse", tuple(reversed(ARMS))))

    started = time.time()
    for label, order in passes:
        for arm in order:
            out = directory / f"{prefix}-{label}-{short(arm)}.log"
            if out.exists():
                print(f"  skip {out.name} (already walked)", flush=True)
                continue
            result = session(arm, out, walk)
            elapsed = int(time.time() - started)
            print(f"[{elapsed}s] {result.stdout.strip() or result.stderr.strip()}",
                  flush=True)
            if result.returncode != 0 and not present():
                # **The phone went away, rather than the walk going wrong.** This
                # is not the unknown state the refusal below is about: nothing
                # touched the app, and the session that was interrupted left no
                # capture, so redoing it redoes exactly what was lost. Happened
                # three times across two walks on 2026-08-18/19 — a cable, not a
                # bug — and each time the repair by hand was this and only this.
                #
                # Bounded, and it retries the SAME session once. A loop that kept
                # going would turn a phone unplugged for the night into a corpus
                # walked over two days, and a retry that moved on to the next arm
                # would be the thing the refusal exists to prevent.
                if wait_for_device():
                    print(f"  {arm}: the phone came back; walking it again", flush=True)
                    result = session(arm, out, walk)
                    print(f"[{int(time.time() - started)}s] "
                          f"{result.stdout.strip() or result.stderr.strip()}", flush=True)
            if result.returncode != 0:
                print(
                    f"refusing to walk the rest: {arm} stopped and every session after "
                    "it would run against a phone in an unknown state",
                    file=sys.stderr, flush=True,
                )
                return result.returncode
    print(f"done in {int(time.time() - started)}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

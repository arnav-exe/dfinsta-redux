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
            result = subprocess.run(
                [sys.executable, str(REPOSITORY / "tools" / "device_session.py"),
                 arm, str(out), walk],
                capture_output=True, text=True, timeout=1800,
            )
            elapsed = int(time.time() - started)
            print(f"[{elapsed}s] {result.stdout.strip() or result.stderr.strip()}",
                  flush=True)
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

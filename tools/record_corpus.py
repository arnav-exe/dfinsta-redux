"""Turn a directory of captures into committed rows, each with a verified redaction.

    tools/record_corpus.py --version 440 --build-sha256 <64 hex> \
        --walk three-round-v2 --captures work/observations-v3 --glob '440c-*.log'

Two things this deliberately does **not** take, and the omissions are the point.

**No `--toggles`.** Which blocks were active comes out of the capture, where the
build stated it on every checked request. An operator-supplied state would be a
formality rather than a safety property — the same shape of mistake as deriving a
retirement's effective version from a flag the same person typed.

**No `--refusals`.** Same argument, one step further: the guard writes down which
literal it refused at the moment it throws. A capture from a build that could not
say so records `None`, which is a silence and not a zero, and no flag here can turn
one into the other.

**What it does take is `--walk`**, because nothing else can. The walk is a property
of the driving script — three rounds or one pass — and neither the phone nor the
capture knows it. That is an argument for this one flag rather than a hole in the
rule.

**`--recorded-at` comes from the capture's own mtime**, not from the clock now. A
corpus recorded hours after it was walked would otherwise sort after work that
happened in between.

**Every capture is redacted and the redaction is verified** before the next one is
recorded. `redact_capture.py --verify` refuses unless `observation.parse` gives the
identical answer for the reduction and the original, so the committed evidence can
be re-derived from a clone rather than taken on trust.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parent.parent


def watch_list() -> tuple[str, ...]:
    """Every literal the observing build watches, read from the manifest.

    Read rather than supplied, and from the same declaration `guards` renders the
    build from — so a session cannot be recorded as watching one set of paths while
    the build that produced it watched another.
    """

    sys.path.insert(0, str(REPOSITORY / "src"))
    from dfinsta_pipeline.guards import (  # noqa: PLC0415
        rules_from_manifest,
        watch_from_manifest,
        watched_literals,
    )

    manifest = REPOSITORY / "manifest" / "hooks.json"
    return watched_literals(rules_from_manifest(manifest), watch_from_manifest(manifest))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", required=True)
    parser.add_argument("--build-sha256", required=True,
                        help="the APK that was installed, not the one that was built")
    parser.add_argument("--walk", required=True,
                        help="the driving protocol, e.g. three-round-v2. Nothing on the "
                             "phone knows this, so it is yours to state")
    parser.add_argument("--captures", type=Path, required=True)
    parser.add_argument("--glob", default="*.log")
    parser.add_argument("--surface", default="feed_explore_reels")
    args = parser.parse_args(argv)

    captures = sorted(Path(args.captures).glob(args.glob))
    if not captures:
        print(f"refusing: {args.captures}/{args.glob} matched nothing", file=sys.stderr)
        return 2

    watched = watch_list()
    listing = REPOSITORY / "work" / "watched.txt"
    listing.parent.mkdir(parents=True, exist_ok=True)
    listing.write_text("\n".join(watched) + "\n", encoding="utf-8")

    for capture in captures:
        stamp = datetime.fromtimestamp(capture.stat().st_mtime, timezone.utc)
        recorded = subprocess.run(
            [sys.executable, "-m", "dfinsta_pipeline.observation", "record",
             "--version", args.version, "--build-sha256", args.build_sha256,
             "--recorded-at", stamp.isoformat().replace("+00:00", "Z"),
             "--session-id", capture.stem, "--surface", args.surface,
             "--walk", args.walk, "--watched-from", str(listing),
             "--capture", str(capture)],
            capture_output=True, text=True, cwd=REPOSITORY,
            env={"PYTHONPATH": str(REPOSITORY / "src"), "PATH": "/usr/bin:/bin"},
        )
        print(recorded.stdout.strip() or recorded.stderr.strip(), flush=True)
        if recorded.returncode != 0:
            return recorded.returncode
        reduced = subprocess.run(
            [sys.executable, str(REPOSITORY / "tools" / "redact_capture.py"),
             str(capture), "--out",
             str(REPOSITORY / "manifest" / "captures" / capture.name), "--verify"],
            capture_output=True, text=True, cwd=REPOSITORY,
        )
        print("   " + (reduced.stdout.strip() or reduced.stderr.strip()), flush=True)
        if reduced.returncode != 0:
            return reduced.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

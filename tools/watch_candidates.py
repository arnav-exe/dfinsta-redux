"""Put every unruled candidate on the observing build's watch list.

    tools/watch_candidates.py --index work/441-port/index [--apply]

A candidate can only carry device evidence if it was on the watch list *before*
the phone was walked, and the gate now refuses to `block` or `offer_toggle` one no
device has looked for. So this closes the loop: stage 4a finds the candidates,
this puts them where the next observing build will watch them, and the walk after
that makes them rulable.

**Watching is safe, which is why this is automatic.** An observed literal is
logged and nothing else — the request is made and the response is delivered. It is
strictly safer than what already ships: the *blocking* path is the one that hands
Meta a string, because Instagram catches our IOException and files it into its own
error event. Nothing here can change what the app receives.

**Prints by default and writes only with `--apply`**, because `manifest/hooks.json`
is the file every build is rendered from, and a tool that edits it as a side
effect of being run is one nobody can use to look.

**It never removes anything.** A literal that stops being a candidate — because a
human ruled on it, or because Instagram moved it — keeps being watched, and that
is the point: "we block this and the app has never once asked for it" is evidence
a recorded decision should be revisited, and it cannot be produced by watching only
the undecided.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY / "src"))

from dfinsta_pipeline.assessment import assess  # noqa: E402
from dfinsta_pipeline.guards import WATCH_KEY  # noqa: E402
from dfinsta_pipeline.hook_index import HookIndex  # noqa: E402
from dfinsta_pipeline.hook_manifest import load_manifest  # noqa: E402

HOOK_ID = "tigon_url_block"


def candidates(index_dir: Path, manifest_path: Path) -> tuple[str, ...]:
    """Every unblocked endpoint the app groups with ones we block.

    The same call stage 4a makes, and deliberately the same one: a watch list
    built from a different derivation could watch a set the assessment never
    asks about, and every candidate outside it would reach the gate reading
    "nobody looked" for a reason nobody could see.
    """

    index = HookIndex.load(index_dir)
    hooks = load_manifest(manifest_path)
    return tuple(item.literal for item in assess(index, hooks)[0])


def missing(literals: tuple[str, ...], manifest_path: Path) -> tuple[str, ...]:
    """Those the guard is not already watching, in the manifest's own spelling.

    Compared against `observe_watch` **and** every `url_block_rules` literal,
    because `guards.watched_literals` unions the two and a blocked literal is
    already watched. Adding it again would watch one path twice and double its
    count.
    """

    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    hook = next(
        (item for item in data.get("hooks", ()) if item.get("hook_id") == HOOK_ID), None
    )
    if hook is None:
        raise SystemExit(f"refusing: {manifest_path} declares no hook {HOOK_ID!r}")
    watched = {str(item) for item in hook.get(WATCH_KEY) or ()}
    for rule in hook.get("url_block_rules") or ():
        for literal in rule.get("literals") or ():
            watched.add(str(literal.get("text", "")))
    # Slash-insensitive, because the index writes `feed/x/` where the guard tests
    # `/feed/x/` — the same join `device_evidence` makes, for the same reason.
    seen = {item.strip("/") for item in watched}
    return tuple(item for item in literals if item.strip("/") not in seen)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--index", type=Path, required=True, help="a built index/ directory")
    parser.add_argument("--manifest", type=Path, default=REPOSITORY / "manifest" / "hooks.json")
    parser.add_argument(
        "--apply", action="store_true", help="write the manifest; otherwise only report"
    )
    args = parser.parse_args(argv)

    found = candidates(args.index, args.manifest)
    absent = missing(found, args.manifest)
    print(f"{len(found)} candidate(s); {len(absent)} not yet watched")
    for literal in absent:
        print(f"  + {literal}")
    if not absent:
        print("nothing to add: every candidate is already on the watch list")
        return 0
    if not args.apply:
        print("\nnot written. Re-run with --apply to add them to observe_watch")
        return 0

    data = json.loads(args.manifest.read_text(encoding="utf-8"))
    hook = next(item for item in data["hooks"] if item.get("hook_id") == HOOK_ID)
    hook[WATCH_KEY] = list(hook.get(WATCH_KEY) or ()) + list(absent)
    args.manifest.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    print(f"\nwrote {args.manifest}: observe_watch is now {len(hook[WATCH_KEY])} literal(s)")
    print("Build an observing APK, walk the phone, and record before the gate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

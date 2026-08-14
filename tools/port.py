"""One port, from a stock Instagram APK to a gate waiting for a human.

    tools/port.py --apk apks/instagram-442-….apk --version 442        # report
    tools/port.py --apk … --version 442 --run                          # do it

Eleven steps stand between a new Instagram release and a shipped DFInsta build.
Two of them are judgement and the other nine are typing, and until now the nine
lived only in a paragraph of `docs/DESIGN_EXPLORATION_FIRST.md` — which is where
`run_corpus.py` and `record_corpus.py` lived until the day before this was
written, and the measurement they drove was reproducible by nobody.

**It stops before the judgement, deliberately.** The last thing it does is raise
the feature gate. Ruling on the candidates is yours, and so is writing the
`url_block_rules` entry afterwards: `rulings` refuses to guess a match kind or a
preference key, and the regularity that once made the match kind look derivable
has since inverted — five of eleven literals now break it, so a generator built
on it would render five rules into silent no-ops.

===============================================================================
  RESUMABLE, BECAUSE ONE STEP TAKES AN HOUR AND A HALF
===============================================================================

Two device walks are about a hundred minutes of phone time. A runbook that
restarted from the top after a failure at step seven would be one nobody uses, so
every step declares how to tell whether it is **already done**, from artefacts on
disk rather than from a state file — a state file is a second thing that can be
wrong about the work, and the artefacts are what the next step reads anyway.

`--run` executes the steps that are not done. Without it nothing is executed and
nothing is written: the default is a report of where the port stands, because a
tool that changes the tree as a side effect of being asked a question is one you
cannot use to ask.

===============================================================================
  WHAT IT WILL NOT DO
===============================================================================

**It never handles the keystore password.** The signing step is invoked with the
`DFINSTA_*` variables inherited from the caller's environment, so the secret is
never read, printed, defaulted or written here. If they are unset it refuses and
says which one, and it never suggests a value.

**It refuses rather than continuing past a step that failed.** Each step's
output is the next step's input, so a run that carried on would be measuring a
phone in an unknown state or recording a corpus from a build that is not the one
installed. The failure is reported with the command, so it can be run by hand.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

REPOSITORY = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY / "src"))

WALKS = ("one-pass-v1", "three-round-v2")


@dataclass
class Step:
    """One command, and how to tell whether it has already happened."""

    name: str
    why: str
    #: Built lazily: a later step's command depends on an earlier step's output.
    command: Callable[["Port"], list[str]]
    done: Callable[["Port"], bool]
    #: Device steps are skipped when no phone is attached, and said so.
    needs_device: bool = False
    #: Minutes, roughly, so an operator knows what they are starting.
    minutes: int = 1
    env: Callable[["Port"], dict] = field(default=lambda port: {})


@dataclass
class Port:
    apk: Path
    version: str
    out: Path
    captures: Path
    reuse_decode: Path | None = None
    reuse_index: Path | None = None

    @property
    def index_dir(self) -> Path:
        """Where the API surface is, which is not always where this run built it.

        `--reuse-index` points at an earlier run's, and the steps that read it —
        finding candidates, and deciding whether they are all watched — do not
        care which run produced it. Returning `out/index` regardless made the
        first step report `todo` for work that was done, which is the failure a
        resumable tool exists to avoid.
        """
        return self.reuse_index or (self.out / "index")

    @property
    def unsigned(self) -> Path:
        return self.out / "dfinsta.apk"

    @property
    def signed(self) -> Path:
        return self.out / f"dfinsta_{self.version}_OBS.apk"

    @property
    def build_sha256(self) -> str:
        """The APK that was installed, read from the release report it wrote."""
        report = self.signed.with_suffix(".release.json")
        if not report.is_file():
            return ""
        return json.loads(report.read_text(encoding="utf-8"))["outputs"]["apk_sha256"]

    def sessions_for(self, walk: str) -> int:
        """How many sessions of `walk` this build already has on record."""
        from dfinsta_pipeline.observation import read  # noqa: PLC0415

        digest = self.build_sha256
        if not digest:
            return 0
        try:
            rows = read(self.version, REPOSITORY)
        except Exception:  # noqa: BLE001 - an unreadable store is "none recorded"
            return 0
        return sum(1 for row in rows if row.walk == walk and row.build_sha256 == digest)


def walked(port: Port, walk: str) -> int:
    return len(list(port.captures.glob(f"{port.version}-{walk[:2]}-*.log")))


def _driver(port: Port, *extra: str) -> list[str]:
    command = [
        sys.executable, "-m", "dfinsta_pipeline.driver", str(port.apk),
        "--out", str(port.out),
        "--framework-apk", str(REPOSITORY / "work" / "430-port" / "framework-res-api36.apk"),
    ]
    if port.reuse_decode:
        command += ["--reuse-decode", str(port.reuse_decode)]
    if port.reuse_index:
        command += ["--reuse-index", str(port.reuse_index)]
    return command + list(extra)


def _sign(port: Port) -> list[str]:
    tools = Path.home() / "Android" / "Sdk" / "build-tools" / "36.0.0"
    return [
        sys.executable, str(REPOSITORY / "tools" / "release" / "finalize.py"),
        str(port.unsigned), str(port.apk),
        "--unsigned-build-report", str(port.out / "dfinsta.build.json"),
        "--unsigned-verification-report", str(port.out / "dfinsta.verification.json"),
        "--policy", str(REPOSITORY / "release" / "signing_policy.json"),
        "--zipalign", str(tools / "zipalign"),
        "--apksigner", str(tools / "apksigner"),
        "--aapt", str(tools / "aapt"),
        "--apktool-jar", str(REPOSITORY / "apktool_2.9.3.jar"),
        "--final-verifier", str(REPOSITORY / "tools" / "verify" / "verify_build.py"),
        "--output-apk", str(port.signed),
    ]


def _adb() -> list[str]:
    return [str(Path.home() / "Android" / "Sdk" / "platform-tools" / "adb")]


STEPS: tuple[Step, ...] = (
    Step(
        "index",
        "decode the APK and index its API surface, so stage 4a can find candidates",
        lambda port: _driver(port, "--stop-after", "index"),
        lambda port: port.index_dir.is_dir(),
        minutes=12,
    ),
    Step(
        "watch",
        "put every candidate on the observing build's watch list, so the gate can "
        "rule on it later — an unwatched candidate may be ignored or deferred but "
        "never blocked",
        lambda port: [
            sys.executable, str(REPOSITORY / "tools" / "watch_candidates.py"),
            "--index", str(port.index_dir), "--apply",
        ],
        lambda port: _nothing_left_to_watch(port),
    ),
    Step(
        "observe-build",
        "build an APK that logs every watched path and every refusal it makes",
        lambda port: _driver(port, "--observe", "--stop-after", "build"),
        lambda port: port.unsigned.is_file(),
        minutes=15,
    ),
    Step(
        "sign",
        "sign with the release key, so `install -r` keeps the login",
        _sign,
        lambda port: port.signed.is_file(),
        minutes=3,
        env=lambda port: {name: os.environ[name] for name in _SIGNING if name in os.environ},
    ),
    Step(
        "install",
        "put the observing build on the phone",
        lambda port: _adb() + ["install", "-r", str(port.signed)],
        lambda port: _installed(port),
        needs_device=True,
    ),
    *[
        Step(
            f"walk-{walk}",
            f"twelve sessions under {walk}: six toggle states, forward and back-to-front",
            lambda port, walk=walk: [
                sys.executable, str(REPOSITORY / "tools" / "run_corpus.py"),
                walk, f"{port.version}-{walk[:2]}", str(port.captures), "both",
            ],
            lambda port, walk=walk: walked(port, walk) >= 12,
            needs_device=True,
            minutes=35 if walk == "one-pass-v1" else 70,
        )
        for walk in WALKS
    ],
    *[
        Step(
            f"record-{walk}",
            f"commit the {walk} rows and a verified redaction of each capture",
            lambda port, walk=walk: [
                sys.executable, str(REPOSITORY / "tools" / "record_corpus.py"),
                "--version", port.version, "--build-sha256", port.build_sha256,
                "--walk", walk, "--captures", str(port.captures),
                "--glob", f"{port.version}-{walk[:2]}-*.log",
            ],
            lambda port, walk=walk: port.sessions_for(walk) >= 12,
        )
        for walk in WALKS
    ],
)

_SIGNING = ("DFINSTA_KEYSTORE", "DFINSTA_KEY_ALIAS", "DFINSTA_KEYSTORE_PASSWORD")


def _nothing_left_to_watch(port: Port) -> bool:
    """Every candidate stage 4a finds is already on the watch list."""
    import importlib.util  # noqa: PLC0415

    spec = importlib.util.spec_from_file_location(
        "watch_candidates", REPOSITORY / "tools" / "watch_candidates.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    manifest = REPOSITORY / "manifest" / "hooks.json"
    if not port.index_dir.is_dir():
        return False
    return not module.missing(module.candidates(port.index_dir, manifest), manifest)


def _installed(port: Port) -> bool:
    """Is the phone running the version this port built?

    Version, not digest: `dumpsys` does not report one. So this answers "the right
    version is installed" and not "the exact build is", which is why the recorded
    rows carry `build_sha256` — the store, not this check, is what ties a session
    to an APK.
    """
    result = subprocess.run(
        _adb() + ["shell", "dumpsys", "package", "com.instagram.android"],
        capture_output=True, text=True, timeout=60,
    )
    return f"versionName={port.version}." in result.stdout


def device_attached() -> bool:
    try:
        result = subprocess.run(_adb() + ["devices"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.endswith("\tdevice") for line in result.stdout.splitlines())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apk", type=Path, required=True, help="the stock Instagram APK")
    parser.add_argument("--version", required=True, help="e.g. 442")
    parser.add_argument("--out", type=Path, default=None, help="default work/<version>-port")
    parser.add_argument("--captures", type=Path, default=None,
                        help="where device captures land; default work/observations-<version>")
    parser.add_argument("--reuse-decode", type=Path, default=None)
    parser.add_argument("--reuse-index", type=Path, default=None)
    parser.add_argument("--run", action="store_true",
                        help="execute the steps that are not done; without it nothing runs")
    args = parser.parse_args(argv)

    if not args.apk.is_file():
        print(f"refusing: {args.apk} is not a file", file=sys.stderr)
        return 2
    port = Port(
        apk=args.apk,
        version=args.version,
        out=args.out or REPOSITORY / "work" / f"{args.version}-port",
        captures=args.captures or REPOSITORY / "work" / f"observations-{args.version}",
        reuse_decode=args.reuse_decode,
        reuse_index=args.reuse_index,
    )
    port.captures.mkdir(parents=True, exist_ok=True)

    have_device = device_attached()
    print(f"PORT {port.version}   {port.apk.name}")
    print(f"  out      {port.out}")
    print(f"  captures {port.captures}")
    print(f"  device   {'attached' if have_device else 'NOT attached'}")
    print()

    remaining = 0
    for step in STEPS:
        done = step.done(port)
        if done:
            print(f"  [done] {step.name}")
            continue
        remaining += step.minutes
        blocked = step.needs_device and not have_device
        mark = "BLOCKED" if blocked else "todo"
        print(f"  [{mark}] {step.name:22} ~{step.minutes}m   {step.why}")
        if blocked:
            print("           attach the phone; this step and everything after it need it")
            return 1
        if not args.run:
            continue
        missing = [name for name in _SIGNING if name not in os.environ] if step.name == "sign" else []
        if missing:
            print(f"refusing: {missing[0]} is not set. This tool never reads, defaults or "
                  "prints the signing secrets; export them and re-run", file=sys.stderr)
            return 2
        command = step.command(port)
        print(f"           $ {' '.join(command)}", flush=True)
        result = subprocess.run(command, cwd=REPOSITORY, env={**os.environ, **step.env(port)})
        if result.returncode != 0:
            print(f"refusing to continue: {step.name} exited {result.returncode}. Every step "
                  "after this one reads what it produced, so carrying on would measure a "
                  "phone in an unknown state or record a corpus from a build that is not "
                  "the one installed", file=sys.stderr)
            return result.returncode
        if not step.done(port):
            print(f"refusing to continue: {step.name} exited 0 and its output is not there. "
                  "That is a step that reported success without doing the work",
                  file=sys.stderr)
            return 1
        print(f"  [done] {step.name}", flush=True)

    if remaining and not args.run:
        print(f"\n~{remaining} minutes of work outstanding. Re-run with --run.")
        return 0
    print("\nEvery mechanical step is done. What is left is yours:")
    print(f"  1. record the assessment   driver --stop-after assess --state-root … "
          f"--assessment-run-id … --actor … --owner-token …")
    print("  2. raise the gate          python -m dfinsta_pipeline.assessment_record raise …")
    print("  3. rule on the candidates  python -m dfinsta_pipeline.submission show/submit …")
    print("  4. apply the rulings       python -m dfinsta_pipeline.rulings … --apply")
    print("  5. write the url_block_rules entry by hand: the match kind and the toggle are")
    print("     not derivable, and the regularity that made the match kind look derivable")
    print("     has since inverted — five of eleven literals break it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

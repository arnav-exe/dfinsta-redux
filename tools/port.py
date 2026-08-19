"""One port, from a stock Instagram APK to a gate waiting for a human.

    tools/port.py --apk apks/instagram-442-….apk --version 442        # report
    tools/port.py --apk … --version 442 --run                          # do it

Eighteen steps stand between a new Instagram release and a patched DFInsta build
on a phone. Thirteen of them are typing this tool runs — decode, index, watch,
build an observing APK, sign it, install it, warm the app, walk the phone twice,
record both corpora, then build, sign and install the APK you would actually use.
They lived only in a paragraph of `docs/DESIGN_EXPLORATION_FIRST.md` until this
existed, which is where `run_corpus.py` and `record_corpus.py` lived until the day
before that, and the measurement they drove was reproducible by nobody.

**It stops at the judgement, deliberately.** If this version raised candidates and
nobody has ruled on them, the shippable build is **blocked** rather than built:
that build renders `url_block_rules`, so making it first would ship a decision
nobody took, silently and with every check passing. The five steps it prints there
are yours. Three of them are typing too, but they need a run id, an actor and an
owner token, and a tool that invented those would be signing a document on
somebody's behalf. The other two are judgement: ruling on the candidates, and
writing the `url_block_rules` entry afterwards — `rulings` refuses to guess a match
kind or a preference key, and the regularity that once made the match kind look
derivable has since inverted, so a generator built on it would render five rules
into silent no-ops.

**The shipped build is last on purpose.** It replaces the observing build on the
phone, and the observing build is what every recorded session was measured
against, so both corpora are on disk and committed before it runs.

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
import shutil
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
    #: Why this step cannot run yet, or "" when it can. Reported exactly like an
    #: absent phone: the run stops there rather than carrying on, because every
    #: step after it reads what this one produces.
    blocked_by: Callable[["Port"], str] = field(default=lambda port: "")
    #: Minutes, roughly, so an operator knows what they are starting.
    minutes: int = 1
    #: What a *failed* attempt at this step leaves behind. The driver and the
    #: builder both refuse to overwrite their own outputs — deliberately, and
    #: `build.py` says why — so a step that died half-way cannot simply be run
    #: again, which is the one thing a resumable runbook must allow.
    stale: Callable[["Port"], list[Path]] = field(default=lambda port: [])
    env: Callable[["Port"], dict] = field(default=lambda port: {})


@dataclass
class Port:
    apk: Path
    version: str
    out: Path
    captures: Path
    reuse_decode: Path | None = None
    reuse_index: Path | None = None
    #: Where the ledger and content store live. `None` means the gate is not
    #: configured for this run, and the two gate steps stay out of the way —
    #: exactly the behaviour every port before 2026-08-19 had.
    state_root: Path | None = None
    #: How the recorded assessment is found again. The gate client takes nothing
    #: but this, so it is the one string that has to survive between a machine
    #: that ports and a human who rules.
    assessment_run_id: str = ""
    #: Who may answer the gate, and what lets a wedged operation be reclaimed.
    #: Neither is invented here — a tool that made up an actor would be signing
    #: on somebody's behalf, which is the reason this handoff was manual until
    #: now.
    actor: str = ""
    owner_token: str = ""
    #: Where the worker is, and which queue and build it was started with.
    #: `build_id` is not optional in practice: both workflows are PINNED, a
    #: versioned worker is only dispatched its deployment's current version, and
    #: a gate raised without it is accepted by the server, never picked up, and
    #: every query times out with nothing naming the cause.
    endpoint: str = "localhost:7233"
    task_queue: str = "dfinsta-phase-a"
    build_id: str = ""
    #: How long the gate stays open. A week, and an unanswered one ends
    #: `blocked` — never an implicit approval.
    gate_timeout_seconds: int = 7 * 24 * 3600

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
    def decode(self) -> Path:
        """Where the decoded tree is — produced by `index`, read by every step after.

        `--reuse-decode` points at an earlier run's, exactly as `--reuse-index`
        does.
        """
        return self.reuse_decode or (self.out / "analysis-decode")

    @property
    def unsigned(self) -> Path:
        return self.out / "dfinsta.apk"

    @property
    def signed(self) -> Path:
        return self.out / f"dfinsta_{self.version}_OBS.apk"

    @property
    def ship_out(self) -> Path:
        """A separate run directory for the shippable build.

        Not `out`: the builder refuses to overwrite seven of its own outputs and
        the driver three more, all of which the observing build already wrote
        there. Sharing the directory would mean deleting the observing build to
        make the shipped one, and the observing build is the thing the recorded
        corpus was measured against.
        """
        return self.out.parent / f"{self.version}-ship"

    @property
    def gate_marker(self) -> Path:
        """What the raised gate left behind: the workflow id, and how to reach it.

        Written by `assessment_record raise --write-workflow-id`, because the id
        is a thing only that command knows. Local, so that asking this runbook
        where it stands never needs a Temporal server — the same rule that keeps
        the install check off the phone until there is a build to ask about.
        """
        return self.out / "gate.json"

    @property
    def gate_configured(self) -> bool:
        """Was this run given what it needs to raise the gate itself?

        All or nothing. A half-configured gate would record an assessment nobody
        can rule on, which is worse than not starting one.
        """
        return bool(
            self.state_root and self.assessment_run_id and self.actor
            and self.owner_token and self.build_id
        )

    @property
    def warm_marker(self) -> Path:
        """What the warm-up read, and the only thing it leaves behind.

        Beside the build rather than in the corpus directory: it is a fact about
        this port's phone state, and deleting it to force a re-warm must not look
        like deleting a session.
        """
        return self.out / "warm.json"

    @property
    def ship_unsigned(self) -> Path:
        return self.ship_out / "dfinsta.apk"

    @property
    def ship_signed(self) -> Path:
        return self.ship_out / f"dfinsta_{self.version}.apk"

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


def _driver(port: Port, *extra: str, out: Path | None = None) -> list[str]:
    command = [
        sys.executable, "-m", "dfinsta_pipeline.driver", str(port.apk),
        "--out", str(out or port.out),
        "--framework-apk", str(REPOSITORY / "work" / "430-port" / "framework-res-api36.apk"),
    ]
    # Every driver step after `index` reads the decode that `index` produced, and
    # must say so: the driver extracts unless told to reuse, and refuses to
    # extract over an occupied directory. Without this the second invocation of
    # the run — `observe-build` — died on the first version this tool was ever
    # run against, and the first three steps could never run in sequence.
    if port.decode.is_dir():
        command += ["--reuse-decode", str(port.decode)]
    if port.index_dir.is_dir():
        command += ["--reuse-index", str(port.index_dir)]
    return command + list(extra)


def _sign(port: Port, out: Path | None = None, output: Path | None = None) -> list[str]:
    out = out or port.out
    tools = Path.home() / "Android" / "Sdk" / "build-tools" / "36.0.0"
    return [
        sys.executable, str(REPOSITORY / "tools" / "release" / "finalize.py"),
        str(out / "dfinsta.apk"), str(port.apk),
        "--unsigned-build-report", str(out / "dfinsta.build.json"),
        "--unsigned-verification-report", str(out / "dfinsta.verification.json"),
        "--policy", str(REPOSITORY / "release" / "signing_policy.json"),
        "--zipalign", str(tools / "zipalign"),
        "--apksigner", str(tools / "apksigner"),
        "--aapt", str(tools / "aapt"),
        "--apktool-jar", str(REPOSITORY / "apktool_2.9.3.jar"),
        "--final-verifier", str(REPOSITORY / "tools" / "verify" / "verify_build.py"),
        "--output-apk", str(output or port.signed),
    ]


def _adb() -> list[str]:
    return [str(Path.home() / "Android" / "Sdk" / "platform-tools" / "adb")]


def _assessment_recorded(port: Port) -> bool:
    """Is this run's assessment already in the ledger, and reachable by run id?

    Answers **False whenever it cannot tell** — no state root, no ledger, an
    operation that never completed — because re-recording refuses by design and
    a wrong "already done" would send the run to a gate with nothing behind it.

    Reads the ledger and the content store, both of which are directories on
    this machine, so asking costs nothing and needs no server. That matters:
    every predicate is evaluated for every step before anything runs, including
    when this tool is only being asked where the port stands.
    """

    if not port.state_root or not port.assessment_run_id:
        return False
    try:
        sys.path.insert(0, str(REPOSITORY / "src"))
        from dfinsta_pipeline import activities, assessment_record  # noqa: PLC0415

        # `configure_runtime` returns None — it installs the runtime, and
        # `runtime()` is the accessor. Getting that wrong made this predicate
        # answer False for an assessment that had just been recorded, and the
        # runbook refused the step it had itself completed. The broad `except`
        # below is what hid it: it is right for "cannot tell", and it swallows a
        # typo just as happily, which is why the live run was the thing that
        # found this and the unit tests could not have.
        activities.configure_runtime(port.state_root, read_only=True)
        configured = activities.runtime()
        assessment_record.resolve_with(
            configured.ledger, configured.store, port.assessment_run_id
        )
    except Exception:  # noqa: BLE001 - absent, unreadable and incomplete alike
        return False
    return True


def _gate_not_configured(port: Port) -> str:
    """Why this run cannot raise its own gate, or "" when it can.

    Named individually rather than as "the gate is not configured", because the
    thing an operator has got wrong is almost always one of them — and
    `--build-id` is the one whose absence produces no error at all, just a
    Workflow nobody ever runs.
    """

    if not _unruled_candidates(port):
        # Nothing to rule means nothing to raise, so there is nothing to be
        # blocked from. Without this a run that was never given the gate flags
        # — which is every run before 2026-08-19, and the default — would report
        # BLOCKED for a step that has no work in it.
        return ""
    missing = [
        name for name, value in (
            ("--state-root", port.state_root),
            ("--assessment-run-id", port.assessment_run_id),
            ("--actor", port.actor),
            ("--owner-token", port.owner_token),
            ("--build-id", port.build_id),
        ) if not value
    ]
    if not missing:
        return ""
    return (
        f"this run cannot raise its own gate: {', '.join(missing)} not given. "
        "Rule on the candidates by hand, or re-run with them and the gate is "
        "raised and waits for you"
    )


def _index_leavings(port: Port) -> list[Path]:
    """A half-built index, and nothing else.

    Only reached when the step is about to run, which now means the completion
    marker is absent — so a *complete* index is never a candidate for removal.
    And never an index this run did not make: `--reuse-index` points at an
    earlier run's directory, and deleting somebody else's expensive artefact
    because this run wanted to rebuild is not a thing a resumable tool may do.
    """

    own = port.out / "index"
    if port.reuse_index or not own.is_dir():
        return []
    return [own]


def _build_leavings(port: Port) -> list[Path]:
    """What a failed build left in the run directory.

    Stated as everything *except* the three things earlier steps produced,
    rather than as a list of the build's outputs: the builder refuses to
    overwrite seven paths today and the driver three more, and a list here
    would be a fourth place to keep in step with them. The decode, the index
    and the framework are the expensive ones and are never touched.
    """
    keep = {port.decode, port.index_dir, port.out / "framework"}
    if not port.out.is_dir():
        return []
    return sorted(path for path in port.out.iterdir() if path not in keep)


def _unruled_candidates(port: Port) -> str:
    """Candidates this version raised that nobody has ruled on yet.

    The shippable build is rendered from `url_block_rules`, and ruling changes
    them — so building before the human has ruled ships a build that predates the
    decision, silently and with every check passing. This is the same
    completeness rule the feature gate already applies to a submission: a
    candidate that was skipped is not an `ignore`, it is unanswered.

    A candidate counts as answered once it has ANY row in `manifest/rulings.jsonl`.
    Not "once it stops being a candidate": an endpoint ruled `ignore` or `defer`
    is still an uncovered gap and would otherwise block every future port forever.
    """
    import importlib.util  # noqa: PLC0415

    if not port.index_dir.is_dir():
        return ""
    spec = importlib.util.spec_from_file_location(
        "watch_candidates", REPOSITORY / "tools" / "watch_candidates.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    manifest = REPOSITORY / "manifest" / "hooks.json"
    candidates = {str(item) for item in module.candidates(port.index_dir, manifest)}
    if not candidates:
        return ""
    rulings = REPOSITORY / "manifest" / "rulings.jsonl"
    ruled: set[str] = set()
    if rulings.is_file():
        for line in rulings.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            identifier = row.get("record", row).get("candidate_id", "")
            ruled.add(identifier[4:] if identifier.startswith("gap:") else identifier)
    outstanding = sorted(candidates - ruled)
    if not outstanding:
        return ""
    return (
        f"{len(outstanding)} candidate(s) have no ruling: {', '.join(outstanding[:4])}"
        f"{' …' if len(outstanding) > 4 else ''}. The shipped build renders "
        "url_block_rules, so building now would ship a decision nobody made. Rule "
        "them first — the steps are printed when the mechanical ones finish."
    )


def _ship_leavings(port: Port) -> list[Path]:
    """What a failed shippable build left behind.

    Same rule as `_build_leavings`, with a smaller keep-list: the decode and the
    index are reused from the port directory and were never here. `framework` is
    an apktool cache rather than an output — `tools/port_430/build.py` says so,
    having once refused to overwrite the directory it had just been handed.
    """
    if not port.ship_out.is_dir():
        return []
    return sorted(path for path in port.ship_out.iterdir() if path.name != "framework")


def _on_device_is(signed: Path) -> bool:
    """Is the APK on the phone byte-for-byte the one `signed` names?

    The version is not enough to tell two of this project's builds apart — both
    report `versionName=<version>.…`, both are signed by the same key, and on 442
    the observing and shipped APKs even came out the same number of bytes. So it
    compares digests, and answers **False whenever it cannot tell**: no signed
    APK, no release report, no `pm path`, no `sha256sum` on the device, a
    mismatch — every one of those means run the step. Re-running `install -r` on
    a build already installed costs twenty seconds and changes nothing, whereas a
    wrong "already done" costs the whole point of the run.

    **One function, since 2026-08-19, because there were two and only one was
    right.** `_installed` used to ask `dumpsys` for `versionName=<version>.`,
    reasoning that dumpsys reports no digest — true of dumpsys, and not true of
    the problem, as the `pm path` + `sha256sum` pair below had been demonstrating
    beside it the whole time. That made the *observing* install's check
    satisfiable by the *shipped* build, which is the last step of the same run:
    delete the captures to re-walk a finished port and `install` reads done, the
    walk measures a build with no observer, and `record-*` files the silence
    under the observing build's `build_sha256`. Every check passes and the
    corpus is wrong.
    """
    report = signed.with_suffix(".release.json")
    if not (signed.is_file() and report.is_file()):
        return False
    try:
        expected = json.loads(report.read_text(encoding="utf-8"))["outputs"]["apk_sha256"]
    except (OSError, KeyError, json.JSONDecodeError, TypeError):
        return False
    try:
        located = subprocess.run(
            _adb() + ["shell", "pm", "path", "com.instagram.android"],
            capture_output=True, text=True, timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no adb
        return False
    paths = [line.split(":", 1)[1].strip() for line in located.splitlines() if ":" in line]
    if len(paths) != 1:
        # A split APK, or none at all. Neither is a thing to guess about.
        return False
    try:
        digest = subprocess.run(
            _adb() + ["shell", "sha256sum", paths[0]],
            capture_output=True, text=True, timeout=180,
        ).stdout.split()
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no adb
        return False
    return bool(digest) and digest[0] == expected


def _shipped_is_installed(port: Port) -> bool:
    """Is the SHIPPED build on the phone, rather than the observing one?

    The last step of a port. Its counterpart is :func:`_installed`, which asks
    the same question about the observing build, and the two are one function so
    that a fix to either reaches both — they were separate, and the one guarding
    the corpus was the weaker of the two.
    """

    return _on_device_is(port.ship_signed)


STEPS: tuple[Step, ...] = (
    Step(
        "index",
        "decode the APK and index its API surface, so stage 4a can find candidates",
        lambda port: _driver(port, "--stop-after", "index"),
        # **The marker the indexer writes last, not the directory it makes
        # first.** `build_index` does `mkdir(exist_ok=True)` before it reads a
        # single file and writes `header.json` after the last one, so a crashed
        # index leaves a directory that `is_dir()` calls finished. This is the
        # only expensive step with no `stale`, so it could not heal itself
        # either: the step read done forever, and the *next* step's predicate
        # raised `IndexError_` out of the run — a traceback, not a refusal, and
        # not a word about the fix being `rm -rf`.
        #
        # Third of its family, after `warm` and `install`. See
        # `EveryStepCanFinishItsOwnStepTests`.
        lambda port: (port.index_dir / "header.json").is_file(),
        stale=_index_leavings,
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
        stale=_build_leavings,
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
    Step(
        "warm",
        "launch the app once and wait until the bottom nav reads, because the "
        "first launch after an install is slow enough to fail a walk",
        lambda port: [
            sys.executable, str(REPOSITORY / "tools" / "device_session.py"), "--warm",
            "--out", str(port.warm_marker),
        ],
        # **Its own artefact, since 2026-08-19.** This used to be "the first walk
        # has produced a capture", which warm does not do — so the step could
        # never both run and pass, and read as done only when a walk had already
        # started. Every port before 443 had captures on disk by the time it was
        # reached, so the seam was routed around rather than exercised.
        #
        # The walk count is still honoured, and deliberately: a corpus already in
        # progress means the nav has been read by something stricter than this,
        # and re-warming mid-corpus would force-stop the app between sessions for
        # no reason.
        lambda port: port.warm_marker.is_file() or walked(port, WALKS[0]) > 0,
        needs_device=True,
        minutes=1,
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
    # ------------------------------------------------------------- the gate
    #
    # Both steps are **no-ops when there is nothing to rule**, which is what 443
    # was: stage 4a raised no candidates, so there is no assessment worth
    # recording and nobody to ask. They are also no-ops when this run was not
    # given what raising a gate needs, which is every port before 2026-08-19 and
    # remains the default — the handoff stays printed rather than performed.
    #
    # What they buy when both do apply: the run stops as a **Workflow parked in
    # Temporal**, durable for a week, rather than as a shell command that ended.
    # A human can answer it tomorrow from another machine holding nothing but a
    # run id, and an unanswered gate expires to `blocked`, which is never an
    # implicit approval.
    Step(
        "assess",
        "record the assessment a human will rule against, so the gate has a subject",
        lambda port: _driver(
            port, "--stop-after", "assess",
            "--state-root", str(port.state_root),
            "--assessment-run-id", port.assessment_run_id,
            "--actor", port.actor,
            "--owner-token", port.owner_token,
        ),
        # Nothing to rule means nothing to record. Otherwise the ledger row is
        # the artefact, and it is this step's own — `resolve_with` is the exact
        # path the gate client uses, so a recorded assessment that this cannot
        # find is one the human could not have ruled on either.
        lambda port: not _unruled_candidates(port) or _assessment_recorded(port),
        blocked_by=_gate_not_configured,
        minutes=2,
    ),
    Step(
        "raise-gate",
        "start the Workflow that asks a human, and park until they answer",
        lambda port: [
            sys.executable, "-m", "dfinsta_pipeline.assessment_record", "raise",
            "--run-id", port.assessment_run_id,
            "--endpoint", port.endpoint,
            "--task-queue", port.task_queue,
            "--build-id", port.build_id,
            "--gate-timeout-seconds", str(port.gate_timeout_seconds),
            "--write-workflow-id", str(port.gate_marker),
        ],
        # The marker, not the ledger: raising twice would start a second
        # Workflow against the same subject, and the first one would sit open
        # until it expired. `_unruled_candidates` stays non-empty for as long as
        # the human has not answered, so it cannot be the thing that says this
        # step is finished.
        lambda port: not _unruled_candidates(port) or port.gate_marker.is_file(),
        blocked_by=_gate_not_configured,
        minutes=1,
    ),
    # Last, and last on purpose: this replaces the observing build on the phone,
    # and the observing build is the one every recorded session was measured
    # against. Both corpora are on disk and committed by the time this runs.
    Step(
        "ship-build",
        "build the APK you would actually use — no observer, no log tag",
        lambda port: _driver(
            port, "--stop-after", "build",
            out=port.ship_out,
        ),
        lambda port: port.ship_unsigned.is_file(),
        minutes=15,
        stale=_ship_leavings,
        blocked_by=_unruled_candidates,
    ),
    Step(
        "ship-sign",
        "sign it with the release key and put it through the release gate",
        lambda port: _sign(port, out=port.ship_out, output=port.ship_signed),
        lambda port: port.ship_signed.is_file(),
        minutes=3,
        env=lambda port: {name: os.environ[name] for name in _SIGNING if name in os.environ},
    ),
    Step(
        "ship-install",
        "put it on the phone, replacing the observing build",
        lambda port: _adb() + ["install", "-r", str(port.ship_signed)],
        _shipped_is_installed,
        needs_device=True,
    ),
)

_SIGNING = ("DFINSTA_KEYSTORE", "DFINSTA_KEY_ALIAS", "DFINSTA_KEYSTORE_PASSWORD")

#: The `env` every step gets unless it asks for one. Identity against it is how a
#: signing step is recognised, so the test and the tool agree by construction.
_DEFAULT_ENV = Step.__dataclass_fields__["env"].default


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
    """Is the OBSERVING build on the phone — the one every session is measured
    against?

    The same digest comparison the shipped install uses, and for a sharper
    reason: this predicate guards the corpus. A walk run against the wrong build
    still produces twelve well-formed captures, and `record-*` will file them
    under this build's `build_sha256` because that is what the runbook passes.
    Nothing downstream can tell.
    """

    return _on_device_is(port.signed)


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
    gate = parser.add_argument_group(
        "raising this run's gate",
        "Give all five and the run records its assessment and raises the gate "
        "itself, then parks in Temporal until you answer. Give none and the "
        "handoff is printed instead, which is what it always was. Nothing here "
        "is invented: an actor and an owner token identify a person, and a tool "
        "that made them up would be signing on somebody's behalf.",
    )
    gate.add_argument("--state-root", type=Path, default=None,
                      help="the ledger and content store")
    gate.add_argument("--assessment-run-id", default="",
                      help="how the recorded assessment is found again; the gate "
                           "client takes nothing but this")
    gate.add_argument("--actor", default="", help="who may answer the gate")
    gate.add_argument("--owner-token", default="",
                      help="what lets a wedged operation be reclaimed later")
    gate.add_argument("--build-id", default="",
                      help="the --build-id the worker was started with. Both "
                           "workflows are PINNED, so without it the gate is "
                           "accepted by the server, dispatched to nobody, and "
                           "every query times out with nothing naming the cause")
    gate.add_argument("--endpoint", default="localhost:7233")
    gate.add_argument("--task-queue", default="dfinsta-phase-a",
                      help="the queue the worker polls. Note this differs from "
                           "`assessment_record raise`'s own default")
    gate.add_argument("--gate-timeout-seconds", type=int, default=7 * 24 * 3600,
                      help="a week by default; an unanswered gate ends `blocked`")
    parser.add_argument("--run", action="store_true",
                        help="execute the steps that are not done; without it nothing runs")
    args = parser.parse_args(argv)

    if not args.apk.is_file():
        print(f"refusing: {args.apk} is not a file", file=sys.stderr)
        return 2
    if args.reuse_decode is not None and not args.reuse_decode.is_dir():
        # Carrying on would extract into the default location instead, so the
        # run would succeed while measuring a decode of nobody's choosing.
        print(f"refusing: --reuse-decode {args.reuse_decode} is not a directory",
              file=sys.stderr)
        return 2
    port = Port(
        apk=args.apk,
        version=args.version,
        out=args.out or REPOSITORY / "work" / f"{args.version}-port",
        captures=args.captures or REPOSITORY / "work" / f"observations-{args.version}",
        reuse_decode=args.reuse_decode,
        state_root=args.state_root,
        assessment_run_id=args.assessment_run_id,
        actor=args.actor,
        owner_token=args.owner_token,
        endpoint=args.endpoint,
        task_queue=args.task_queue,
        build_id=args.build_id,
        gate_timeout_seconds=args.gate_timeout_seconds,
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
        try:
            done = step.done(port)
        except Exception as error:  # noqa: BLE001 - every predicate, every reason
            # A predicate that raises used to kill the run with a traceback from
            # a module the operator never invoked — `watch` reading a half-built
            # index raised `IndexError_` from the indexer, four frames deep, and
            # said nothing about which step or what to do. Named here instead.
            # Deliberately not "treat it as not done": a predicate that cannot
            # answer is a broken artefact, and running the step on top of one is
            # how a corrupt input becomes a corrupt output.
            print(f"refusing: the {step.name} step cannot tell whether it is already "
                  f"done: {type(error).__name__}: {error}", file=sys.stderr)
            print(f"           the artefact it reads is unreadable rather than absent. "
                  f"Remove it and re-run, or say why it is like that.", file=sys.stderr)
            return 1
        # **Asked before `done`, since 2026-08-19.** `ship-build` renders
        # `url_block_rules`, and a build made before a candidate was ruled does
        # not carry the ruling — so an artefact left by an earlier run must not
        # let the step report finished once a new candidate appears. It did:
        # after a completed port, adding an endpoint to the manifest left
        # `ship-build` reading `[done]` off the old APK and the run announcing
        # "the port is finished" with a build that predates the decision, which
        # is the exact thing `_unruled_candidates` was written to prevent.
        #
        # Safe in the other direction because every `blocked_by` answers "" when
        # its step has nothing to do — a step cannot be blocked from work that
        # does not exist.
        reason = step.blocked_by(port)
        if done and not reason:
            print(f"  [done] {step.name}")
            continue
        remaining += step.minutes
        blocked = (step.needs_device and not have_device) or bool(reason)
        mark = "BLOCKED" if blocked else "todo"
        print(f"  [{mark}] {step.name:22} ~{step.minutes}m   {step.why}")
        if blocked:
            print(f"           {reason or 'attach the phone; this step and everything after it need it'}")
            if reason:
                _judgement_steps(port)
            return 1
        if not args.run:
            continue
        # Any step that signs, not the one named `sign`. `ship-sign` was added
        # later and named differently, so it would have run `finalize.py` with no
        # credentials and failed inside the signer — a confusing error for a thing
        # this tool exists to refuse by name.
        # A step that signs is one that declares an `env`, not one whose NAME
        # ends in "sign". Two traps avoided: `ship-sign` was added later under a
        # different name and a name-equality check missed it entirely, and
        # `step.env(port)` is empty precisely when the secrets are unset — so
        # asking it would have made the refusal conditional on the thing it
        # refuses the absence of.
        missing = (
            [name for name in _SIGNING if name not in os.environ]
            if step.env is not _DEFAULT_ENV
            else []
        )
        if missing:
            print(f"refusing: {missing[0]} is not set. This tool never reads, defaults or "
                  "prints the signing secrets; export them and re-run", file=sys.stderr)
            return 2
        for path in step.stale(port):
            # Announced, because removing a build tree silently is how a tool
            # loses the trust that lets it be run unattended.
            print(f"           removing {path}, left by an attempt that did not finish")
            shutil.rmtree(path) if path.is_dir() else path.unlink()
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
    print(f"\nThe port is finished. {port.ship_signed.name} is signed and on the phone.")
    print("  built   " + str(port.ship_signed))
    print("  measured" + f"  {port.sessions_for(WALKS[0])} + {port.sessions_for(WALKS[1])} "
          "sessions recorded against the observing build")
    return 0


def _judgement_steps(port: Port) -> None:
    """What is left for a human, which depends on whether the gate was raised.

    Two of the five used to be typing this tool would not do, because raising a
    gate needs a run id, an actor and an owner token, and a tool that invented
    those would be signing a document on somebody's behalf. Given them, it does
    exactly those two — and what remains is judgement, or follows from it.
    """

    raised = _raised_gate(port)
    print("\n  What is yours to do:")
    if raised is None:
        print("    1. record the assessment   driver --stop-after assess --state-root … "
              "--assessment-run-id … --actor … --owner-token …")
        print("    2. raise the gate          python -m dfinsta_pipeline.assessment_record raise …")
        print("       — or re-run this with --state-root/--assessment-run-id/--actor/")
        print("         --owner-token/--build-id and both happen here, leaving the gate")
        print("         open and waiting for you.")
    else:
        hours = int(raised.get("gate_timeout_seconds") or 0) // 3600
        print(f"    the gate is raised and waiting: {raised.get('workflow_id', '?')}")
        print(f"    on {raised.get('endpoint', '?')}, queue {raised.get('task_queue', '?')}"
              + (f", open for {hours}h" if hours else ""))
        print("    An unanswered gate expires to `blocked`, which is never an approval.")
    print("    · rule on the candidates  python -m dfinsta_pipeline.submission show \\")
    print("                                  --assessment --rulings-template --consent-test")
    print("                              python -m dfinsta_pipeline.submission submit …")
    print("    · apply the rulings       python -m dfinsta_pipeline.rulings … --apply")
    print("    · write the url_block_rules entry by hand: the match kind and the toggle are")
    print("      not derivable, and the regularity that made the match kind look derivable")
    print("      has since inverted — five of eleven literals break it")
    print("\n  Then re-run this; it resumes at the shippable build.")


def _raised_gate(port: Port) -> dict | None:
    """What this run's raised gate says about itself, or `None` if none was.

    Unreadable counts as absent **here and only here**: this is a message for a
    human, and withholding the rest of it because a marker got truncated would
    be the tail wagging the dog. Every predicate that decides anything keeps
    absent and unreadable apart.
    """

    if not port.gate_marker.is_file():
        return None
    try:
        found = json.loads(port.gate_marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return found if isinstance(found, dict) else None


if __name__ == "__main__":
    raise SystemExit(main())

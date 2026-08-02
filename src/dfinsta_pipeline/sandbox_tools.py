"""Read-only tools an agent gets over a decode sandbox, and nothing else.

`proposer.py` builds the sandbox and writes the prompt; `proposals.py` decides
what an answer is worth. This is the missing third of stage 5a: what the agent
can actually *do* while it works. Until now that was a human with a terminal,
which is why no run produces its own candidates end to end.

The tool surface is deliberately three verbs — list, read, search — and there is
no fourth. Two reasons, and both are load-bearing rather than tidiness.

**A sandbox is hardlinked, so a write is not a mistake, it is corruption.**
`build_sandbox` uses `cp -al` because a 1.7 GiB decode is otherwise not free to
copy, and the consequence is that the sandbox and the master decode share
inodes. There is therefore no write tool that could be made safe by refusing bad
paths; the safe number of write tools is zero, and the safe number of shell
tools is zero for the same reason.

**A blind experiment needs the answers absent, not forbidden.** The prompt tells
the proposer not to read the repository, and that is worth exactly nothing on its
own: this repository holds the resolved anchor for every version ported so far,
and a model that wanders into `manifest/hooks.json` produces a number that looks
like a result and measures nothing. `build_sandbox` removes the answers from the
*tree*; :class:`SandboxReader` removes them from the *reach*. Every path an agent
supplies is resolved and then required to live under the root, so a `..`, an
absolute path, or a symlink inside the decode that points at a home directory all
fail the same check.

What that check does not defend against is an attacker swapping a symlink between
the resolution and the open. That is deliberate: the threat here is a model
wandering, not a same-uid attacker racing us, and the rest of this pipeline
already places same-uid attackers outside its trust boundary. Saying so is better
than implying a guarantee that is not being made.

Nothing here is specific to a model vendor. :func:`tool_specifications` returns
the three tools as plain JSON Schema, and :meth:`SandboxReader.dispatch` takes a
name and a decoded argument object, so whichever runtime ends up calling this
adapts at its own edge rather than in here.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

#: One read cannot return more than this. A model that is handed 400 KB of smali
#: spends its context on it and reasons worse, and the files that tempt it are
#: exactly the huge generated ones that are least likely to hold the answer.
MAX_READ_BYTES = 64_000

#: Lines per read when the caller does not choose. Enough to see a method and its
#: neighbours without pulling a whole class.
DEFAULT_READ_LINES = 200
MAX_READ_LINES = 2_000

#: Search results are capped, and the cap is REPORTED. A silently truncated
#: search is how a proposer concludes "this literal appears in one class" when it
#: appears in five — which is the precise mistake `co_literals` exists to stop.
MAX_MATCHES = 200

#: `grep --max-count` is per file. Kept low so the overall budget is spent on
#: *how many files* contain the pattern rather than on one file containing it
#: repeatedly: "which classes hold this literal" is the question a proposer is
#: actually asking, and one loop-heavy class would otherwise crowd out the rest.
MAX_MATCHES_PER_FILE = 20

#: `grep` over a 1.7 GiB decode is fast, but a pathological pattern is not.
SEARCH_TIMEOUT_SECONDS = 120

#: A pattern this long is a mistake or an attempt to hang the search.
MAX_PATTERN = 512

MAX_ENTRIES = 1_000


class SandboxDenied(RuntimeError):
    """A tool call that would leave the sandbox, or that cannot be honoured.

    Raised rather than returned as a normal result: an escape attempt is not a
    search that found nothing, and the two must never be confusable by whatever
    is reading the transcript afterwards.
    """


def _relative(root: Path, path: Path) -> str:
    return str(path.relative_to(root)) or "."


@dataclass(frozen=True)
class SandboxReader:
    """Read-only access to one decode sandbox. Every path is checked, every time.

    `root` must already be resolved. It is not re-resolved per call so that a
    caller cannot narrow or widen the sandbox after construction by mutating the
    object -- the dataclass is frozen for the same reason.
    """

    root: Path
    max_read_bytes: int = MAX_READ_BYTES
    max_matches: int = MAX_MATCHES
    timeout_seconds: int = SEARCH_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path):
            raise TypeError("Sandbox root must be a Path")
        if self.root != self.root.resolve():
            raise SandboxDenied(f"Sandbox root must be resolved: {self.root}")
        if not self.root.is_dir():
            raise SandboxDenied(f"Sandbox root is not a directory: {self.root}")
        for name, value in (
            ("max_read_bytes", self.max_read_bytes),
            ("max_matches", self.max_matches),
            ("timeout_seconds", self.timeout_seconds),
        ):
            if type(value) is not int or value < 1:
                raise SandboxDenied(f"Sandbox {name} must be a positive integer")

    # ------------------------------------------------------------ confinement

    def resolve(self, candidate: object) -> Path:
        """Turn an agent-supplied path into a real path inside the sandbox, or refuse.

        `Path.resolve()` is what makes this work: it collapses `..` *and* follows
        symlinks, so the containment test runs against where the path actually
        lands rather than where it claims to. A decode is extracted from an APK
        and can legitimately contain links; one pointing at a home directory
        would otherwise be a hole with nobody at fault.

        An absolute path is not rejected outright -- the prompt hands the agent
        the sandbox's absolute path, so it will use absolute paths, and refusing
        them would only teach it to prefix everything with the root by hand.
        Absolute or relative, the same containment test decides.
        """

        if type(candidate) is not str or not candidate.strip():
            raise SandboxDenied("Path must be a non-empty string")
        if "\x00" in candidate:
            raise SandboxDenied("Path must not contain a null byte")
        supplied = Path(candidate)
        joined = supplied if supplied.is_absolute() else self.root / supplied
        try:
            resolved = joined.resolve()
        except OSError as error:
            raise SandboxDenied(f"Path cannot be resolved: {candidate}") from error
        if resolved != self.root and self.root not in resolved.parents:
            raise SandboxDenied(
                f"{candidate} resolves outside the sandbox. Everything you need is "
                f"under {self.root}; a previously recorded answer exists elsewhere on "
                "this machine and using it would make the result worthless."
            )
        return resolved

    # ------------------------------------------------------------------ tools

    def list_dir(self, path: str = ".") -> str:
        target = self.resolve(path)
        if not target.is_dir():
            raise SandboxDenied(f"Not a directory: {_relative(self.root, target)}")
        entries: list[str] = []
        truncated = False
        with os.scandir(target) as scan:
            for index, entry in enumerate(sorted(scan, key=lambda item: item.name)):
                if index >= MAX_ENTRIES:
                    truncated = True
                    break
                # `@` for a symlink, `ls -F` style. Marked rather than hidden:
                # a link that leaves the sandbox is refused on use, and an agent
                # that can see why spends one turn on it instead of several.
                if entry.is_symlink():
                    suffix = "@"
                elif entry.is_dir(follow_symlinks=False):
                    suffix = "/"
                else:
                    suffix = ""
                entries.append(f"{entry.name}{suffix}")
        listing = "\n".join(entries) or "(empty)"
        if truncated:
            listing += f"\n... truncated at {MAX_ENTRIES} entries"
        return listing

    def read_file(
        self, path: str, start_line: int = 1, line_count: int = DEFAULT_READ_LINES
    ) -> str:
        """Return numbered lines, and say so whenever the answer is partial.

        Line numbers are included because every downstream consumer wants them:
        `evidence` is required to cite file and line, and an agent that has to
        count lines itself will cite them wrong.
        """

        target = self.resolve(path)
        if not target.is_file():
            raise SandboxDenied(f"Not a file: {_relative(self.root, target)}")
        if type(start_line) is not int or start_line < 1:
            raise SandboxDenied("start_line must be a positive integer")
        if type(line_count) is not int or not 1 <= line_count <= MAX_READ_LINES:
            raise SandboxDenied(f"line_count must be between 1 and {MAX_READ_LINES}")

        collected: list[str] = []
        size = 0
        stopped_on_bytes = False
        total = 0
        with target.open("r", encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle, start=1):
                total = number
                if number < start_line:
                    continue
                if len(collected) >= line_count:
                    # Keep counting so the footer can say how much was left.
                    continue
                rendered = f"{number:6d}  {line.rstrip()}"
                size += len(rendered) + 1
                if size > self.max_read_bytes:
                    stopped_on_bytes = True
                    break
                collected.append(rendered)
        if not collected:
            # Order matters here, and getting it wrong was a silent truncation:
            # a first line longer than the byte cap collected nothing, fell into
            # the branch below, and told the reader the range was empty. A
            # proposer looking at a generated file was informed there was
            # nothing in it. The cause is reported instead.
            if stopped_on_bytes:
                return (
                    f"(line {start_line} of {_relative(self.root, target)} is longer than "
                    f"{self.max_read_bytes} bytes and was not returned; this is a generated "
                    "or minified file)"
                )
            return f"({_relative(self.root, target)} has {total} lines; none in the requested range)"
        body = "\n".join(collected)
        last = start_line + len(collected) - 1
        if stopped_on_bytes:
            return (
                f"{body}\n... stopped at line {last} after {self.max_read_bytes} bytes; "
                "read again from there"
            )
        if last < total:
            return f"{body}\n... {total - last} more lines (file has {total})"
        return body

    def search(
        self, pattern: str, path: str = ".", include: str | None = None, fixed: bool = True
    ) -> str:
        """Search the sandbox, reporting the cap whenever it bites.

        Shelled out to `grep` with a fixed argv rather than reimplemented: a
        decode is well over a gigabyte and a Python scan of it is slow enough
        that an agent would search less, which is the opposite of what makes a
        proposal good. There is no shell, so nothing in `pattern` can become a
        command -- and `--` precedes it so that a pattern beginning with `-`
        cannot become a flag either.

        `fixed` defaults to True. The overwhelmingly common query here is an API
        path or a class descriptor, both full of regex metacharacters, and a
        literal search that finds it beats a regex that quietly matches
        something else.
        """

        if type(pattern) is not str or not pattern:
            raise SandboxDenied("Search pattern must be a non-empty string")
        if len(pattern) > MAX_PATTERN:
            raise SandboxDenied(f"Search pattern is longer than {MAX_PATTERN} characters")
        if "\x00" in pattern:
            raise SandboxDenied("Search pattern must not contain a null byte")
        if type(fixed) is not bool:
            raise SandboxDenied("fixed must be a boolean")
        if not fixed:
            try:
                re.compile(pattern)
            except re.error as error:
                raise SandboxDenied(f"Invalid regular expression: {error}") from error
        target = self.resolve(path)
        if not target.exists():
            # `--no-messages` suppresses grep's own explanation, so without this
            # a mistyped path returns a bare "Search failed:" and reads like a
            # dead end rather than like a typo.
            raise SandboxDenied(f"No such path in the sandbox: {_relative(self.root, target)}")

        # `-r`, never `-R`. They differ in exactly one way and it is the way that
        # matters here: `-R` follows symlinks encountered while recursing, so a
        # single link inside the decode would walk the search straight out of the
        # sandbox and quietly into the repository holding the answers. `-r` skips
        # them. The path argument itself is already resolved and contained above.
        #
        # `--max-count` is PER FILE, not overall, and that difference was measured
        # rather than assumed: on the real 439 decode, `invoke-static` produces
        # 202 MB of matches. Buffering that to return 200 lines is the kind of
        # waste that only shows up under a pattern nobody tried. Two bounds
        # instead — a small per-file cap so results span many files rather than
        # coming from the first one, and an overall cap enforced by reading
        # incrementally and killing grep the moment it is reached.
        # `-H` unconditionally: without it grep omits the filename when given a
        # single file, so the tool's own description ("returns file:line:text")
        # becomes false exactly when a proposer is narrowing in on one class.
        argv = [
            "grep",
            "-rnH",
            "--binary-files=without-match",
            "--no-messages",
            f"--max-count={MAX_MATCHES_PER_FILE}",
        ]
        if fixed:
            argv.append("-F")
        else:
            # `-E`, because the pattern was validated with Python's `re` and
            # grep's default is POSIX *basic* regex. Without this,
            # `invoke-(static|virtual)` and `a+` pass validation, run, and
            # return "no matches" -- a false negative shaped exactly like an
            # answer, in the tool used to decide whether a literal is unique.
            # ERE is not Python's dialect either, so a pattern using `\d` or a
            # lookahead still misbehaves; `fixed=True` is the default for that
            # reason and is what suits API paths and smali descriptors.
            argv.append("-E")
        if include is not None:
            if type(include) is not str or not include or "\x00" in include:
                raise SandboxDenied("include must be a non-empty string")
            argv.append(f"--include={include}")
        # `--` first, so a pattern that starts with a dash is a pattern.
        argv += ["--", pattern, str(target)]

        lines, truncated, failure = self._stream_matches(argv, pattern)
        if failure is not None:
            raise SandboxDenied(failure)
        if not lines:
            return f"no matches for {pattern!r}"
        rendered = "\n".join(self._relativise(line) for line in lines)
        if truncated:
            # Never silent. A capped search that reads as complete is how a
            # proposer concludes a literal is unique when it is not.
            rendered += (
                f"\n... more than {self.max_matches} matches; this list is TRUNCATED and "
                "you must not conclude anything is unique from it"
            )
        return rendered

    def _stream_matches(
        self, argv: list[str], pattern: str
    ) -> tuple[list[str], bool, str | None]:
        """Read grep's output incrementally and stop the moment the cap is hit.

        `subprocess.run` would buffer everything first, and "everything" is not
        hypothetical: `invoke-static` over the real 439 decode is 202 MB of
        matches, all of it collected in order to return 200 lines. Reading the
        pipe as it fills bounds memory to the cap and makes the broadest searches
        the *fastest* rather than the slowest.

        grep is killed rather than left to finish, and the pipe is drained after,
        because a process whose reader has stopped blocks forever on a full pipe.
        A killed grep's exit status is therefore not an error here.
        """

        lines: list[str] = []
        truncated = False
        stopped_early = False
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(self.root),
        )
        # A watchdog, not a deadline checked between lines. Checking inside the
        # read loop bounds the GAP BETWEEN RESULTS, not the call: a grep that
        # matches nothing across a 1.7 GiB tree produces no lines at all, so the
        # loop never runs and the timeout never fires. That is the shape of the
        # search most likely to be slow.
        expired = threading.Event()

        def on_deadline() -> None:
            expired.set()
            if process.poll() is None:
                process.kill()

        watchdog = threading.Timer(self.timeout_seconds, on_deadline)
        watchdog.daemon = True
        watchdog.start()
        try:
            assert process.stdout is not None
            for line in process.stdout:
                line = line.rstrip("\n")
                if line:
                    lines.append(line)
                if len(lines) >= self.max_matches:
                    truncated = stopped_early = True
                    break
        finally:
            # Kill ONLY when we stopped reading early. Killing unconditionally
            # looks harmless and is not: grep can have written every match and
            # closed stdout while still a moment from exiting, so `poll()` is
            # None, the kill lands on a process that was about to succeed, and a
            # complete search is reported as "Search failed" — intermittently,
            # which is the worst way for it to be wrong.
            watchdog.cancel()
            if stopped_early and process.poll() is None:
                process.kill()
            stderr = ""
            try:
                _, stderr = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - a wedged grep
                process.kill()
                process.communicate()
        if expired.is_set():
            return (
                [],
                False,
                f"Search for {pattern!r} exceeded {self.timeout_seconds}s; narrow it "
                "with `path` or `include`",
            )
        # grep exits 1 for "no matches", which is an answer rather than a
        # failure; a non-zero status after we killed it says nothing at all.
        if not stopped_early and process.returncode not in (0, 1):
            return [], False, f"Search failed: {(stderr or '').strip()[:200]}"
        return lines, truncated, None

    def _relativise(self, line: str) -> str:
        prefix = f"{self.root}/"
        return line[len(prefix) :] if line.startswith(prefix) else line

    # --------------------------------------------------------------- dispatch

    def dispatch(self, name: object, arguments: object) -> str:
        """Run one tool call by name. The single entry point a runtime needs.

        Unknown names and unexpected arguments are refused rather than ignored:
        a model calling a tool this sandbox does not have is a model working from
        a different idea of what it can do, and silently dropping the call would
        leave it reasoning about a result it never got.
        """

        if type(name) is not str:
            raise SandboxDenied("Tool name must be a string")
        if not isinstance(arguments, Mapping):
            raise SandboxDenied("Tool arguments must be an object")
        handlers = {
            "list_dir": (self.list_dir, {"path"}),
            "read_file": (self.read_file, {"path", "start_line", "line_count"}),
            "search": (self.search, {"pattern", "path", "include", "fixed"}),
        }
        if name not in handlers:
            raise SandboxDenied(
                f"Unknown tool {name!r}. This sandbox is read-only and offers exactly: "
                f"{', '.join(sorted(handlers))}."
            )
        handler, allowed = handlers[name]
        unexpected = sorted(set(arguments) - allowed)
        if unexpected:
            raise SandboxDenied(f"Unknown argument for {name}: {unexpected[0]}")
        try:
            return handler(**arguments)
        except TypeError as error:
            # A missing required argument would otherwise escape as `TypeError`,
            # and every caller here catches `SandboxDenied` in order to hand the
            # refusal back to the model. One omitted `path` would crash the run
            # instead of costing a turn.
            raise SandboxDenied(f"Invalid arguments for {name}: {error}") from error


def tool_specifications() -> list[dict[str, Any]]:
    """The three tools as plain JSON Schema, for a runtime to adapt at its edge.

    Vendor-neutral on purpose. Which agent runtime this pipeline ends up using is
    not settled, and a tool surface expressed in one vendor's wrapper types would
    have to be rewritten to find out.
    """

    return [
        {
            "name": "list_dir",
            "description": (
                "List one directory inside the decode. Directories end with '/'. "
                "Start here when you do not yet know the layout."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "directory, relative to the decode root or absolute inside it",
                    }
                },
                "required": [],
                "additionalProperties": False,
            },
        },
        {
            "name": "read_file",
            "description": (
                "Read numbered lines from one file. Output is capped, and says so "
                "when it is partial — read again from the reported line."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "line_count": {"type": "integer", "minimum": 1, "maximum": MAX_READ_LINES},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
        {
            "name": "search",
            "description": (
                "Search file contents under a path, returning 'file:line:text'. "
                "Literal by default, which is what you want for API paths and smali "
                "descriptors; set fixed=false for a regular expression. Results are "
                "capped and the cap is reported — never conclude a string is unique "
                "from a truncated list."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "include": {
                        "type": "string",
                        "description": "filename glob, e.g. '*.smali'",
                    },
                    "fixed": {"type": "boolean"},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
        },
    ]


def tool_transcript(calls: Iterator[tuple[str, Mapping[str, Any], str]]) -> str:
    """Render tool calls for the record, so a proposal can be audited later.

    A proposal's `evidence` is the agent's own account of what it checked. This
    is what actually happened, which is a different thing, and the gap between
    them is where a fabricated justification shows up.
    """

    parts = []
    for name, arguments, result in calls:
        parts.append(f"$ {name} {json.dumps(arguments, sort_keys=True)}\n{result}")
    return "\n\n".join(parts)

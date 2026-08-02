"""Tests for the three read-only tools a proposer gets over a decode sandbox.

The module's governing rule is one sentence: **nothing an agent can say reaches
outside the sandbox root, and nothing it is handed back can be mistaken for more
than it is.** Almost every test here is an attack on one of the three ways that
rule quietly stops holding.

**Containment is the validity of the experiment, not a hardening nicety.** Stage
5a is a blind holdout. The sandbox is a hardlinked copy of a stock decode, and
elsewhere on the same machine sits this repository, holding the resolved anchor
for every hook and every version already ported. One file read outside the root
turns the whole run into a number that looks like a result and measures nothing —
and it fails silently, because a proposer that read the answer produces a *better*
looking proposal, not a broken one. `ContainmentTests` therefore runs the same
escape against all three tools and against `dispatch`, and pairs every refusal
with a legitimate path that must be allowed: a containment test with no positive
control passes just as well when the tool is broken.

Two mutations that this suite exists to kill, because both are what a careful
person would plausibly write:

* `os.path.normpath` instead of `Path.resolve()`. It collapses `..` and does
  *not* follow symlinks, so every `..` test still passes while a single link in
  the decode pointing at a home directory becomes a hole.
  `test_a_symlink_to_an_outside_*` and
  `test_a_legitimate_path_through_an_inside_symlink_is_allowed` are the pair that
  catches it in both directions.
* `str(resolved).startswith(str(root))`. `/tmp/x/sandbox-answers` starts with
  `/tmp/x/sandbox`. The fixture ships a sibling directory named exactly that.

**A partial answer that does not say it is partial is worse than no answer.** A
capped search that reads as complete is how a proposer concludes a literal is
unique when it appears in five classes, which is the mistake `co_literals` exists
to stop. Every truncation path is asserted on its exact wording, and
`SearchCapTests` builds trees that genuinely exceed the caps rather than trusting
that the branch is reachable.

**There is no fourth verb.** The sandbox shares inodes with the master decode, so
a write is not a mistake but corruption of the original. `DispatchTests` and
`ToolSpecificationTests` assert that the surface is exactly three read verbs —
by name, and by refusing every mutation verb a model might reach for.

Six behaviours here were defects first and are now fixed, and each keeps a test
that fails if the fix is reverted, with the reason in the docstring rather than
in a changelog nobody reads: the byte cap no longer reports an unreadable line as
an empty range; `fixed=False` runs `-E` so the engine that validates the pattern
and the engine that runs it agree; the timeout is a watchdog and so bounds a grep
that produces nothing; a missing path is named instead of failing bare; `-H` makes
the result shape independent of whether `path` was a file; and `dispatch` converts
`TypeError` so every refusal a model can provoke arrives as `SandboxDenied`.

Five things remain wrong on purpose, and each says DELIBERATE WART where it is
pinned, because a wart that is not written down is indistinguishable from a bug
nobody found: a non-`Path` root raises `TypeError` rather than `SandboxDenied`;
four of the seven caps are module constants a caller cannot set; `grep` is
resolved through `PATH`; ERE is still not Python's dialect, so `\\d` and lookaround
return a clean "no matches"; and the truncation warning fires at exactly the cap
as well as beyond it. The first four are judged not worth what changing them
would cost; the last is an asymmetry that points one way.
"""

import dataclasses
import json
import os
import re
import subprocess
import time
import types
import unittest
from unittest import mock
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable

from dfinsta_pipeline import sandbox_tools
from dfinsta_pipeline.sandbox_tools import (
    DEFAULT_READ_LINES,
    MAX_ENTRIES,
    MAX_MATCHES,
    MAX_MATCHES_PER_FILE,
    MAX_PATTERN,
    MAX_READ_BYTES,
    MAX_READ_LINES,
    SandboxDenied,
    SandboxReader,
    tool_specifications,
    tool_transcript,
)


# --------------------------------------------------------------------- fixture

#: What must never be reachable. Stands in for `manifest/hooks.json`: a string
#: that exists only outside the sandbox, so any test that finds it has found a
#: hole rather than a coincidence.
ANSWER_MARKER = "RESOLVED-ANCHOR-LX-0aaa-A02"

ANSWERS = json.dumps(
    {"hook": "settings", "descriptor": "LX/0aaa;", "anchor": ANSWER_MARKER}, indent=2
)

#: 19 lines. Line numbers are load-bearing in the read tests below, so this is
#: written out rather than generated.
FOUR_TC = """\
.class public final LX/04tC;
.super Ljava/lang/Object;
.source "ProfileActionBarHelper.java"


# direct methods
.method public static A00(LX/0aaa;)V
    .locals 2

    const-string v0, "feed/timeline_stream/"

    invoke-static {v0}, LX/05t2;->A01(Ljava/lang/String;)Z

    move-result v1

    invoke-virtual {p0, v1}, LX/0aaa;->A02(Z)V

    return-void
.end method
"""

#: 17 lines.
FIVE_T2 = """\
.class public final LX/05t2;
.super Ljava/lang/Object;
.source "TimelineFetcher.java"


# direct methods
.method public static A01(Ljava/lang/String;)Z
    .locals 1

    const-string v0, "instagram://feed"

    invoke-static {p0, v0}, LX/04tC;->A03(Ljava/lang/String;Ljava/lang/String;)Z

    move-result v0

    return v0
.end method
"""

#: 11 lines, in a second dex directory: `search` has to span them.
ZERO_AAA = """\
.class public final LX/0aaa;
.super Ljava/lang/Object;
.source "ProfileActionBar.java"


# virtual methods
.method public final A02(Z)V
    .locals 0

    return-void
.end method
"""

MANIFEST = """\
<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="com.instagram.android">
    <application android:label="Instagram">
        <activity android:name="com.instagram.mainactivity.MainActivity"/>
    </application>
</manifest>
"""

TC_PATH = "smali_classes4/X/04tC.smali"
T2_PATH = "smali_classes4/X/05t2.smali"
AAA_PATH = "smali_classes12/X/0aaa.smali"
MANIFEST_PATH = "AndroidManifest.xml"

#: A pattern for the confinement tests, which are about `path` and must not be
#: refused for some unrelated reason before `path` is ever looked at.
ANY_PATTERN = "const-string"


class SandboxTestCase(unittest.TestCase):
    """One decode-shaped sandbox per test, with the answers next door.

    The layout matters as much as the contents. `base` holds three siblings: the
    sandbox itself, an `answers` directory standing in for this repository, and
    `sandbox-answers`, whose name begins with the sandbox's own — the prefix trap
    that a `startswith` containment test walks straight into.
    """

    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.base = Path(self._directory.name).resolve()

        self.root = self.base / "sandbox"
        (self.root / "smali_classes4" / "X").mkdir(parents=True)
        (self.root / "smali_classes12" / "X").mkdir(parents=True)
        self.write(TC_PATH, FOUR_TC)
        self.write(T2_PATH, FIVE_T2)
        self.write(AAA_PATH, ZERO_AAA)
        self.write(MANIFEST_PATH, MANIFEST)

        # Outside, and unreachable if this module works.
        self.answers = self.base / "answers"
        self.answers.mkdir()
        (self.answers / "hooks.json").write_text(ANSWERS, encoding="utf-8")
        self.prefix_trap = self.base / "sandbox-answers"
        self.prefix_trap.mkdir()
        (self.prefix_trap / "hooks.json").write_text(ANSWERS, encoding="utf-8")

        # Links a decode could plausibly contain, all of them escapes.
        (self.root / "escape_dir").symlink_to("../answers")
        (self.root / "escape_file").symlink_to("../answers/hooks.json")
        (self.root / "chain_hop").symlink_to("escape_file")
        (self.root / "chain").symlink_to("chain_hop")
        # And two that stay inside, which must keep working.
        (self.root / "inside_link").symlink_to(TC_PATH)
        (self.root / "deep").symlink_to("smali_classes4/X")

        self.reader = SandboxReader(root=self.root)

    # -- helpers

    def write(self, relative: str, body: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def escape_message(self, candidate: str) -> str:
        return (
            f"{candidate} resolves outside the sandbox. Everything you need is "
            f"under {self.root}; a previously recorded answer exists elsewhere on "
            "this machine and using it would make the result worthless."
        )

    def tool_calls(self, path: Any) -> dict[str, Callable[[], str]]:
        """Every route by which an agent-supplied path reaches `resolve`."""
        return {
            "list_dir": lambda: self.reader.list_dir(path),
            "read_file": lambda: self.reader.read_file(path),
            "search": lambda: self.reader.search(ANY_PATTERN, path),
            "dispatch/list_dir": lambda: self.reader.dispatch("list_dir", {"path": path}),
            "dispatch/read_file": lambda: self.reader.dispatch("read_file", {"path": path}),
            "dispatch/search": lambda: self.reader.dispatch(
                "search", {"pattern": ANY_PATTERN, "path": path}
            ),
        }

    def assert_every_tool_refuses(self, path: Any, message: str) -> None:
        for label, call in self.tool_calls(path).items():
            with self.subTest(tool=label):
                with self.assertRaises(SandboxDenied) as raised:
                    call()
                self.assertEqual(str(raised.exception), message)

    def denied(self, call: Callable[[], Any]) -> str:
        with self.assertRaises(SandboxDenied) as raised:
            call()
        return str(raised.exception)

    def match_files(self, output: str) -> list[str]:
        """The distinct files a search result names, sorted.

        Sorted rather than in the order grep produced them: `grep -r` walks a
        directory in `readdir` order, which is neither alphabetical nor stable
        across filesystems, and a test that pinned it would fail on someone
        else's machine for a reason that has nothing to do with this module.
        """
        return sorted({line.split(":", 1)[0] for line in output.splitlines()})


# ---------------------------------------------------------------- construction


class ConstructionTests(SandboxTestCase):
    """A reader that was never a sandbox cannot confine anything."""

    def test_a_resolved_directory_is_accepted(self) -> None:
        reader = SandboxReader(root=self.root)
        self.assertEqual(reader.root, self.root)
        self.assertEqual(reader.max_read_bytes, MAX_READ_BYTES)
        self.assertEqual(reader.max_matches, MAX_MATCHES)

    def test_the_root_must_be_a_path(self) -> None:
        """A DELIBERATE WART: this one refusal is a `TypeError`, not a `SandboxDenied`.

        Every other refusal in the module is a `SandboxDenied`, and `dispatch`
        now converts even a missing tool argument into one. This does not
        convert, and the inconsistency is on purpose rather than overlooked: no
        agent can reach this argument. `root` comes from the caller that built
        the sandbox, so a non-`Path` here is a bug in the pipeline and should
        stop it, where an agent's bad path is a turn to be spent. A runtime
        catching only `SandboxDenied` around construction therefore gets a
        traceback, which is the outcome that belongs to a programming error.
        """
        for value in (str(self.root), None, 7, b"/tmp"):
            with self.subTest(root=value):
                with self.assertRaises(TypeError) as raised:
                    SandboxReader(root=value)  # type: ignore[arg-type]
                self.assertEqual(str(raised.exception), "Sandbox root must be a Path")

    def test_an_unresolved_root_is_refused(self) -> None:
        """Otherwise every containment test compares against a path nothing equals."""
        unresolved = self.root / "smali_classes4" / ".."
        self.assertEqual(
            self.denied(lambda: SandboxReader(root=unresolved)),
            f"Sandbox root must be resolved: {unresolved}",
        )

    def test_a_symlinked_root_is_refused_because_it_is_not_its_own_resolution(self) -> None:
        """The subtle one: `link -> sandbox` is a directory, and still refused.

        Every path an agent supplies is compared against `root` after resolution,
        and resolution would land on the target rather than the link — so a
        symlinked root would refuse every legitimate path in the sandbox while
        looking perfectly well formed.
        """
        link = self.base / "link_root"
        link.symlink_to(self.root)
        self.assertTrue(link.is_dir())
        self.assertEqual(
            self.denied(lambda: SandboxReader(root=link)),
            f"Sandbox root must be resolved: {link}",
        )

    def test_a_root_that_is_a_file_or_absent_is_refused(self) -> None:
        for label, root in (
            ("file", self.root / MANIFEST_PATH),
            ("absent", self.base / "no-such-decode"),
        ):
            with self.subTest(root=label):
                self.assertEqual(
                    self.denied(lambda root=root: SandboxReader(root=root)),
                    f"Sandbox root is not a directory: {root}",
                )

    def test_every_cap_must_be_a_positive_integer(self) -> None:
        """`True` is refused too: `type(value) is not int` rather than `isinstance`.

        Right, because a cap of `True` is a cap of 1 by accident.
        """
        for name in ("max_read_bytes", "max_matches", "timeout_seconds"):
            for value in (0, -1, "200", 2.5, True, None):
                with self.subTest(cap=name, value=value):
                    self.assertEqual(
                        self.denied(
                            lambda name=name, value=value: SandboxReader(
                                root=self.root, **{name: value}
                            )
                        ),
                        f"Sandbox {name} must be a positive integer",
                    )

    def test_a_cap_of_one_is_accepted(self) -> None:
        """The positive control for the loop above: the bound is `< 1`, not `< 2`."""
        reader = SandboxReader(root=self.root, max_read_bytes=1, max_matches=1, timeout_seconds=1)
        self.assertEqual(reader.max_matches, 1)

    def test_only_three_of_the_seven_caps_are_per_reader(self) -> None:
        """A DELIBERATE WART: the caps are half configurable, and the split is not principled.

        Three are dataclass fields a caller can set per sandbox. Four are module
        constants that only a monkeypatch can move, and nothing about them is
        more universal than the other three — `MAX_MATCHES_PER_FILE` in
        particular is the sibling of `max_matches` and lives on the other side of
        the line. It is recorded rather than fixed because moving a constant onto
        the dataclass changes its default's meaning for every existing caller,
        and because the tests that need to vary a bound (`max_read_bytes`,
        `max_matches`, `timeout_seconds`) happen to be the three that are fields.

        The consequence is real and worth naming: the two bounds most likely to
        bite a large decode — 1000 directory entries and 20 matches per file —
        cannot be raised for one difficult hook without editing the module. This
        test is also the drift check: adding a field or a constant fails it and
        forces the question to be answered again.
        """
        fields = {field.name for field in dataclasses.fields(SandboxReader)}
        self.assertEqual(fields, {"root", "max_read_bytes", "max_matches", "timeout_seconds"})
        self.assertEqual(
            {
                name
                for name in vars(sandbox_tools)
                if name.isupper() and isinstance(getattr(sandbox_tools, name), int)
            },
            {
                "MAX_READ_BYTES",
                "DEFAULT_READ_LINES",
                "MAX_READ_LINES",
                "MAX_MATCHES",
                "MAX_MATCHES_PER_FILE",
                "SEARCH_TIMEOUT_SECONDS",
                "MAX_PATTERN",
                "MAX_ENTRIES",
            },
        )

    def test_the_reader_is_frozen(self) -> None:
        """Nobody widens the sandbox after construction by assigning to `root`."""
        with self.assertRaises(Exception) as raised:
            self.reader.root = self.base  # type: ignore[misc]
        self.assertIn("cannot assign to field", str(raised.exception))


# ------------------------------------------------------------------ containment


class ContainmentTests(SandboxTestCase):
    """THE stage-defining property. Every escape, against every tool.

    A hole here does not produce a failing run; it produces a run that scores
    better than it should and says nothing. The negative cases are therefore
    exhaustive and the positive controls are not optional.
    """

    # -- the escapes

    def test_dot_dot_at_every_depth_is_refused(self) -> None:
        for candidate in (
            "..",
            "../",
            "../..",
            "../answers",
            "../answers/hooks.json",
            "../../etc/passwd",
            "smali_classes4/../../answers/hooks.json",
            "smali_classes4/X/../../../answers",
            "./../answers",
            "smali_classes4/X/./../../../answers/hooks.json",
        ):
            with self.subTest(path=candidate):
                self.assert_every_tool_refuses(candidate, self.escape_message(candidate))

    def test_an_absolute_path_outside_the_sandbox_is_refused(self) -> None:
        for candidate in (
            str(self.answers),
            str(self.answers / "hooks.json"),
            "/etc/passwd",
            "/",
            "/proc/self/environ",
            str(self.base),
        ):
            with self.subTest(path=candidate):
                self.assert_every_tool_refuses(candidate, self.escape_message(candidate))

    def test_a_sibling_whose_name_extends_the_root_is_refused(self) -> None:
        """THE PREFIX TRAP. `/x/sandbox-answers` starts with `/x/sandbox`.

        Mutation: `str(resolved).startswith(str(self.root))`. Every other test in
        this class still passes and the answers directory next door is readable.
        The real check is `root in resolved.parents`, which is a path-component
        comparison rather than a string one.
        """
        self.assertTrue(str(self.prefix_trap).startswith(str(self.root)))
        for candidate in (
            "../sandbox-answers",
            "../sandbox-answers/hooks.json",
            str(self.prefix_trap),
            str(self.prefix_trap / "hooks.json"),
        ):
            with self.subTest(path=candidate):
                self.assert_every_tool_refuses(candidate, self.escape_message(candidate))

    def test_a_symlink_to_an_outside_directory_is_refused(self) -> None:
        """Mutation: `os.path.normpath`. It collapses `..` but never reads a link.

        The link is real and the target is readable — only the tool refuses.
        """
        self.assertTrue((self.root / "escape_dir").is_dir())
        for candidate in ("escape_dir", "escape_dir/hooks.json", str(self.root / "escape_dir")):
            with self.subTest(path=candidate):
                self.assert_every_tool_refuses(candidate, self.escape_message(candidate))

    def test_a_symlink_to_an_outside_file_is_refused(self) -> None:
        link = self.root / "escape_file"
        self.assertTrue(link.is_file(), "the link resolves to a real file; only the tool refuses")
        self.assertIn(ANSWER_MARKER, link.read_text(encoding="utf-8"))
        for candidate in ("escape_file", str(link)):
            with self.subTest(path=candidate):
                self.assert_every_tool_refuses(candidate, self.escape_message(candidate))

    def test_a_chain_of_symlinks_is_followed_to_the_end(self) -> None:
        """`chain -> chain_hop -> escape_file -> ../answers/hooks.json`.

        A containment check that read only one level of indirection would pass
        the first two hops and hand over the answers on the third.
        """
        self.assertEqual(
            (self.root / "chain").resolve(), (self.answers / "hooks.json").resolve()
        )
        self.assert_every_tool_refuses("chain", self.escape_message("chain"))
        self.assert_every_tool_refuses("chain_hop", self.escape_message("chain_hop"))

    def test_a_path_that_only_escapes_after_resolution_is_refused(self) -> None:
        """No component of this path is `..` and no prefix of it is outside."""
        candidate = "deep/../../../answers/hooks.json"
        self.assertTrue((self.root / "deep").is_dir())
        self.assert_every_tool_refuses(candidate, self.escape_message(candidate))

    # -- the positive controls, without which none of the above means anything

    def test_the_sandbox_root_itself_is_allowed(self) -> None:
        for candidate in (".", "./", str(self.root), f"{self.root}/", "smali_classes4/.."):
            with self.subTest(path=candidate):
                self.assertEqual(self.reader.resolve(candidate), self.root)
                self.assertIn(MANIFEST_PATH, self.reader.list_dir(candidate))

    def test_a_nested_path_is_allowed_relative_and_absolute(self) -> None:
        for candidate in (TC_PATH, f"./{TC_PATH}", str(self.root / TC_PATH)):
            with self.subTest(path=candidate):
                self.assertEqual(self.reader.resolve(candidate), self.root / TC_PATH)
                self.assertIn("LX/04tC;", self.reader.read_file(candidate))

    def test_a_symlink_that_stays_inside_is_allowed(self) -> None:
        self.assertIn("LX/04tC;", self.reader.read_file("inside_link"))
        self.assertIn("04tC.smali", self.reader.list_dir("deep"))

    def test_a_legitimate_path_through_an_inside_symlink_is_allowed(self) -> None:
        """The other half of the `normpath` mutation, and the half people forget.

        `deep -> smali_classes4/X`, so `deep/../..` is the sandbox root by way of
        the link's *target*. Textual collapsing would compute `sandbox/..` — the
        parent — and refuse a path that is perfectly legal. A containment check
        that is merely strict is not correct.
        """
        self.assertEqual(self.reader.resolve("deep/../.."), self.root)
        self.assertIn("<manifest", self.reader.read_file(f"deep/../../{MANIFEST_PATH}"))

    # -- malformed paths

    def test_an_empty_or_blank_path_is_refused(self) -> None:
        for candidate in ("", " ", "   ", "\t", "\n"):
            with self.subTest(path=repr(candidate)):
                self.assert_every_tool_refuses(candidate, "Path must be a non-empty string")

    def test_a_non_string_path_is_refused(self) -> None:
        """A `Path` is refused too: the tool surface speaks JSON, and only JSON."""
        for candidate in (None, 7, 1.5, True, b"AndroidManifest.xml", ["."], {"path": "."}):
            with self.subTest(path=repr(candidate)):
                self.assert_every_tool_refuses(candidate, "Path must be a non-empty string")
        self.assert_every_tool_refuses(self.root, "Path must be a non-empty string")

    def test_a_path_with_a_null_byte_is_refused(self) -> None:
        """Otherwise `Path()` raises `ValueError`, which no caller is catching."""
        for candidate in ("\x00", "AndroidManifest.xml\x00", "\x00/etc/passwd", "a\x00b"):
            with self.subTest(path=repr(candidate)):
                self.assert_every_tool_refuses(candidate, "Path must not contain a null byte")

    def test_a_missing_path_inside_the_sandbox_is_reported_as_missing_not_as_an_escape(
        self,
    ) -> None:
        """The two must stay distinguishable: one is a typo, the other is the run."""
        self.assertEqual(
            self.denied(lambda: self.reader.read_file("smali_classes4/X/0000.smali")),
            "Not a file: smali_classes4/X/0000.smali",
        )
        self.assertEqual(
            self.denied(lambda: self.reader.list_dir("smali_classes9")),
            "Not a directory: smali_classes9",
        )

    def test_a_directory_is_not_a_file_and_a_file_is_not_a_directory(self) -> None:
        self.assertEqual(
            self.denied(lambda: self.reader.read_file(".")), "Not a file: ."
        )
        self.assertEqual(
            self.denied(lambda: self.reader.read_file("smali_classes4")),
            "Not a file: smali_classes4",
        )
        self.assertEqual(
            self.denied(lambda: self.reader.list_dir(MANIFEST_PATH)),
            f"Not a directory: {MANIFEST_PATH}",
        )

    def test_a_dangling_symlink_inside_the_sandbox_is_not_an_escape(self) -> None:
        (self.root / "dangling").symlink_to("smali_classes4/X/nothing.smali")
        self.assertEqual(
            self.denied(lambda: self.reader.read_file("dangling")),
            "Not a file: smali_classes4/X/nothing.smali",
        )


class SearchRecursionContainmentTests(SandboxTestCase):
    """`grep -r`, never `-R`. One letter, and the whole holdout rests on it.

    `-R` follows symlinks encountered while recursing; `-r` skips them. The
    sandbox contains a link to the directory standing in for this repository, so
    a search from the root under `-R` walks into the answers without any path
    ever being supplied by the agent — the containment check in `resolve` is not
    even consulted.
    """

    def test_a_search_from_the_root_does_not_descend_through_a_symlink(self) -> None:
        outside = (self.answers / "hooks.json").read_text(encoding="utf-8")
        self.assertIn(ANSWER_MARKER, outside, "the marker really is out there to be found")

        self.assertEqual(
            self.reader.search(ANSWER_MARKER), f"no matches for {ANSWER_MARKER!r}"
        )
        self.assertEqual(
            self.reader.search(ANSWER_MARKER, include="*.json"),
            f"no matches for {ANSWER_MARKER!r}",
        )
        self.assertEqual(
            self.reader.search(ANSWER_MARKER, fixed=False),
            f"no matches for {ANSWER_MARKER!r}",
        )

        # POSITIVE CONTROL. Without it, "not found" is also what a search that
        # cannot succeed returns — a broken pattern, a wrong root, a grep that
        # never ran. The same string in a real file inside the sandbox is found.
        self.write("smali_classes4/X/decoy.smali", f'    const-string v0, "{ANSWER_MARKER}"\n')
        found = self.reader.search(ANSWER_MARKER)
        self.assertEqual(found, f'smali_classes4/X/decoy.smali:1:    const-string v0, "{ANSWER_MARKER}"')

    def test_the_outside_tree_is_reachable_by_grep_when_nothing_stops_it(self) -> None:
        """The second positive control: prove `-R` would have found it.

        Without this the previous test could pass because the fixture is wrong —
        an unreadable file, a link to nowhere, a marker that is not in the file.
        This runs the exact search the module would run with the one letter
        changed, and it finds the answers.
        """
        argv = [
            "grep",
            "-Rn",
            "--binary-files=without-match",
            "--no-messages",
            f"--max-count={MAX_MATCHES_PER_FILE}",
            "-F",
            "--",
            ANSWER_MARKER,
            str(self.root),
        ]
        result = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=60)
        self.assertEqual(result.returncode, 0, "-R must find what -r must not")
        self.assertIn("escape_dir/hooks.json", result.stdout)

    def test_a_symlinked_directory_given_as_the_path_is_refused_before_grep_runs(self) -> None:
        """The other way in: hand grep the link on the command line, where it *is* followed."""
        self.assertEqual(
            self.denied(lambda: self.reader.search(ANSWER_MARKER, "escape_dir")),
            self.escape_message("escape_dir"),
        )


# --------------------------------------------------------------------- list_dir


class ListDirTests(SandboxTestCase):
    def test_the_root_listing_marks_directories_and_symlinks(self) -> None:
        """`ls -F` style, and a link is `@` even when it points at a directory.

        Marked rather than hidden on purpose: the link is refused on use, and an
        agent that can see it is a link spends one turn on that instead of
        several on a directory that seems to exist and cannot be opened.
        """
        self.assertEqual(
            self.reader.list_dir("."),
            "\n".join(
                [
                    "AndroidManifest.xml",
                    "chain@",
                    "chain_hop@",
                    "deep@",
                    "escape_dir@",
                    "escape_file@",
                    "inside_link@",
                    "smali_classes12/",
                    "smali_classes4/",
                ]
            ),
        )

    def test_the_default_path_is_the_root(self) -> None:
        self.assertEqual(self.reader.list_dir(), self.reader.list_dir("."))

    def test_a_nested_listing_is_sorted_by_name(self) -> None:
        self.assertEqual(
            self.reader.list_dir("smali_classes4/X"), "04tC.smali\n05t2.smali"
        )

    def test_an_empty_directory_says_so(self) -> None:
        (self.root / "smali_classes7").mkdir()
        self.assertEqual(self.reader.list_dir("smali_classes7"), "(empty)")

    def test_a_crowded_directory_is_truncated_and_says_so(self) -> None:
        """A decode has directories with thousands of entries; silence would lie."""
        crowd = self.root / "crowd"
        crowd.mkdir()
        for index in range(MAX_ENTRIES + 1):
            (crowd / f"{index:05d}.smali").touch()

        listing = self.reader.list_dir("crowd").splitlines()
        self.assertEqual(len(listing), MAX_ENTRIES + 1)
        self.assertEqual(listing[0], "00000.smali")
        self.assertEqual(listing[MAX_ENTRIES - 1], f"{MAX_ENTRIES - 1:05d}.smali")
        self.assertEqual(listing[-1], f"... truncated at {MAX_ENTRIES} entries")

    def test_a_directory_at_the_limit_is_not_marked_truncated(self) -> None:
        """Positive control for the footer: it appears only when it bites."""
        crowd = self.root / "exact"
        crowd.mkdir()
        for index in range(MAX_ENTRIES):
            (crowd / f"{index:05d}.smali").touch()
        listing = self.reader.list_dir("exact").splitlines()
        self.assertEqual(len(listing), MAX_ENTRIES)
        self.assertNotIn("truncated", listing[-1])


# -------------------------------------------------------------------- read_file


class ReadFileTests(SandboxTestCase):
    """Numbered lines, and a partial answer that always announces itself."""

    def test_a_short_file_is_returned_whole_and_numbered(self) -> None:
        body = self.reader.read_file(AAA_PATH)
        lines = body.splitlines()
        self.assertEqual(len(lines), 11)
        self.assertEqual(lines[0], "     1  .class public final LX/0aaa;")
        self.assertEqual(lines[-1], "    11  .end method")
        self.assertNotIn("...", body)

    def test_the_numbers_are_the_file_s_own_line_numbers(self) -> None:
        """`evidence` must cite a line; an agent that counts them itself gets it wrong."""
        body = self.reader.read_file(TC_PATH, start_line=10, line_count=3)
        self.assertEqual(
            body,
            "\n".join(
                [
                    '    10      const-string v0, "feed/timeline_stream/"',
                    "    11" + "  ",
                    "    12      invoke-static {v0}, LX/05t2;->A01(Ljava/lang/String;)Z",
                    "... 7 more lines (file has 19)",
                ]
            ),
        )

    def test_a_file_longer_than_the_line_count_says_how_much_is_left(self) -> None:
        self.assertEqual(
            self.reader.read_file(TC_PATH, line_count=3),
            "\n".join(
                [
                    "     1  .class public final LX/04tC;",
                    "     2  .super Ljava/lang/Object;",
                    '     3  .source "ProfileActionBarHelper.java"',
                    "... 16 more lines (file has 19)",
                ]
            ),
        )

    def test_a_read_that_reaches_the_end_carries_no_footer(self) -> None:
        """Positive control: the footer means something only if it is absent sometimes."""
        self.assertEqual(
            self.reader.read_file(TC_PATH, start_line=18, line_count=2),
            "    18      return-void\n    19  .end method",
        )

    def test_a_start_line_past_the_end_reports_the_length(self) -> None:
        self.assertEqual(
            self.reader.read_file(TC_PATH, start_line=20),
            f"({TC_PATH} has 19 lines; none in the requested range)",
        )
        self.assertEqual(
            self.reader.read_file(TC_PATH, start_line=10_000),
            f"({TC_PATH} has 19 lines; none in the requested range)",
        )

    def test_an_empty_file_reports_zero_lines(self) -> None:
        self.write("smali_classes4/X/empty.smali", "")
        self.assertEqual(
            self.reader.read_file("smali_classes4/X/empty.smali"),
            "(smali_classes4/X/empty.smali has 0 lines; none in the requested range)",
        )

    def test_the_byte_cap_stops_the_read_and_names_the_line_to_resume_from(self) -> None:
        """The cap exists so one read cannot eat a context window."""
        self.write("smali_classes4/X/big.smali", "aa\n" + ("y" * 500 + "\n") * 4)
        reader = SandboxReader(root=self.root, max_read_bytes=100)
        self.assertEqual(
            reader.read_file("smali_classes4/X/big.smali"),
            "     1  aa\n... stopped at line 1 after 100 bytes; read again from there",
        )

    def test_a_line_longer_than_the_byte_cap_says_so_instead_of_reading_as_empty(
        self,
    ) -> None:
        """THE LAST SILENT TRUNCATION, and why the branch order is what it is.

        Two different things produce an empty collection, and they must not share
        a message. A range past the end of the file really is empty. A first line
        larger than `max_read_bytes` is not: it is a line that exists and was
        withheld. Reporting the second as the first told a proposer looking at a
        generated file that there was nothing in it — false twice over, since the
        file had four lines and the range was not empty but unreadable — and it
        was silent, which is the one thing this module's caps must never be.

        The byte-cap branch therefore runs first, and it names the line rather
        than a count: after a `break`, `total` is the line the read stopped at
        and not the length of the file, so the count in the other message would
        have been wrong as well.

        Mutation: remove the `stopped_on_bytes` branch, or move it below the
        empty-range return. The empty-range control below is what makes that
        detectable rather than a message nobody compares.
        """
        path = "smali_classes4/X/one_long_line.smali"
        self.write(path, ("z" * 500 + "\n") * 4)
        reader = SandboxReader(root=self.root, max_read_bytes=100)

        self.assertEqual(
            reader.read_file(path),
            f"(line 1 of {path} is longer than 100 bytes and was not returned; "
            "this is a generated or minified file)",
        )
        # The line named is the line asked for, not a hardcoded first line.
        self.assertEqual(
            reader.read_file(path, start_line=3),
            f"(line 3 of {path} is longer than 100 bytes and was not returned; "
            "this is a generated or minified file)",
        )
        # POSITIVE CONTROL, and the other half of the distinction: a range that
        # is genuinely empty still says so, and says nothing about bytes.
        self.assertEqual(
            reader.read_file(path, start_line=9),
            f"({path} has 4 lines; none in the requested range)",
        )

    def test_trailing_whitespace_is_stripped_but_indentation_is_kept(self) -> None:
        """Smali indentation is structure; a CRLF decode must not show as `^M`."""
        self.write("smali_classes4/X/crlf.smali", "    .locals 2   \r\n\tconst v0, 0x1\r\n")
        self.assertEqual(
            self.reader.read_file("smali_classes4/X/crlf.smali"),
            "     1      .locals 2\n     2  \tconst v0, 0x1",
        )

    def test_undecodable_bytes_are_replaced_rather_than_raising(self) -> None:
        """A decode contains binaries; a read of one must not crash the agent loop."""
        (self.root / "smali_classes4" / "X" / "raw.bin").write_bytes(b"\xff\xfe ok\n")
        self.assertIn("ok", self.reader.read_file("smali_classes4/X/raw.bin"))


class ReadFileArgumentTests(SandboxTestCase):
    """Arguments arrive from a model, so every one of them is hostile input."""

    def test_a_start_line_that_is_not_a_positive_integer_is_refused(self) -> None:
        for value in (0, -1, "1", 1.0, None, True, [1]):
            with self.subTest(start_line=value):
                self.assertEqual(
                    self.denied(lambda value=value: self.reader.read_file(TC_PATH, value)),
                    "start_line must be a positive integer",
                )

    def test_a_line_count_outside_the_bounds_is_refused(self) -> None:
        for value in (0, -1, MAX_READ_LINES + 1, 10_000, "200", 2.0, None, True):
            with self.subTest(line_count=value):
                self.assertEqual(
                    self.denied(
                        lambda value=value: self.reader.read_file(TC_PATH, 1, value)
                    ),
                    f"line_count must be between 1 and {MAX_READ_LINES}",
                )

    def test_the_bounds_themselves_are_accepted(self) -> None:
        self.assertEqual(
            self.reader.read_file(TC_PATH, 1, 1),
            "     1  .class public final LX/04tC;\n... 18 more lines (file has 19)",
        )
        self.assertEqual(len(self.reader.read_file(TC_PATH, 1, MAX_READ_LINES).splitlines()), 19)
        self.assertEqual(DEFAULT_READ_LINES, 200)

    def test_the_path_is_checked_before_the_numbers(self) -> None:
        """An escape attempt with a nonsense start_line is still reported as an escape."""
        self.assertEqual(
            self.denied(lambda: self.reader.read_file("../answers/hooks.json", 0, 0)),
            self.escape_message("../answers/hooks.json"),
        )


# ----------------------------------------------------------------------- search


class SearchTests(SandboxTestCase):
    """Literal by default, because a descriptor is not a regular expression."""

    def test_a_search_returns_file_line_and_text(self) -> None:
        """THE SMOKE TEST, and it exists for a specific past failure.

        `search` once referenced a module-level constant that did not exist. Every
        argument-validation test still passed, because they all return before the
        argv is built; only a search that actually runs catches a `NameError` in
        the body. At least one test in this suite must therefore assert a real
        successful result.
        """
        self.assertEqual(
            self.reader.search("feed/timeline_stream/"),
            f'{TC_PATH}:10:    const-string v0, "feed/timeline_stream/"',
        )

    def test_a_search_that_reads_to_the_end_is_never_reported_as_a_failure(self) -> None:
        """REGRESSION. One search in six used to refuse at random, and this is how.

        `_stream_matches` killed the child in its `finally` whenever `poll()`
        returned None. After the stdout iterator reaches EOF that is a race and
        not a condition: EOF means grep closed the pipe, and whether the parent
        has seen the exit status yet is a matter of scheduling. On the runs that
        lost, a grep which had finished perfectly was SIGKILLed, `returncode`
        became -9, and — nothing having stopped early — the -9 was read as an
        error, so the caller got `SandboxDenied('Search failed: ')` with
        `--no-messages` having eaten the one line that might have explained it.

        Measured at 7/60 and 12/60 on two runs of this fixture, matching and
        non-matching searches alike. Fifty iterations here, so a reintroduction
        shows up with probability better than 999 in 1000; the shim test in
        `GrepProcessLifecycleTests` catches it every time instead of nearly.

        The guard is `if stopped_early and process.poll() is None:` — kill only
        what was deliberately abandoned.
        """
        expected = f'{TC_PATH}:10:    const-string v0, "feed/timeline_stream/"'
        for attempt in range(50):
            with self.subTest(attempt=attempt):
                self.assertEqual(self.reader.search("feed/timeline_stream/"), expected)
                self.assertEqual(self.reader.search("LX/9999;"), "no matches for 'LX/9999;'")

    def test_a_literal_with_regex_metacharacters_matches_itself(self) -> None:
        """A smali descriptor is nothing but metacharacters."""
        self.assertEqual(
            self.match_files(self.reader.search("LX/04tC;")), sorted([TC_PATH, T2_PATH])
        )
        self.assertEqual(
            self.match_files(self.reader.search("LX/05t2;->A01(Ljava/lang/String;)Z")), [TC_PATH]
        )

    def test_the_default_really_is_literal(self) -> None:
        """`.` is a character, not a wildcard, until the caller says otherwise.

        Mutation: drop `-F`. `feed/timeline.stream/` then matches the const-string
        it was never meant to, and a proposer citing that line is citing a string
        the app does not contain.
        """
        self.assertEqual(
            self.reader.search("feed/timeline.stream/"),
            "no matches for 'feed/timeline.stream/'",
        )
        self.assertEqual(
            self.reader.search("const-string.*timeline"), "no matches for 'const-string.*timeline'"
        )
        # And the same patterns, as regexes, do match: the fixture is not the reason.
        self.assertIn(TC_PATH, self.reader.search("feed/timeline.stream/", fixed=False))
        self.assertIn(TC_PATH, self.reader.search("const-string.*timeline", fixed=False))

    def test_a_regex_is_run_as_an_extended_expression_so_validation_means_something(
        self,
    ) -> None:
        """`-E`, because the pattern is validated by Python and executed by grep.

        Without it grep reads the pattern as a POSIX *basic* expression, in which
        `(`, `)`, `|` and `+` are literal characters — so
        `invoke-(static|virtual)` passed `re.compile`, ran, and returned "no
        matches". A false negative shaped exactly like an answer, in the one tool
        a proposer uses to decide whether a literal is unique. The two engines
        now agree on everything a proposer is likely to reach for.

        They still are not the same dialect, and that is why `fixed=True` remains
        the default rather than being a convenience: ERE has no `\\d`, no `\\b`,
        no lookaround. `test_perl_only_syntax_is_not_an_extended_expression`
        below pins that boundary, so nobody reads this test as a promise that
        Python's regex language works here.

        Mutation: drop the `-E`. Both assertions below return "no matches", which
        is what makes this a test rather than a demonstration.
        """
        self.assertEqual(
            self.match_files(self.reader.search("invoke-(static|virtual)", fixed=False)),
            sorted([TC_PATH, T2_PATH]),
        )
        self.assertEqual(
            self.match_files(self.reader.search("invoke-stat+ic", fixed=False)),
            sorted([TC_PATH, T2_PATH]),
        )
        # Anchors, classes and quantifiers, on the descriptors this exists for.
        self.assertEqual(
            self.match_files(self.reader.search("^\\.class .*LX/0(4tC|5t2);$", fixed=False)),
            sorted([TC_PATH, T2_PATH]),
        )
        self.assertIn(TC_PATH, self.reader.search("invoke-.*A01", fixed=False))
        # And the BRE spelling is now the one that does not work, which is the
        # sharpest evidence that the dialect actually changed.
        self.assertEqual(
            self.reader.search(r"invoke-\(static\|virtual\)", fixed=False),
            r"no matches for 'invoke-\\(static\\|virtual\\)'",
        )

    def test_perl_only_syntax_is_still_not_an_extended_expression(self) -> None:
        """The boundary `-E` does NOT move, measured rather than assumed.

        A DELIBERATE WART, and the reason `fixed=True` stays the default. Python
        accepts all four patterns below; grep implements none of them and says so
        only on stderr, which `--no-messages` suppresses — so a lookahead comes
        back as a clean "no matches" that reads like a finding. `-P` is not
        compiled into every grep, and a proposer searching for a smali descriptor
        wants a literal anyway, so the gap is accepted rather than closed.

        The half that does work is asserted too, because the dialect is narrower
        than Python and wider than POSIX: GNU implements `\\w` and `\\b` as
        extensions. Without that half, someone reading only the failures would
        "fix" this test by assuming the whole escape set is missing.
        """
        for pattern in (r"invoke-\d", r"invoke-static\Z", "invoke-(?=static)", "(?i)INVOKE-STATIC"):
            with self.subTest(unsupported=pattern):
                re.compile(pattern)  # Python is happy with every one of them.
                self.assertEqual(
                    self.reader.search(pattern, fixed=False), f"no matches for {pattern!r}"
                )
        for pattern in (r"\binvoke-static\b", r"invoke-\w+ \{v0\}", "invoke-s{1,2}tatic"):
            with self.subTest(supported=pattern):
                self.assertIn(TC_PATH, self.reader.search(pattern, fixed=False))
        # And the POSIX spelling of the Perl class above, which is the advice.
        self.assertIn(TC_PATH, self.reader.search(r"invoke-[a-z]+ \{v[0-9]\}", fixed=False))

    def test_a_pattern_beginning_with_a_dash_is_a_pattern(self) -> None:
        """Mutation: drop the `--`. `grep` then reads `->A01` as a bundle of flags
        and exits 2, so every search for a method reference fails with
        "Search failed: grep: invalid option".
        """
        self.assertEqual(
            self.reader.search("->A01"),
            f"{TC_PATH}:12:    invoke-static {{v0}}, LX/05t2;->A01(Ljava/lang/String;)Z",
        )
        self.assertEqual(self.reader.search("-.locals"), "no matches for '-.locals'")

    def test_the_include_glob_narrows_by_filename(self) -> None:
        self.assertEqual(
            self.match_files(self.reader.search("instagram")), sorted([MANIFEST_PATH, T2_PATH])
        )
        self.assertEqual(
            self.match_files(self.reader.search("instagram", include="*.smali")), [T2_PATH]
        )
        self.assertEqual(
            self.match_files(self.reader.search("instagram", include="*.xml")), [MANIFEST_PATH]
        )
        self.assertEqual(
            self.reader.search("instagram", include="*.json"), "no matches for 'instagram'"
        )

    def test_the_path_narrows_the_search(self) -> None:
        self.assertEqual(
            self.match_files(self.reader.search("LX/0aaa;", "smali_classes12")), [AAA_PATH]
        )
        self.assertEqual(
            self.match_files(self.reader.search("LX/0aaa;")), sorted([AAA_PATH, TC_PATH])
        )

    def test_results_are_relative_to_the_sandbox_root(self) -> None:
        """An absolute path in a result is the root leaking into the transcript."""
        output = self.reader.search("class public final")
        self.assertNotIn(str(self.root), output)
        self.assertNotIn(str(self.base), output)
        self.assertEqual(self.match_files(output), sorted([AAA_PATH, TC_PATH, T2_PATH]))

    def test_a_search_of_a_single_file_still_names_the_file(self) -> None:
        """`-H` unconditionally, so the result shape never depends on the argument.

        Left to itself grep prints no filename when handed exactly one file, and
        the tool's own description ("returning 'file:line:text'") would become
        false at precisely the moment a proposer narrows onto one class. One
        shape, whatever `path` was, so `evidence` can be parsed without knowing
        what was asked.

        Mutation: `-rn` instead of `-rnH`. The directory searches elsewhere in
        this class all still pass, because grep volunteers the name once there is
        more than one file — which is why this needs its own test.
        """
        self.assertEqual(
            self.reader.search("const-string", TC_PATH),
            f'{TC_PATH}:10:    const-string v0, "feed/timeline_stream/"',
        )
        # A directory holding exactly one matching file is the same shape.
        self.assertEqual(
            self.reader.search("LX/0aaa;", "smali_classes12"),
            f"{AAA_PATH}:1:.class public final LX/0aaa;",
        )

    def test_no_matches_is_an_answer_rather_than_a_failure(self) -> None:
        self.assertEqual(
            self.reader.search("LX/9999;"), "no matches for 'LX/9999;'"
        )

    def test_a_missing_path_is_named_rather_than_reported_as_a_bare_failure(self) -> None:
        """A typo must read as a typo, and `--no-messages` is why it did not.

        grep exits 2 for a path that does not exist and its stderr is suppressed,
        so the agent used to be told `Search failed: ` and nothing else — a dead
        end where the truth was one misspelled directory. The existence check
        runs before grep and names the path, relative to the root like every
        other message here.

        Mutation: remove the `target.exists()` check. This returns to the bare
        `Search failed: `, which no proposer can act on.
        """
        self.assertEqual(
            self.denied(lambda: self.reader.search("const-string", "smali_classes9")),
            "No such path in the sandbox: smali_classes9",
        )
        self.assertEqual(
            self.denied(lambda: self.reader.search("const-string", "smali_classes4/X/0000.smali")),
            "No such path in the sandbox: smali_classes4/X/0000.smali",
        )
        # A path outside is still the *escape* message: the two must not merge,
        # since one is a typo and the other is the whole point of the stage.
        self.assertEqual(
            self.denied(lambda: self.reader.search("const-string", "../answers")),
            self.escape_message("../answers"),
        )
        # A dangling symlink is a missing path, named by where it pointed.
        (self.root / "dangling").symlink_to("smali_classes4/X/nothing.smali")
        self.assertEqual(
            self.denied(lambda: self.reader.search("const-string", "dangling")),
            "No such path in the sandbox: smali_classes4/X/nothing.smali",
        )

    def test_a_binary_file_is_skipped_rather_than_dumped(self) -> None:
        (self.root / "resources.arsc").write_bytes(b"\x00\x01const-string\x00\x02")
        self.assertNotIn("resources.arsc", self.reader.search("const-string"))


class SearchArgumentTests(SandboxTestCase):
    def test_an_empty_or_non_string_pattern_is_refused(self) -> None:
        for value in ("", None, 7, b"const", ["const"], True):
            with self.subTest(pattern=repr(value)):
                self.assertEqual(
                    self.denied(lambda value=value: self.reader.search(value)),
                    "Search pattern must be a non-empty string",
                )

    def test_an_over_long_pattern_is_refused_and_the_bound_itself_is_not(self) -> None:
        self.assertEqual(
            self.denied(lambda: self.reader.search("x" * (MAX_PATTERN + 1))),
            f"Search pattern is longer than {MAX_PATTERN} characters",
        )
        self.assertEqual(
            self.reader.search("x" * MAX_PATTERN), f"no matches for {'x' * MAX_PATTERN!r}"
        )

    def test_a_pattern_with_a_null_byte_is_refused(self) -> None:
        """It cannot survive the argv, and truncating it silently would search
        for something other than what was asked."""
        self.assertEqual(
            self.denied(lambda: self.reader.search("const\x00string")),
            "Search pattern must not contain a null byte",
        )

    def test_an_invalid_regex_is_refused_before_grep_runs(self) -> None:
        for pattern in ("LX/[04tC", "(unclosed", "a{2,1}", "*leading"):
            with self.subTest(pattern=pattern):
                try:
                    re.compile(pattern)
                except re.error as error:
                    expected = f"Invalid regular expression: {error}"
                else:  # pragma: no cover - the fixture would be wrong
                    self.fail(f"{pattern!r} is a valid regex")
                self.assertEqual(
                    self.denied(lambda pattern=pattern: self.reader.search(pattern, fixed=False)),
                    expected,
                )

    def test_the_same_broken_regex_is_a_fine_literal(self) -> None:
        """Positive control: validation is attached to `fixed=False`, not to the pattern."""
        self.write("smali_classes4/X/odd.smali", "const-string v0, \"LX/[04tC\"\n")
        self.assertIn("odd.smali", self.reader.search("LX/[04tC"))

    def test_fixed_must_be_a_boolean(self) -> None:
        """`fixed=1` would work by accident and `fixed="false"` would be *true*."""
        for value in (1, 0, "true", "false", None, [], "yes"):
            with self.subTest(fixed=repr(value)):
                self.assertEqual(
                    self.denied(lambda value=value: self.reader.search("const", fixed=value)),
                    "fixed must be a boolean",
                )

    def test_an_empty_or_non_string_include_is_refused(self) -> None:
        for value in ("", 7, [], "*.sm\x00ali", True):
            with self.subTest(include=repr(value)):
                self.assertEqual(
                    self.denied(lambda value=value: self.reader.search("const", include=value)),
                    "include must be a non-empty string",
                )

    def test_include_none_means_no_filter(self) -> None:
        self.assertEqual(
            self.match_files(self.reader.search("instagram", include=None)),
            sorted([MANIFEST_PATH, T2_PATH]),
        )


class SearchCapTests(SandboxTestCase):
    """Two bounds, one of them per file, and both of them reported when they bite.

    A capped list that reads as complete is the failure this whole module is
    shaped around: it is how a proposer concludes a literal appears in one class
    when it appears in five, and nothing downstream can tell the difference.
    """

    #: A descriptor that appears nowhere in the base fixture, so every count in
    #: this class is exactly the number of lines these files were given.
    BULK = "LX/0bul;"

    def fill(self, name: str, matches: int) -> None:
        """One file with `matches` matching lines and some noise between them."""
        body = "".join(
            f"    invoke-static {{v0}}, {self.BULK}->A{index:03d}()V\n\n"
            for index in range(matches)
        )
        self.write(f"smali_classes4/X/{name}.smali", body)

    def test_the_per_file_cap_spreads_the_budget_across_files(self) -> None:
        """`--max-count` is per file, and that is the point rather than an accident.

        "Which classes hold this literal" is the question a proposer is asking, so
        one loop-heavy class must not crowd out the other nine.

        Mutation: `--max-count={self.max_matches}`, which is what the argv said
        before the cap was split in two. Each file then contributes 50 and the
        result is 150 lines from three files instead of 60 from three.
        """
        self.assertEqual(MAX_MATCHES_PER_FILE, 20)
        for name in ("aaa", "bbb", "ccc"):
            self.fill(name, 50)

        output = self.reader.search(self.BULK)
        lines = output.splitlines()
        self.assertNotIn("TRUNCATED", output, "60 is under the overall cap of 200")
        self.assertEqual(len(lines), 3 * MAX_MATCHES_PER_FILE)
        counted = {name: 0 for name in self.match_files(output)}
        for line in lines:
            counted[line.split(":", 1)[0]] += 1
        self.assertEqual(
            counted,
            {
                "smali_classes4/X/aaa.smali": MAX_MATCHES_PER_FILE,
                "smali_classes4/X/bbb.smali": MAX_MATCHES_PER_FILE,
                "smali_classes4/X/ccc.smali": MAX_MATCHES_PER_FILE,
            },
        )

    def test_a_file_under_the_per_file_cap_is_reported_whole(self) -> None:
        """Positive control: the cap trims, it does not round."""
        self.fill("few", 5)
        self.assertEqual(
            len(self.reader.search(self.BULK, "smali_classes4/X/few.smali").splitlines()), 5
        )

    def test_a_result_over_the_overall_cap_is_trimmed_and_says_so(self) -> None:
        reader = SandboxReader(root=self.root, max_matches=30)
        for name in ("aaa", "bbb", "ccc"):
            self.fill(name, 50)

        output = reader.search(self.BULK)
        lines = output.splitlines()
        self.assertEqual(len(lines), 31)
        self.assertEqual(
            lines[-1],
            "... more than 30 matches; this list is TRUNCATED and you must not "
            "conclude anything is unique from it",
        )
        for line in lines[:-1]:
            self.assertRegex(line, r"^smali_classes4/X/\w+\.smali:\d+:")

    def test_the_real_default_cap_is_two_hundred_and_it_bites(self) -> None:
        """Bound to `MAX_MATCHES` rather than to a test-only reader.

        Mutation: raise the cap in the module and this fails on the count; return
        `truncated=False` and it fails on the footer.
        """
        self.assertEqual(MAX_MATCHES, 200)
        for index in range(11):
            self.fill(f"bulk{index:02d}", 25)

        output = self.reader.search(self.BULK)
        lines = output.splitlines()
        self.assertEqual(len(lines), MAX_MATCHES + 1)
        self.assertEqual(
            lines[-1],
            f"... more than {MAX_MATCHES} matches; this list is TRUNCATED and you must "
            "not conclude anything is unique from it",
        )
        # The point of the per-file cap: 200 results, spread over many files.
        self.assertGreaterEqual(len(self.match_files(output)), 10)

    def test_a_result_exactly_at_the_cap_is_still_announced_as_truncated(self) -> None:
        """A DELIBERATE WART: the warning over-fires, always in the safe direction.

        The loop stops the moment it holds `max_matches` lines, so it cannot
        distinguish "exactly the cap" from "the cap and more" and says TRUNCATED
        for both. Finding out would mean reading one line further, which means
        keeping a 202 MB pipe open to learn something that changes nothing: a
        proposer told its list may be incomplete re-searches more narrowly and
        loses a turn, where a proposer wrongly told its list is complete
        concludes a literal is unique and loses the proposal. The asymmetry is
        the whole argument, and it points one way.
        """
        reader = SandboxReader(root=self.root, max_matches=30)
        self.fill("aaa", 25)  # trimmed to 20 by the per-file cap
        self.fill("bbb", 10)  # exactly 30 in total, and no more exist
        output = reader.search(self.BULK)
        self.assertEqual(len(output.splitlines()), 31)
        self.assertIn("more than 30 matches", output)

    def test_a_result_one_under_the_cap_carries_no_footer(self) -> None:
        """Positive control for the footer: it is not simply always appended."""
        reader = SandboxReader(root=self.root, max_matches=30)
        self.fill("aaa", 20)
        self.fill("bbb", 9)
        output = reader.search(self.BULK)
        self.assertEqual(len(output.splitlines()), 29)
        self.assertNotIn("TRUNCATED", output)

    def test_repeated_truncated_searches_return_promptly_and_leave_no_children(self) -> None:
        """Killing grep must reap it. An unreaped child is a leaked pipe and a zombie.

        The assertion is per-PID, and deliberately not `os.waitpid(-1, WNOHANG)`.
        That form asks whether *this process* has any child at all, which is a
        stronger claim than intended and is not this test's to make: run under
        the full suite, a Temporal test server or a `multiprocessing` worker
        started elsewhere is also a child, so `-1` reports someone else's process
        and the test fails for a reason that has nothing to do with grep. It
        passed alone and failed in the suite, which is the signature of a test
        asserting something process-global from inside a shared process.

        Naming the PIDs keeps the real property -- every grep this search started
        was reaped -- while making the test independent of what else is running.
        """
        reader = SandboxReader(root=self.root, max_matches=30)
        for name in ("aaa", "bbb", "ccc"):
            self.fill(name, 50)

        pids: list[int] = []
        real_popen = subprocess.Popen

        def recording_popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            pids.append(process.pid)
            return process

        with mock.patch.object(sandbox_tools.subprocess, "Popen", recording_popen):
            started = time.monotonic()
            for _ in range(8):
                self.assertIn("TRUNCATED", reader.search(self.BULK))
            self.assertLess(time.monotonic() - started, 20, "a blocked pipe would stall here")

        self.assertEqual(len(pids), 8, "each search must start exactly one grep")
        for pid in pids:
            with self.assertRaises(ChildProcessError, msg=f"grep {pid} was never reaped"):
                os.waitpid(pid, os.WNOHANG)


class GrepProcessLifecycleTests(SandboxTestCase):
    """The parts of `_stream_matches` that real `grep` is too fast to exercise.

    Killing grep early and timing it out are both about *when* the child stops,
    and neither is observable through a fixture of a few files: real grep finishes
    before the difference exists. These tests put a `grep` shim first on `PATH`
    that produces output on a schedule the test chooses, which is the only way to
    make either behaviour deterministic rather than a sleep-and-hope.

    The shim is not a stand-in for grep's semantics — every other search test
    above runs the real binary. It stands in only for its *timing*.

    That the shim works at all is A DELIBERATE WART worth stating: the module
    runs `subprocess.Popen(["grep", ...])`, so the binary is whatever `PATH`
    resolves, and anyone who can set this process's environment chooses what
    "grep" means. It is not tightened to `/usr/bin/grep` because that is not
    where grep lives everywhere, and because it would buy nothing under the
    module's stated threat model — a model wandering, not a same-uid attacker,
    who by then has easier routes than an environment variable. Tests depending
    on it is the honest evidence that it is true.
    """

    def install_grep(self, script: str) -> None:
        binaries = self.base / "bin"
        binaries.mkdir(exist_ok=True)
        shim = binaries / "grep"
        shim.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
        shim.chmod(0o700)
        previous = os.environ.get("PATH", "")
        self.addCleanup(os.environ.__setitem__, "PATH", previous)
        os.environ["PATH"] = f"{binaries}{os.pathsep}{previous}"

    def test_reaching_the_cap_kills_grep_rather_than_waiting_for_it(self) -> None:
        """Mutation: drop `process.kill()` from the `finally`.

        The result is byte-identical, so only the child's fate distinguishes the
        two. The shim records whether it was allowed to finish; on the real decode
        the difference is 202 MB read to return 200 lines.
        """
        marker = self.base / "grep-ran-to-completion"
        # Both pipes are closed before the sleep, so that what is measured is
        # whether the child was *killed* rather than whether some grandchild still
        # holds a write end. Without this the test has a race of its own: on the
        # runs where the shim gets far enough to spawn `sleep`, that grandchild
        # inherits stdout and stderr, and the drain in `communicate` waits the
        # full five seconds even though grep itself was killed on time. The
        # marker below is the load-immune half of the assertion; the clock is a
        # cross-check.
        self.install_grep(
            "i=0\n"
            "while [ $i -lt 300 ]; do echo \"f$i.smali:1:invoke-static\"; i=$((i+1)); done\n"
            "exec 1>&- 2>&-\n"
            "sleep 5\n"
            f"touch {marker}\n"
        )
        reader = SandboxReader(root=self.root, max_matches=30)

        started = time.monotonic()
        output = reader.search("invoke-static")
        elapsed = time.monotonic() - started

        self.assertEqual(len(output.splitlines()), 31)
        self.assertIn("TRUNCATED", output)
        self.assertLess(elapsed, 2.5, "the cap was reached; there is nothing left to wait for")
        self.assertFalse(marker.exists(), "grep outlived the read it was serving")

    def test_a_grep_that_closes_stdout_before_exiting_is_not_killed(self) -> None:
        """The deterministic form of the race above, with the timing chosen rather
        than left to the scheduler.

        Real grep closes stdout and exits in the same breath, so the window is
        whatever the kernel makes it and the bug appears as one search in six.
        This shim widens it to a second and makes it certain: it closes stdout,
        then lingers. A `finally` that kills whatever `poll()` reports as running
        kills a grep that has already produced its complete answer and turns it
        into `Search failed: `. Mutation: drop the `stopped_early and` guard.
        """
        self.install_grep(
            'echo "f.smali:1:invoke-static"\nexec 1>&-\nsleep 1\nexit 0'
        )
        self.assertEqual(
            self.reader.search("invoke-static"), "f.smali:1:invoke-static"
        )

    def test_a_search_that_keeps_producing_past_the_timeout_is_refused(self) -> None:
        """A grep that is answering, slowly, is still bounded by the timeout.

        Forty lines and then an exit, rather than an endless loop: a test whose
        failure mode is a hung suite is not a test. If the bound were ever removed
        this fails in eight seconds on "SandboxDenied not raised" rather than
        never returning.
        """
        self.install_grep(
            "i=0\n"
            'while [ $i -lt 40 ]; do echo "f.smali:1:invoke-static"; sleep 0.2; i=$((i+1)); done'
        )
        reader = SandboxReader(root=self.root, max_matches=10_000, timeout_seconds=1)

        started = time.monotonic()
        message = self.denied(lambda: reader.search("invoke-static"))
        elapsed = time.monotonic() - started

        self.assertEqual(
            message,
            "Search for 'invoke-static' exceeded 1s; narrow it with `path` or `include`",
        )
        self.assertLess(elapsed, 4, "the timeout fired rather than the shim finishing on its own")

    def test_a_grep_that_produces_no_output_is_timed_out_too(self) -> None:
        """THE CASE THE OLD DEADLINE COULD NOT SEE, and the reason it is a watchdog.

        A deadline checked inside `for line in process.stdout` bounds the gap
        between results, not the call: a grep that is working hard and emitting
        nothing — a pattern matching nowhere across 1.7 GiB, an unreadable mount —
        never enters the loop, so the check never runs and the search is
        unbounded. That is the shape of search most likely to be slow, so the one
        the bound existed for was the one it missed.

        A `threading.Timer` fires whether or not anything was read. The shim
        `exec`s `sleep`, deliberately: a forked grandchild would inherit the
        pipes and hold `communicate` open for the shim's full lifetime, so the
        call would take three seconds even with the watchdog working and this
        test would measure the fixture instead of the module.

        Mutations: remove the `expired.is_set()` check and the killed grep
        reports `Search failed: ` instead; remove the watchdog and this waits the
        shim out and returns "no matches".
        """
        self.install_grep("exec sleep 3")
        reader = SandboxReader(root=self.root, timeout_seconds=1)

        started = time.monotonic()
        message = self.denied(lambda: reader.search("invoke-static"))
        elapsed = time.monotonic() - started

        self.assertEqual(
            message,
            "Search for 'invoke-static' exceeded 1s; narrow it with `path` or `include`",
        )
        self.assertLess(elapsed, 2.5, "the watchdog fired at 1s, not when the shim ended")

        # POSITIVE CONTROL. A watchdog that always fires, or an `expired` that is
        # never cleared between calls, would pass everything above.
        self.install_grep("exec sleep 0.2")
        self.assertEqual(
            reader.search("invoke-static"), "no matches for 'invoke-static'"
        )

    def test_a_grep_that_fails_is_reported_with_its_own_words(self) -> None:
        self.install_grep('echo "grep: something went wrong" >&2\nexit 2')
        self.assertEqual(
            self.denied(lambda: self.reader.search("invoke-static")),
            "Search failed: grep: something went wrong",
        )

    def test_a_failure_message_is_bounded(self) -> None:
        """A tool result goes into a context window; an unbounded one is a hazard."""
        self.install_grep('head -c 5000 /dev/zero | tr "\\0" "x" >&2\nexit 2')
        message = self.denied(lambda: self.reader.search("invoke-static"))
        self.assertEqual(message, "Search failed: " + "x" * 200)


# --------------------------------------------------------------------- dispatch


class DispatchTests(SandboxTestCase):
    """Three verbs. The fourth does not exist, and asking for it is an error."""

    def test_each_tool_is_reachable_by_name(self) -> None:
        self.assertEqual(
            self.reader.dispatch("list_dir", {"path": "smali_classes4/X"}),
            self.reader.list_dir("smali_classes4/X"),
        )
        self.assertEqual(
            self.reader.dispatch("read_file", {"path": TC_PATH, "start_line": 10, "line_count": 1}),
            '    10      const-string v0, "feed/timeline_stream/"'
            "\n... 9 more lines (file has 19)",
        )
        self.assertEqual(
            self.reader.dispatch(
                "search", {"pattern": "instagram", "include": "*.xml", "fixed": True}
            ),
            self.reader.search("instagram", include="*.xml"),
        )

    def test_the_optional_arguments_may_be_omitted(self) -> None:
        self.assertEqual(self.reader.dispatch("list_dir", {}), self.reader.list_dir())
        self.assertEqual(
            self.reader.dispatch("search", {"pattern": "LX/0aaa;"}),
            self.reader.search("LX/0aaa;"),
        )

    def test_no_write_tool_exists_under_any_name_a_model_might_try(self) -> None:
        """The sandbox is hardlinked to the master decode: a write is corruption.

        There is no path to guard because there is no writer. The message names
        the three real tools so a model that guessed spends one turn on it.
        """
        for name in (
            "write_file",
            "write",
            "create_file",
            "edit_file",
            "edit",
            "str_replace",
            "apply_patch",
            "delete",
            "rm",
            "move",
            "copy",
            "mkdir",
            "chmod",
            "bash",
            "shell",
            "sh",
            "run",
            "run_command",
            "exec",
            "python",
            "execute_code",
            "glob",
            "grep",
            "find",
            "cat",
            "ls",
            "open",
            "fetch",
            "web_search",
            "todo_write",
        ):
            with self.subTest(tool=name):
                self.assertEqual(
                    self.denied(lambda name=name: self.reader.dispatch(name, {})),
                    f"Unknown tool {name!r}. This sandbox is read-only and offers "
                    "exactly: list_dir, read_file, search.",
                )

    def test_the_names_are_matched_exactly(self) -> None:
        for name in ("List_dir", "READ_FILE", " search", "search ", "read_file()", "search\n", ""):
            with self.subTest(tool=name):
                self.assertIn(
                    "Unknown tool",
                    self.denied(lambda name=name: self.reader.dispatch(name, {})),
                )

    def test_a_non_string_tool_name_is_refused(self) -> None:
        for name in (None, 7, ["search"], b"search", {"name": "search"}, True):
            with self.subTest(name=repr(name)):
                self.assertEqual(
                    self.denied(lambda name=name: self.reader.dispatch(name, {})),
                    "Tool name must be a string",
                )

    def test_non_mapping_arguments_are_refused(self) -> None:
        for arguments in (None, [], ["path", "."], "path=.", 7, ("path", "."), {"a"}):
            with self.subTest(arguments=repr(arguments)):
                self.assertEqual(
                    self.denied(
                        lambda arguments=arguments: self.reader.dispatch("list_dir", arguments)
                    ),
                    "Tool arguments must be an object",
                )

    def test_any_mapping_is_accepted_not_only_a_dict(self) -> None:
        """A runtime may hand over a read-only view of the decoded JSON."""
        arguments = types.MappingProxyType({"path": "smali_classes4/X"})
        self.assertEqual(self.reader.dispatch("list_dir", arguments), "04tC.smali\n05t2.smali")

    def test_an_unknown_argument_is_refused_rather_than_dropped(self) -> None:
        """A model passing `recursive=True` believes it got a recursive listing."""
        for name, arguments, unexpected in (
            ("list_dir", {"path": ".", "recursive": True}, "recursive"),
            ("read_file", {"path": TC_PATH, "encoding": "utf-8"}, "encoding"),
            ("read_file", {"path": TC_PATH, "end_line": 4}, "end_line"),
            ("search", {"pattern": "x", "regex": True}, "regex"),
            ("search", {"pattern": "x", "case_sensitive": False}, "case_sensitive"),
            ("list_dir", {"content": "x", "path": "."}, "content"),
        ):
            with self.subTest(tool=name, argument=unexpected):
                self.assertEqual(
                    self.denied(
                        lambda name=name, arguments=arguments: self.reader.dispatch(
                            name, arguments
                        )
                    ),
                    f"Unknown argument for {name}: {unexpected}",
                )

    def test_the_first_unknown_argument_by_name_is_the_one_reported(self) -> None:
        self.assertEqual(
            self.denied(lambda: self.reader.dispatch("list_dir", {"zeta": 1, "alpha": 2})),
            "Unknown argument for list_dir: alpha",
        )

    def test_a_missing_required_argument_is_refused_in_the_same_channel_as_the_rest(
        self,
    ) -> None:
        """Every refusal a model can provoke arrives as `SandboxDenied`, including this one.

        The schema marks `path` and `pattern` required and nothing enforces that
        before the call, so a model that omits one used to get a `TypeError` out
        of the `**` expansion. A runtime catches `SandboxDenied` in order to hand
        the refusal back to the model and carry on; one omitted argument would
        instead have ended the run. Which exception type this is decides whether
        a typo costs a turn or a whole proposal.

        The conversion wraps the whole call rather than pre-checking the required
        names, so a `TypeError` raised *inside* a handler would also be reported
        as an argument error. Nothing can currently do that — every handler
        validates its own arguments and raises `SandboxDenied` first — but it is
        the reason `__cause__` is asserted below: the original is preserved for
        whoever has to debug it.

        Mutation: drop the `try`/`except`. Both subtests then fail on the
        exception type rather than on the message.
        """
        for name, argument in (("read_file", "path"), ("search", "pattern")):
            with self.subTest(tool=name):
                with self.assertRaises(SandboxDenied) as raised:
                    self.reader.dispatch(name, {})
                self.assertEqual(
                    str(raised.exception),
                    f"Invalid arguments for {name}: SandboxReader.{name}() missing 1 "
                    f"required positional argument: '{argument}'",
                )
                self.assertIsInstance(raised.exception.__cause__, TypeError)

        # The two argument channels stay distinct: a *wrong* name is still named
        # as unknown rather than collapsing into the message above.
        self.assertEqual(
            self.denied(lambda: self.reader.dispatch("read_file", {"file": TC_PATH})),
            "Unknown argument for read_file: file",
        )

    def test_dispatch_enforces_containment_exactly_as_the_methods_do(self) -> None:
        """It is the entry point a runtime uses, so it is the one that must hold."""
        for name, arguments in (
            ("list_dir", {"path": "../answers"}),
            ("read_file", {"path": "../answers/hooks.json"}),
            ("read_file", {"path": "escape_file"}),
            ("search", {"pattern": ANSWER_MARKER, "path": "escape_dir"}),
            ("search", {"pattern": ANSWER_MARKER, "path": "/etc"}),
        ):
            with self.subTest(tool=name, path=arguments["path"]):
                self.assertEqual(
                    self.denied(
                        lambda name=name, arguments=arguments: self.reader.dispatch(
                            name, arguments
                        )
                    ),
                    self.escape_message(arguments["path"]),
                )


# --------------------------------------------------------------- specifications


class ToolSpecificationTests(unittest.TestCase):
    """What a runtime advertises must be what the sandbox will actually do."""

    def setUp(self) -> None:
        self.specifications = tool_specifications()

    def test_exactly_three_tools_are_offered(self) -> None:
        self.assertEqual(
            [tool["name"] for tool in self.specifications], ["list_dir", "read_file", "search"]
        )

    def test_the_advertised_tools_are_exactly_the_dispatchable_ones(self) -> None:
        """Drift here is a model told it can do something the sandbox refuses.

        Mutation: add a fourth specification without a handler. Every schema is
        still valid JSON and the tool 404s at the first call.

        Both directions are covered. Advertised-implies-dispatchable is the loop
        below; dispatchable-implies-advertised falls out of the refusal message,
        which is built by joining the handler names — a fourth handler with no
        specification would appear in it and fail the comparison.
        """
        names = [tool["name"] for tool in self.specifications]
        with TemporaryDirectory() as directory:
            reader = SandboxReader(root=Path(directory).resolve())
            for name in names:
                with self.subTest(tool=name):
                    try:
                        reader.dispatch(name, {})
                    except SandboxDenied as error:
                        self.assertNotIn("Unknown tool", str(error))
                    except TypeError:
                        pass  # a required argument is missing; the name resolved
            with self.assertRaises(SandboxDenied) as raised:
                reader.dispatch("write_file", {})
            self.assertEqual(
                str(raised.exception),
                f"Unknown tool 'write_file'. This sandbox is read-only and offers "
                f"exactly: {', '.join(sorted(names))}.",
            )

    def test_no_tool_offers_mutation(self) -> None:
        """Read the surface the way a model reads it: by name and by description.

        Word boundaries rather than substrings — "truncated" contains "run".
        """
        forbidden = (
            "write", "edit", "create", "delete", "remove", "modify", "replace", "insert",
            "append", "patch", "apply", "execute", "run", "shell", "bash", "command",
            "chmod", "move", "copy", "rename", "mkdir", "download", "fetch",
        )
        for tool in self.specifications:
            text = f"{tool['name']} {tool['description']}".lower()
            for word in forbidden:
                with self.subTest(tool=tool["name"], word=word):
                    self.assertIsNone(
                        re.search(rf"\b{word}\b", text),
                        f"{tool['name']} description offers {word!r}",
                    )

    def test_every_schema_is_closed_and_names_only_real_arguments(self) -> None:
        """`additionalProperties: False` is the same refusal `dispatch` makes."""
        expected = {
            "list_dir": ({"path"}, []),
            "read_file": ({"path", "start_line", "line_count"}, ["path"]),
            "search": ({"pattern", "path", "include", "fixed"}, ["pattern"]),
        }
        for tool in self.specifications:
            with self.subTest(tool=tool["name"]):
                schema = tool["input_schema"]
                properties, required = expected[tool["name"]]
                self.assertEqual(schema["type"], "object")
                self.assertEqual(set(schema["properties"]), properties)
                self.assertEqual(schema["required"], required)
                self.assertIs(schema["additionalProperties"], False)

    def test_the_read_bound_in_the_schema_is_the_bound_the_reader_enforces(self) -> None:
        """A schema promising 10_000 lines over a reader that refuses at 2_000 is
        a model told to ask for something it will be refused for."""
        read_file = next(tool for tool in self.specifications if tool["name"] == "read_file")
        properties = read_file["input_schema"]["properties"]
        self.assertEqual(properties["line_count"]["maximum"], MAX_READ_LINES)
        self.assertEqual(properties["line_count"]["minimum"], 1)
        self.assertEqual(properties["start_line"]["minimum"], 1)

    def test_the_specifications_are_plain_json(self) -> None:
        """Vendor-neutral by construction: no wrapper type survives a round trip."""
        self.assertEqual(json.loads(json.dumps(self.specifications)), self.specifications)

    def test_the_descriptions_warn_about_the_two_things_that_mislead(self) -> None:
        """The caps are the model's only defence against a partial answer.

        A model that is not told a list can be truncated will reason from it as
        though it were complete, which is the `co_literals` failure again.
        """
        by_name = {tool["name"]: tool["description"] for tool in self.specifications}
        self.assertIn("capped", by_name["read_file"])
        self.assertIn("partial", by_name["read_file"])
        self.assertIn("capped", by_name["search"])
        self.assertIn("never conclude a string is unique", by_name["search"])
        self.assertIn("Literal by default", by_name["search"])


# ------------------------------------------------------------------- transcript


class ToolTranscriptTests(unittest.TestCase):
    """What actually happened, which is not what the proposal says happened."""

    def test_each_call_is_rendered_with_its_arguments_and_its_result(self) -> None:
        self.assertEqual(
            tool_transcript(
                iter(
                    [
                        ("list_dir", {"path": "smali_classes4/X"}, "04tC.smali"),
                        ("search", {"pattern": "LX/04tC;", "fixed": True}, "no matches"),
                    ]
                )
            ),
            '$ list_dir {"path": "smali_classes4/X"}\n04tC.smali\n\n'
            '$ search {"fixed": true, "pattern": "LX/04tC;"}\nno matches',
        )

    def test_arguments_are_rendered_in_a_stable_order(self) -> None:
        """A transcript that reorders between runs cannot be diffed against another."""
        arguments = {"pattern": "a", "fixed": True, "path": "."}
        first = tool_transcript(iter([("search", arguments, "x")]))
        reordered = {key: arguments[key] for key in reversed(list(arguments))}
        second = tool_transcript(iter([("search", reordered, "x")]))
        self.assertNotEqual(list(arguments), list(reordered))
        self.assertEqual(first, second)

    def test_no_calls_render_as_nothing(self) -> None:
        self.assertEqual(tool_transcript(iter([])), "")


if __name__ == "__main__":
    unittest.main()

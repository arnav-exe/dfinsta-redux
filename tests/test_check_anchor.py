"""Counting what an anchor picks out, and refusing to call silence a pass.

The tool exists because every anchor mistake this project has made was invisible
to reading and obvious to counting. 442's repair matched exactly one site in
187,163 classes; three rejected candidates for the same job matched 114, about
1,400, and 49. Nobody could tell those apart by looking at them.

The property that needs the most protection here is the least interesting one:
**an anchor that matches nothing must not report success.** A dead pattern is
exactly as quiet as a perfect one — same empty output, same absence of
complaints — and this repository has already shipped a search that could not
succeed and therefore always passed.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPOSITORY = Path(__file__).resolve().parents[1]


def load():
    """Load `tools/check_anchor.py` as a module.

    Registered in `sys.modules` before execution because `@dataclass` resolves
    its annotations through `sys.modules[cls.__module__]`.
    """
    spec = importlib.util.spec_from_file_location(
        "check_anchor", REPOSITORY / "tools" / "check_anchor.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_anchor"] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop("check_anchor", None)
    return module


def smali(descriptor: str, body: list[str]) -> str:
    return "\n".join([f".class public {descriptor}", ".super Ljava/lang/Object;", "",
                      ".method public A00()V", "    .locals 2", "", *body, "    return-void",
                      ".end method", ""])


#: A shape worth pinning: two instructions, the second binding a register the
#: first named. Enough for the cross-line binding rule to be exercised.
ANCHOR = ("new-instance <r:reg>, <cls:type>", "invoke-direct {<r>}, <cls>-><init>()V")
MATCHES = ["    new-instance v1, LX/Aa;", "", "    invoke-direct {v1}, LX/Aa;-><init>()V"]
#: Same instructions, different registers — the binding rule must reject it.
MISBOUND = ["    new-instance v1, LX/Aa;", "", "    invoke-direct {v2}, LX/Aa;-><init>()V"]


class CheckAnchorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def decode(self, label: str, classes: dict[str, list[str]]) -> Path:
        path = self.root / "work" / label / "analysis-decode" / "smali" / "X"
        path.mkdir(parents=True)
        for descriptor, body in classes.items():
            name = descriptor.rsplit("/", 1)[-1].rstrip(";")
            (path / f"{name}.smali").write_text(smali(descriptor, body), encoding="utf-8")
        return path

    def verdict_of(self, anchor=ANCHOR) -> str:
        return self.module.verdict(self.module.check(anchor, self.root))


class VerdictTests(CheckAnchorTestCase):
    def test_one_class_in_every_decode_is_unique(self) -> None:
        self.decode("439-a", {"LX/Aa;": MATCHES, "LX/Bb;": []})
        self.decode("440-a", {"LX/Cc;": MATCHES})
        self.assertEqual(self.module.UNIQUE, self.verdict_of())

    def test_matching_only_the_newest_decode_is_selective(self) -> None:
        """The variant shape: 442's pooled-fetch anchor matches once on 442 and
        nowhere else, and that is correct rather than a weakness."""
        self.decode("441-a", {"LX/Aa;": []})
        self.decode("442-a", {"LX/Cc;": MATCHES})
        self.assertEqual(self.module.SELECTIVE, self.verdict_of())

    def test_two_classes_anywhere_is_ambiguous(self) -> None:
        self.decode("439-a", {"LX/Aa;": MATCHES})
        self.decode("440-a", {"LX/Cc;": MATCHES, "LX/Dd;": MATCHES})
        self.assertEqual(self.module.AMBIGUOUS, self.verdict_of())

    def test_matching_nothing_anywhere_is_dead_and_not_unique(self) -> None:
        """The one that matters. A dead anchor and a perfect one are equally
        quiet, so the difference has to be stated rather than inferred."""
        self.decode("439-a", {"LX/Aa;": []})
        self.decode("440-a", {"LX/Cc;": []})
        self.assertEqual(self.module.DEAD, self.verdict_of())

    def test_no_decodes_at_all_is_dead_not_unique(self) -> None:
        """`all()` over an empty list is True, which would make "matched exactly
        once everywhere" true of a machine with nothing on it."""
        self.assertEqual(self.module.DEAD, self.verdict_of())

    def test_the_cross_line_binding_rule_is_the_matcher_s_not_ours(self) -> None:
        """Same instructions, different register: not a match. Asserted here
        because it is the one part of the grammar a re-implementation would be
        most likely to get wrong, and this tool must not have one."""
        self.decode("439-a", {"LX/Aa;": MISBOUND})
        self.assertEqual(self.module.DEAD, self.verdict_of())


class ExitCodeTests(CheckAnchorTestCase):
    """The exit code is the machine-readable half; a write-back would gate on it."""

    def run_main(self, *extra: str) -> tuple[int, str, str]:
        anchor_file = self.root / "anchor.txt"
        anchor_file.write_text("\n".join(ANCHOR) + "\n", encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(self.module, "REPOSITORY", self.root):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = self.module.main(["--anchor-file", str(anchor_file), *extra])
        return code, out.getvalue(), err.getvalue()

    def test_unique_exits_zero(self) -> None:
        self.decode("439-a", {"LX/Aa;": MATCHES})
        code, text, _ = self.run_main()
        self.assertEqual(0, code)
        self.assertIn("unique", text)

    def test_ambiguous_exits_nonzero(self) -> None:
        self.decode("439-a", {"LX/Aa;": MATCHES, "LX/Bb;": MATCHES})
        code, text, _ = self.run_main()
        self.assertEqual(1, code)
        self.assertIn("ambiguous", text)

    def test_dead_exits_nonzero_and_says_it_is_not_a_pass(self) -> None:
        self.decode("439-a", {"LX/Aa;": []})
        code, text, _ = self.run_main()
        self.assertEqual(2, code)
        self.assertIn("dead", text)
        self.assertIn("Not a pass", text)

    def test_a_pattern_that_does_not_compile_is_refused_by_name(self) -> None:
        """The commonest mistake, and the compiler already says which capture."""
        self.decode("439-a", {"LX/Aa;": MATCHES})
        broken = self.root / "broken.txt"
        broken.write_text("move-result-object <r>\n", encoding="utf-8")  # no kind
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(self.module, "REPOSITORY", self.root):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = self.module.main(["--anchor-file", str(broken)])
        self.assertEqual(2, code)
        self.assertIn("does not compile", err.getvalue())
        self.assertIn("kind", err.getvalue())

    def test_json_carries_the_same_verdict_as_the_table(self) -> None:
        self.decode("439-a", {"LX/Aa;": MATCHES})
        code, text, _ = self.run_main("--json")
        self.assertEqual(0, code)
        self.assertEqual("unique", json.loads(text)["verdict"])


class DiscoveryTests(CheckAnchorTestCase):
    def test_decodes_are_found_not_listed(self) -> None:
        """A version decoded and never added to a constant would be a version
        this tool silently did not answer for."""
        self.decode("441-port", {"LX/Aa;": []})
        self.decode("442-port", {"LX/Bb;": []})
        found = self.module.decodes(self.root)
        self.assertEqual(["441-port", "442-port"], [decode.label for decode in found])
        self.assertEqual(["441", "442"], [decode.version for decode in found])

    def test_several_runs_of_one_version_are_all_reported(self) -> None:
        """They are not guaranteed to be the same tree, and a disagreement
        between two decodes of one version is worth seeing rather than hiding."""
        self.decode("440-clean", {"LX/Aa;": MATCHES})
        self.decode("440-port", {"LX/Aa;": MATCHES})
        self.assertEqual(2, len(self.module.decodes(self.root)))

    def test_a_directory_with_no_version_prefix_is_still_reported(self) -> None:
        self.decode("scratch", {"LX/Aa;": []})
        found = self.module.decodes(self.root)
        self.assertEqual(("scratch", "?"), (found[0].label, found[0].version))

    def test_one_decode_can_be_named(self) -> None:
        self.decode("441-port", {"LX/Aa;": MATCHES})
        self.decode("442-port", {"LX/Bb;": MATCHES})
        results = self.module.check(ANCHOR, self.root, only=("442",))
        self.assertEqual(["442-port"], [result.decode.label for result in results])


class ItUsesThePipelineMatcherTests(unittest.TestCase):
    """Not a second implementation, and it must stay that way.

    A checker with its own regex would drift from the resolver and say so
    silently — a count that answers a different question from the one asked is
    worse than no count.
    """

    def test_the_scan_is_the_resolver_s(self) -> None:
        module = load()
        from dfinsta_pipeline import resolve

        self.assertIs(module.scan_for_anchor, resolve.scan_for_anchor)

    def test_it_compiles_no_smali_pattern_of_its_own(self) -> None:
        source = (REPOSITORY / "tools" / "check_anchor.py").read_text(encoding="utf-8")
        # One compiled regex, and it reads a directory name rather than smali.
        self.assertEqual(1, source.count("re.compile("))
        self.assertIn('_VERSION = re.compile(r"^(\\d+)")', source)

    def test_the_probe_marker_cannot_collide_with_a_real_patch(self) -> None:
        """`scan_for_anchor` reports classes carrying the marker as already
        patched. A marker a real decode could hold would turn a fresh class into
        a false already-applied."""
        module = load()
        manifest = json.loads(
            (REPOSITORY / "manifest" / "hooks.json").read_text(encoding="utf-8")
        )
        shipped = {hook["marker"] for hook in manifest["hooks"]}
        self.assertNotIn(module.PROBE_MARKER, shipped)
        self.assertIn("check_anchor", module.PROBE_MARKER)

    def test_the_probe_hook_carries_the_anchor_unchanged(self) -> None:
        module = load()
        self.assertEqual(ANCHOR, module.probe_hook(ANCHOR).anchor)


class RealManifestTests(unittest.TestCase):
    """Against the committed manifest, which is what a caller will check."""

    def test_every_shipped_anchor_compiles(self) -> None:
        module = load()
        from dfinsta_pipeline.hook_manifest import load_manifest

        checked = 0
        for hook in load_manifest(REPOSITORY / "manifest" / "hooks.json"):
            for index in range(len(hook.forms)):
                anchor = module.anchor_from_manifest(hook.hook_id, index)
                module.probe_hook(anchor)  # raises ManifestError if it does not
                checked += 1
        self.assertGreaterEqual(checked, 8, "seven hooks and at least one variant")

    def test_asking_for_a_form_a_hook_does_not_have_is_refused(self) -> None:
        module = load()
        with self.assertRaises(SystemExit) as caught:
            module.anchor_from_manifest("set_app_context", 9)
        self.assertIn("form", str(caught.exception))

    def test_an_unknown_hook_is_refused(self) -> None:
        module = load()
        with self.assertRaises(SystemExit) as caught:
            module.anchor_from_manifest("no_such_hook", 0)
        self.assertIn("no_such_hook", str(caught.exception))

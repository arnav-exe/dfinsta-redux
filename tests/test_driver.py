"""Tests for the driver: the stage that joins decode, index, resolve, gate, build.

Nothing here decodes, assembles or signs anything. Every run reuses a synthetic
decode (`--reuse-decode`/`--reuse-index`), and `driver.run_command` is replaced
in `setUp` for every test in this file, so no test can shell out to apktool,
java or `tools/port_430/build.py` even by accident. The decode is a handful of
hand-written smali classes spread over `smali`, `smali_classes3` and
`smali_classes10`; the index is written by `tests.test_resolve.write_index`,
which emits the same three files `tools/indexer/build_index.py` does. Reusing
that helper rather than writing a second one keeps one description of the index
format in the suite.

The tests that matter most are the two things the module docstring says it works
out for itself, because both silently produce a broken APK when wrong:

**Which DEX index is free.** `free_custom_tree` must return the first *unused*
`smali_classesN`. 430 ships 19 trees and 439 ships 20, so the answers are
`smali_classes20` and `smali_classes21`; a lexicographic maximum returns
`smali_classes9` and the custom code lands on top of a stock DEX.

**Which host DEX files to graft.** `host_dex_entries` must name exactly the trees
the resolved hosts live in — including hosts a previous run already patched —
sorted numerically, and `host_hook_map` must derive from the resolved payloads
what the verifier is then required to find in each of them.

`EvidenceGateTests` pins the distinction the whole pipeline hangs on: the gate
before the build is the PRE_APPLY half only. Requiring static, runtime and
differential evidence there would make it unsatisfiable — those need an APK that
does not exist yet — and treating a pre-apply pass as proof that a hook works is
how three inert patches shipped. So the same hook must pass at `PRE_APPLY` and
fail at the release phase, and only `--skip-evidence-gate` may get past a gate
that refuses.

`MutationTests` adds no coverage. It re-attacks four guards from the direction a
broken implementation would take, so "the guard exists" and "the guard bites"
stay separate claims. `KnownGapTests` pins four defects this suite found, which
are reported rather than fixed; each records what today's code does so a future
fix fails loudly instead of silently changing what gets built.
"""

from __future__ import annotations

import contextlib
import inspect
import io
import json
import shutil
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from dfinsta_pipeline import driver
from dfinsta_pipeline.driver import (
    FIELD_TARGET,
    STAGES,
    DriverError,
    RunPaths,
    RunResult,
    compose_patch_source,
    dex_name,
    free_custom_tree,
    host_dex_entries,
    host_hook_map,
    load_host_proposals,
    main,
    port,
    record_resolution_evidence,
    smali_trees,
    tree_order,
)
from dfinsta_pipeline.evidence import (
    PRE_APPLY,
    EvidenceError,
    EvidenceKind,
    EvidenceLedger,
)
from dfinsta_pipeline.hook_index import HookIndex
from dfinsta_pipeline.hook_manifest import Hook, HostFingerprint
from dfinsta_pipeline.resolve import (
    HookResolution,
    Outcome,
    ResolveReport,
    resolve_manifest,
)
from tests.test_resolve import write_index, write_manifest

# The hosts. Obfuscated names are this project's own shape; the trees they sit
# in are chosen so that a numeric and a lexicographic ordering disagree.
SHELL = "Lcom/instagram/app/InstagramAppShell;"  # smali          -> classes.dex
ACTION_BAR = "LX/09rb;"  # smali_classes3  -> classes3.dex
ENDPOINT = "LX/04tC;"  # smali_classes10 -> classes10.dex
SETTINGS = "LX/0Di2;"  # the by_agent host, proposed from outside


# --------------------------------------------------------------------- hooks


CONTEXT_HOOK = Hook(
    hook_id="set_probe_context",
    intent="hand the Application instance to the mod at startup",
    tier="robust",
    strategy="insert after the super call in onCreate",
    semantic_deps=(),
    hosts=(HostFingerprint("named", descriptor=SHELL, note="stable, non-obfuscated"),),
    anchor=("invoke-super {<app:reg>}, Landroid/app/Application;->onCreate()V",),
    payload=(
        "",
        "    invoke-static {<app>}, Lcom/dfinstagram/startapp;->setContext"
        "(Landroid/app/Application;)V",
    ),
    marker="Lcom/dfinstagram/startapp;->setContext(Landroid/app/Application;)V",
    expected_marker_count=1,
)

# Two DFInsta calls plus one unrelated call, and the two DFInsta descriptors are
# written in reverse sorted order on purpose: `host_hook_map` must sort them and
# must ignore the third.
ENDPOINT_HOOK = Hook(
    hook_id="replace_probe_endpoint",
    intent="route the outgoing Reels path through the mod",
    tier="robust",
    strategy="hand the path to the mod before the request is built",
    semantic_deps=("clips/discover/", "clips/homecoming/"),
    hosts=(
        HostFingerprint(
            "by_literal",
            literal="clips/discover/",
            co_literals=("clips/homecoming/",),
            note="one literal is not selective; only the request builder holds both",
        ),
    ),
    anchor=('const-string <r:reg>, "clips/discover/"',),
    payload=(
        "    invoke-static {<r>}, Lcom/dfinstagram/hooks;->replaceEndpoint"
        "(Ljava/lang/String;)Ljava/lang/String;",
        "    invoke-static {<r>}, Lcom/dfinstagram/adv_settings;->noteEndpoint"
        "(Ljava/lang/String;)V",
        "    invoke-virtual {<r>}, Ljava/lang/String;->trim()Ljava/lang/String;",
    ),
    marker="Lcom/dfinstagram/hooks;->replaceEndpoint",
    expected_marker_count=1,
)

# The action-bar shape: the payload never calls into DFInsta at all, it builds
# the mod's listener and stores it in a stock field. The only DFInsta type
# reference is the `new-instance`.
ACTION_BAR_HOOK = Hook(
    hook_id="install_probe_long_click",
    intent="swap the options-menu long-press listener for the mod's",
    tier="ui",
    strategy="replace the listener construction and the field store",
    semantic_deps=(),
    hosts=(HostFingerprint("named", descriptor=ACTION_BAR, note="found by an agent once"),),
    anchor=(
        "new-instance <l:reg>, <cls:type>",
        "iput-object <l>, <o:reg>, <owner:type>->A0H:"
        "Landroid/view/View$OnLongClickListener;",
    ),
    payload=(
        "    new-instance <l>, Lcom/dfinstagram/SettingsWrapper;",
        "    iput-object <l>, <o>, <owner>->A0H:Landroid/view/View$OnLongClickListener;",
    ),
    marker="Lcom/dfinstagram/SettingsWrapper;",
    expected_marker_count=1,
    mode="replace",
)

# The same shape with a `by_agent` fingerprint: nothing mechanical points at the
# class, so a host has to arrive through --proposals. Its own marker, because two
# hooks sharing one is refused by `assert_distinct`.
AGENT_HOOK = Hook(
    hook_id="install_probe_settings_entry",
    intent="add the mod's settings entry to the profile action bar",
    tier="ui",
    strategy="replace the listener construction and the field store",
    semantic_deps=(),
    hosts=(HostFingerprint("by_agent", note="no literal and no stable type point here"),),
    anchor=(
        "new-instance <l:reg>, <cls:type>",
        "iput-object <l>, <o:reg>, <owner:type>->A0H:"
        "Landroid/view/View$OnLongClickListener;",
    ),
    payload=(
        "    new-instance <l>, Lcom/dfinstagram/SettingsEntry;",
        "    iput-object <l>, <o>, <owner>->A0H:Landroid/view/View$OnLongClickListener;",
    ),
    marker="Lcom/dfinstagram/SettingsEntry;",
    expected_marker_count=1,
    mode="replace",
)

# Names a class this version does not have: the shape of a hook whose host moved.
MISSING_HOOK = Hook(
    hook_id="probe_hook_whose_host_moved",
    intent="a hook whose named host is gone in this version",
    tier="robust",
    strategy="none",
    semantic_deps=(),
    hosts=(HostFingerprint("named", descriptor="LX/0Gone;", note="not in this version"),),
    anchor=("nop",),
    payload=("    invoke-static {}, Lcom/dfinstagram/hooks;->vanished()V",),
    marker="Lcom/dfinstagram/hooks;->vanished()V",
    expected_marker_count=1,
)

RETIRED_HOOK = Hook(
    hook_id="retired_probe_hook",
    intent="a hook this version no longer needs",
    tier="robust",
    strategy="none",
    semantic_deps=(),
    hosts=(HostFingerprint("named", descriptor=SHELL, note="still present"),),
    anchor=("invoke-super {<app:reg>}, Landroid/app/Application;->onCreate()V",),
    payload=("    invoke-static {<app>}, Lcom/dfinstagram/hooks;->retired"
             "(Landroid/app/Application;)V",),
    marker="Lcom/dfinstagram/hooks;->retired(Landroid/app/Application;)V",
    expected_marker_count=1,
    status="retired",
)


# ------------------------------------------------------------------- sources


CLEAN_SHELL = """.class public Lcom/instagram/app/InstagramAppShell;
.super Landroid/app/Application;

.method public onCreate()V
    .locals 0

    .line 12
    invoke-super {p0}, Landroid/app/Application;->onCreate()V

    return-void
.end method
"""

APPLIED_SHELL = CLEAN_SHELL.replace(
    "    return-void",
    "    invoke-static {p0}, Lcom/dfinstagram/startapp;->setContext"
    "(Landroid/app/Application;)V\n\n    return-void",
)


def endpoint_class(descriptor: str, register: str = "v1") -> str:
    """The Reels request builder: the endpoint anchor matches here exactly once."""
    return (
        f".class public {descriptor}\n"
        ".super Ljava/lang/Object;\n"
        "\n"
        ".method public A00()Ljava/lang/String;\n"
        "    .locals 2\n"
        "\n"
        "    .line 31\n"
        f'    const-string {register}, "clips/discover/"\n'
        "\n"
        f"    return-object {register}\n"
        ".end method\n"
    )


def listener_class(descriptor: str, listener: str = "LX/0Dn9;") -> str:
    """An action-bar class that builds a listener and stores it in its own field."""
    return (
        f".class public {descriptor}\n"
        ".super Ljava/lang/Object;\n"
        "\n"
        ".method public A0G(Landroid/view/View;)V\n"
        "    .locals 2\n"
        "\n"
        "    .line 88\n"
        f"    new-instance v0, {listener}\n"
        "\n"
        f"    iput-object v0, p0, {descriptor}->A0H:"
        "Landroid/view/View$OnLongClickListener;\n"
        "\n"
        "    return-void\n"
        ".end method\n"
    )


def applied_listener_class(descriptor: str) -> str:
    """The same class after the action-bar hook landed: the marker is present."""
    return listener_class(descriptor, "Lcom/dfinstagram/SettingsWrapper;")


# ------------------------------------------------------------------ fixtures


@dataclass(frozen=True)
class Fixture:
    """A fake decode, the index built from it, and the custom code to copy in."""

    decode: Path
    index_dir: Path
    index: HookIndex
    custom_code: Path


def tree_of(path: str) -> str:
    return path.split("/", 1)[0]


class DriverCase(unittest.TestCase):
    """Base: a scratch directory, a stub `run_command`, and decode builders.

    `run_command` is replaced for every test in this file rather than only where
    a build is asserted on, so that a test which accidentally reaches stage 6
    records an argv instead of launching apktool.
    """

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.commands: list[tuple[str, list[str]]] = []
        self.printed = ""

        original = driver.run_command
        self.addCleanup(setattr, driver, "run_command", original)
        driver.run_command = self._record_command

        self.custom_code = self.base / "custom-code"
        classes = self.custom_code / "newCode" / "com" / "dfinstagram"
        classes.mkdir(parents=True)
        (classes / "startapp.smali").write_text(
            ".class public Lcom/dfinstagram/startapp;\n", encoding="utf-8"
        )
        (classes / "SettingsWrapper.smali").write_text(
            ".class public Lcom/dfinstagram/SettingsWrapper;\n", encoding="utf-8"
        )

    def _record_command(self, command: Sequence[Any], label: str) -> None:
        self.commands.append((label, [str(part) for part in command]))

    # ------------------------------------------------------------- builders

    def make_decode(
        self,
        classes: Mapping[str, tuple[str, str]],
        *,
        api_paths: Mapping[str, Sequence[str]] | None = None,
        extra_trees: Sequence[str] = (),
        name: str = "decode",
    ) -> Fixture:
        """A decode holding `classes` (descriptor -> (tree, smali text)) and its index.

        `extra_trees` are empty smali trees: apktool emits one directory per DEX
        whether or not any class this test cares about lives there, and the DEX
        topology functions read the directory listing rather than the index.
        """
        decode = self.base / name
        paths: dict[str, str] = {}
        for descriptor, (tree, body) in classes.items():
            relative = f"{tree}/" + descriptor[1:-1] + ".smali"
            paths[descriptor] = relative
            target = decode / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        for tree in extra_trees:
            (decode / tree).mkdir(parents=True, exist_ok=True)
        decode.mkdir(parents=True, exist_ok=True)
        index_dir = write_index(self.base / f"{name}-index", decode, paths, api_paths)
        return Fixture(decode, index_dir, HookIndex.load(index_dir), self.custom_code)

    def make_trees(self, trees: Sequence[str], *, name: str = "topology") -> Path:
        """A decode that is nothing but smali directories, for the DEX topology."""
        decode = self.base / name
        decode.mkdir(parents=True, exist_ok=True)
        for tree in trees:
            (decode / tree).mkdir()
        return decode

    def three_dex_fixture(self) -> Fixture:
        """One host per DEX, in trees whose numeric and text orders disagree."""
        return self.make_decode(
            {
                SHELL: ("smali", CLEAN_SHELL),
                ACTION_BAR: ("smali_classes3", listener_class(ACTION_BAR)),
                ENDPOINT: ("smali_classes10", endpoint_class(ENDPOINT)),
            },
            api_paths={
                "clips/discover/": [ENDPOINT],
                "clips/homecoming/": [ENDPOINT],
            },
            extra_trees=("smali_classes2",),
        )

    def shared_dex_fixture(self) -> Fixture:
        """Two hosts in ONE tree: the graft list must not name it twice."""
        return self.make_decode(
            {
                ACTION_BAR: ("smali_classes10", listener_class(ACTION_BAR)),
                ENDPOINT: ("smali_classes10", endpoint_class(ENDPOINT)),
            },
            api_paths={
                "clips/discover/": [ENDPOINT],
                "clips/homecoming/": [ENDPOINT],
            },
            extra_trees=("smali", "smali_classes2"),
        )

    def rerun_fixture(self) -> Fixture:
        """The re-run state: one host already carries the patch, one does not."""
        return self.make_decode(
            {
                SHELL: ("smali", APPLIED_SHELL),
                ACTION_BAR: ("smali_classes3", listener_class(ACTION_BAR)),
            },
            extra_trees=("smali_classes2",),
        )

    def agent_fixture(self) -> Fixture:
        """A mechanical host plus one that only a proposal can name."""
        return self.make_decode(
            {
                SHELL: ("smali", CLEAN_SHELL),
                SETTINGS: ("smali_classes3", listener_class(SETTINGS)),
            },
            extra_trees=("smali_classes2",),
        )

    # ------------------------------------------------------------- running

    def resolve(self, hooks: Sequence[Hook], fixture: Fixture, proposals=None):
        return resolve_manifest(hooks, fixture.index, fixture.decode, proposals)

    def run_port(self, fixture: Fixture, hooks: Sequence[Hook], **kwargs) -> RunResult:
        """Call `port` over a reused decode and index, capturing what it printed."""
        out = kwargs.pop("out", "run")
        stream = io.StringIO()
        try:
            with contextlib.redirect_stdout(stream):
                return port(
                    apk=self.base / "stock.apk",
                    paths=RunPaths(self.base / out, fixture.decode, fixture.index_dir),
                    hooks=list(hooks),
                    apktool=self.base / "apktool_2.9.3.jar",
                    framework_apk=kwargs.pop(
                        "framework_apk", self.base / "framework-res.apk"
                    ),
                    custom_code=kwargs.pop("custom_code", fixture.custom_code),
                    **kwargs,
                )
        finally:
            self.printed = stream.getvalue()

    def build_argv(self) -> dict[str, str]:
        """The builder invocation as `{flag: value}`, for the argv assertions."""
        labels = [label for label, _ in self.commands]
        self.assertIn("build", labels, f"the build never ran; commands were {labels}")
        command = dict(self.commands)["build"]
        flags: dict[str, str] = {}
        for index, part in enumerate(command):
            if part.startswith("--"):
                flags[part] = command[index + 1]
        return flags


# ------------------------------------------------------------ dex topology


class DexTopologyTests(DriverCase):
    """Which smali trees exist, in which order, and which DEX each becomes."""

    def test_trees_are_ordered_numerically_not_lexicographically(self):
        """`smali_classes10` follows `smali_classes9`, which a string sort gets wrong.

        The last entry is what `free_custom_tree` counts from, so a text sort
        does not merely mis-order the list, it returns an occupied index.
        """
        decode = self.make_trees(
            ["smali", "smali_classes2", "smali_classes9", "smali_classes10"]
        )
        self.assertEqual(
            smali_trees(decode),
            ["smali", "smali_classes2", "smali_classes9", "smali_classes10"],
        )
        self.assertEqual(
            sorted(["smali_classes9", "smali_classes10"]),
            ["smali_classes10", "smali_classes9"],
            "a plain string sort really does disagree, so the key is load-bearing",
        )

    def test_non_smali_directories_are_not_trees(self):
        """apktool writes `res/`, `lib/`, `original/` beside the smali trees.

        Counting one of those as a tree would shift every DEX index by one.
        """
        decode = self.make_trees(
            ["smali", "smali_classes2", "res", "lib", "original", "unknown"]
        )
        self.assertEqual(smali_trees(decode), ["smali", "smali_classes2"])

    def test_tree_order_places_the_base_tree_first(self):
        self.assertEqual(tree_order("smali"), 1)
        self.assertEqual(tree_order("smali_classes2"), 2)
        self.assertEqual(tree_order("smali_classes21"), 21)

    def test_dex_name_maps_the_base_tree_to_the_unnumbered_dex(self):
        """`smali` is `classes.dex`, not `classes1.dex`; the graft list is by name."""
        self.assertEqual(dex_name("smali"), "classes.dex")
        self.assertEqual(dex_name("smali_classes4"), "classes4.dex")
        self.assertEqual(dex_name("smali_classes21"), "classes21.dex")

    def test_free_custom_tree_on_the_430_topology_is_smali_classes20(self):
        """430 ships 19 trees, so 20 is the first free index — the real answer.

        Returning an index that already exists overwrites a stock DEX with the
        custom classes, which assembles and installs and is missing whatever was
        in the tree it replaced.
        """
        trees = ["smali"] + [f"smali_classes{n}" for n in range(2, 20)]
        decode = self.make_trees(trees, name="ig430")
        self.assertEqual(len(trees), 19)
        self.assertEqual(free_custom_tree(decode), "smali_classes20")
        self.assertNotIn(free_custom_tree(decode), trees)

    def test_free_custom_tree_on_the_439_topology_is_smali_classes21(self):
        """439 ships 20 trees. This is why the value cannot be a constant.

        The 430 port used `smali_classes20`; carrying that forward to 439 lands
        the custom classes on a tree the stock APK already uses.
        """
        trees = ["smali"] + [f"smali_classes{n}" for n in range(2, 21)]
        decode = self.make_trees(trees, name="ig439")
        self.assertEqual(len(trees), 20)
        self.assertEqual(free_custom_tree(decode), "smali_classes21")
        self.assertNotIn(free_custom_tree(decode), trees)

    def test_free_custom_tree_on_a_single_tree_decode_is_smali_classes2(self):
        """A one-DEX app: the first free index is 2, never 1.

        `classes1.dex` is not a name Android loads, so counting from `smali` as
        index 1 has to produce `smali_classes2`.
        """
        decode = self.make_trees(["smali"], name="single")
        self.assertEqual(free_custom_tree(decode), "smali_classes2")

    def test_a_directory_with_no_smali_tree_is_not_a_decode(self):
        """An empty or wrong `--reuse-decode` must be named, not counted as zero trees.

        Without this the first free index would be computed from an empty list
        and the run would proceed against a directory holding no app at all.
        """
        decode = self.make_trees(["res", "lib"], name="not-a-decode")
        with self.assertRaises(DriverError) as caught:
            free_custom_tree(decode)
        self.assertIn("no smali tree", str(caught.exception))
        self.assertIn(str(decode), str(caught.exception))


# --------------------------------------------------------- host dex entries


class HostDexEntryTests(DriverCase):
    """The graft list: exactly the DEX files the resolved hosts live in."""

    def test_the_graft_list_is_the_resolved_hosts_dex_files_in_numeric_order(self):
        """Neither short (a hook silently missing) nor long (a stock DEX replaced).

        The numeric order matters beyond tidiness: the list is passed through to
        `build.py --replace-dex` and read back in reports, and `classes10.dex`
        sorting before `classes3.dex` makes two runs of the same version look
        like different topologies.
        """
        fixture = self.three_dex_fixture()
        report = self.resolve([CONTEXT_HOOK, ACTION_BAR_HOOK, ENDPOINT_HOOK], fixture)
        self.assertEqual(
            [item.outcome for item in report.resolutions],
            [Outcome.RESOLVED] * 3,
        )
        self.assertEqual(
            host_dex_entries(report, fixture.index),
            ["classes.dex", "classes3.dex", "classes10.dex"],
        )

    def test_two_hooks_in_one_tree_graft_that_dex_once(self):
        """The list names DEX files to replace, so a repeat is a wasted round trip.

        `build.py` grafts by name; a duplicate would also make `--replace-dex`
        disagree with the verifier's own set of grafted entries.
        """
        fixture = self.shared_dex_fixture()
        report = self.resolve([ACTION_BAR_HOOK, ENDPOINT_HOOK], fixture)
        self.assertEqual(
            [item.outcome for item in report.resolutions], [Outcome.RESOLVED] * 2
        )
        self.assertEqual(host_dex_entries(report, fixture.index), ["classes10.dex"])

    def test_already_applied_hosts_are_grafted_too(self):
        """A hook this run did not have to patch still lives in a DEX that changed.

        On a re-run the marker is already in the host, so the hook reports
        `already_applied` and produces no operation — but the class still differs
        from stock and must be carried into the output APK. Dropping it grafts
        fewer DEX files than were actually patched, and the shipped APK loses
        that hook while every stage reports success.
        """
        fixture = self.rerun_fixture()
        report = self.resolve([CONTEXT_HOOK, ACTION_BAR_HOOK], fixture)
        outcomes = {item.hook_id: item.outcome for item in report.resolutions}
        self.assertIs(outcomes[CONTEXT_HOOK.hook_id], Outcome.ALREADY_APPLIED)
        self.assertIs(outcomes[ACTION_BAR_HOOK.hook_id], Outcome.RESOLVED)
        self.assertEqual(
            host_dex_entries(report, fixture.index), ["classes.dex", "classes3.dex"]
        )

    def test_escalating_hooks_contribute_no_dex(self):
        """A hook that did not resolve names no host, so it grafts nothing.

        The run stops on an escalation anyway; this pins that the list is derived
        from outcomes rather than from the manifest's length.
        """
        fixture = self.three_dex_fixture()
        report = self.resolve([CONTEXT_HOOK, MISSING_HOOK], fixture)
        self.assertIs(report.resolutions[1].outcome, Outcome.NOT_FOUND)
        self.assertEqual(host_dex_entries(report, fixture.index), ["classes.dex"])

    def test_a_resolved_host_the_index_cannot_place_is_a_hard_error(self):
        """A descriptor with no path means the report and the index disagree.

        Silently skipping it would drop a DEX from the graft list and ship a
        stock copy of a class this run patched.
        """
        fixture = self.three_dex_fixture()
        report = ResolveReport(
            decode=str(fixture.decode),
            index_decode=str(fixture.decode),
            index_content_hash="sha256:" + "ab" * 32,
            resolutions=(
                HookResolution(
                    "probe_ghost_host", Outcome.RESOLVED, descriptor="LX/0Ghost;"
                ),
            ),
        )
        with self.assertRaises(DriverError) as caught:
            host_dex_entries(report, fixture.index)
        self.assertIn("probe_ghost_host", str(caught.exception))
        self.assertIn("LX/0Ghost;", str(caught.exception))


# ------------------------------------------------------------ host hook map


class HostHookMapTests(DriverCase):
    """What the verifier is told to find in each grafted DEX, read from the payloads."""

    def test_an_invoke_payload_yields_the_descriptor_and_bare_method_name(self):
        """A DEX stores a method reference as three indices, never as one string.

        Only the type descriptor and the bare method name survive as literal
        strings in the DEX, so the pair is what a byte search can actually find —
        searching for the smali form `L…;->setContext(…)V` finds nothing and the
        verifier passes while proving nothing. That has already shipped once.
        """
        fixture = self.three_dex_fixture()
        report = self.resolve([CONTEXT_HOOK], fixture)
        self.assertEqual(
            host_hook_map(report, fixture.index, [CONTEXT_HOOK]),
            {"classes.dex": [["Lcom/dfinstagram/startapp;", "setContext"]]},
        )

    def test_a_payload_that_only_stores_the_object_still_yields_a_pair(self):
        """The action-bar hook calls nothing: it builds a listener and stores it.

        Its only DFInsta reference is `new-instance`, so a map derived from
        invocations alone would have no entry for that DEX at all and the
        verifier would be asked to prove nothing about the DEX carrying the
        hook.
        """
        fixture = self.three_dex_fixture()
        report = self.resolve([ACTION_BAR_HOOK], fixture)
        payload = report.resolutions[0].resolution.payload
        self.assertNotIn("invoke", "\n".join(payload))
        self.assertEqual(
            host_hook_map(report, fixture.index, [ACTION_BAR_HOOK]),
            {"classes3.dex": [["Lcom/dfinstagram/SettingsWrapper;", "<init>"]]},
        )

    def test_calls_that_are_not_dfinstagram_are_ignored(self):
        """The map says what proves the PATCH is present, not what the payload does.

        `Ljava/lang/String;->trim` is in every DEX in the app, so asserting it
        would make the check pass on an unpatched graft. The stock field the
        action-bar payload writes to is in the host DEX by definition.
        """
        fixture = self.three_dex_fixture()
        report = self.resolve([ENDPOINT_HOOK, ACTION_BAR_HOOK], fixture)
        rendered = "\n".join(
            line
            for item in report.resolutions
            for line in item.resolution.payload
        )
        self.assertIn("Ljava/lang/String;->trim", rendered)
        self.assertIn(f"{ACTION_BAR}->A0H:", rendered)

        flattened = json.dumps(host_hook_map(report, fixture.index, [ENDPOINT_HOOK, ACTION_BAR_HOOK]))
        self.assertNotIn("Ljava/lang/String;", flattened)
        self.assertNotIn(ACTION_BAR, flattened)

    def test_pairs_in_one_dex_are_merged_and_sorted(self):
        """Two hooks in one tree contribute to one entry, in a stable order.

        The map is written to disk and read by another process, so an order that
        follows payload order would make two runs of the same version produce
        different `host-hooks.json` files for the same build.
        """
        fixture = self.shared_dex_fixture()
        report = self.resolve([ENDPOINT_HOOK, ACTION_BAR_HOOK], fixture)
        mapped = host_hook_map(report, fixture.index, [ENDPOINT_HOOK, ACTION_BAR_HOOK])
        self.assertEqual(list(mapped), ["classes10.dex"])
        self.assertEqual(
            mapped["classes10.dex"],
            [
                ["Lcom/dfinstagram/SettingsWrapper;", "<init>"],
                ["Lcom/dfinstagram/adv_settings;", "noteEndpoint"],
                ["Lcom/dfinstagram/hooks;", "replaceEndpoint"],
            ],
        )
        # The payload writes `hooks` before `adv_settings`, so this is a sort and
        # not the order the lines happened to be in.
        payload = "\n".join(report.resolutions[0].resolution.payload)
        self.assertLess(
            payload.index("Lcom/dfinstagram/hooks;"),
            payload.index("Lcom/dfinstagram/adv_settings;"),
        )

    def test_the_map_is_reproducible_across_runs(self):
        """Same decode, same map, byte for byte — it is compared between runs."""
        fixture = self.shared_dex_fixture()
        hooks = [ENDPOINT_HOOK, ACTION_BAR_HOOK]
        first = host_hook_map(self.resolve(hooks, fixture), fixture.index, hooks)
        second = host_hook_map(self.resolve(list(reversed(hooks)), fixture), fixture.index, hooks)
        self.assertEqual(
            json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True)
        )


# ------------------------------------------------------------------ stages


class StageStoppingTests(DriverCase):
    """`--stop-after` runs the named stage and nothing after it."""

    def test_each_stage_stops_where_it_was_told_to(self):
        """Every stop point is reachable and reports itself.

        The stages exist so a human can inspect the intermediate artifact before
        the next one consumes it; a stop that silently ran on would defeat that.
        """
        expected_artifacts = {
            "extract": {"analysis_decode"},
            "index": {"analysis_decode", "index"},
            "resolve": {"analysis_decode", "index", "resolution"},
            "gate": {"analysis_decode", "index", "resolution", "readiness", "evidence"},
            "compose": {
                "analysis_decode",
                "index",
                "resolution",
                "readiness",
                "evidence",
                "patch_source",
            },
        }
        for stage, artifacts in expected_artifacts.items():
            with self.subTest(stage=stage):
                fixture = self.three_dex_fixture()
                result = self.run_port(
                    fixture,
                    [CONTEXT_HOOK, ACTION_BAR_HOOK, ENDPOINT_HOOK],
                    stop_after=stage,
                    out=f"run-{stage}",
                )
                self.assertEqual(result.stage_reached, stage)
                self.assertIs(result.ok, True, result.stopped_because)
                self.assertEqual(set(result.artifacts), artifacts)
                # Nothing after the named stage ran: the builder is a subprocess
                # and the decode/index are reused, so no command at all is right.
                self.assertEqual(self.commands, [])

    def test_stopping_before_the_gate_writes_no_evidence(self):
        """`--stop-after resolve` must not record claims it has not made.

        The ledger is append-only and persisted, so a stage that wrote claims
        before it evaluated them would leave a file a later run trusts.
        """
        fixture = self.three_dex_fixture()
        result = self.run_port(
            fixture, [CONTEXT_HOOK], stop_after="resolve", out="run-early"
        )
        self.assertEqual(result.stage_reached, "resolve")
        self.assertFalse((self.base / "run-early" / "evidence.jsonl").exists())
        self.assertFalse((self.base / "run-early" / "readiness.json").exists())
        self.assertFalse((self.base / "run-early" / "patch-source").exists())

    def test_stopping_at_compose_leaves_no_apk_and_no_build_command(self):
        """Compose is the last stage that touches nothing outside the run directory."""
        fixture = self.three_dex_fixture()
        result = self.run_port(
            fixture, [CONTEXT_HOOK, ACTION_BAR_HOOK], stop_after="compose", out="run-c"
        )
        self.assertEqual(result.stage_reached, "compose")
        self.assertTrue((self.base / "run-c" / "patch-source").is_dir())
        self.assertNotIn("host_hooks", result.artifacts)
        self.assertNotIn("apk", result.artifacts)
        self.assertEqual(self.commands, [])

    def test_an_unknown_stage_is_refused_before_anything_runs(self):
        """A typo in `--stop-after` must not silently mean "run everything".

        Checked first, so the refusal cannot come after a decode has been
        overwritten or a patch source composed.
        """
        fixture = self.three_dex_fixture()
        with self.assertRaises(DriverError) as caught:
            self.run_port(fixture, [CONTEXT_HOOK], stop_after="verify")
        self.assertIn("unknown stage 'verify'", str(caught.exception))
        for stage in STAGES:
            self.assertIn(stage, str(caught.exception))
        self.assertFalse((self.base / "run" / "resolution.json").exists())


class EscalationTests(DriverCase):
    """The pipeline refuses to continue past a hook it could not resolve."""

    def test_a_hook_that_cannot_resolve_stops_the_run_at_resolve(self):
        """Continuing would build an APK missing a hook nobody was told about.

        The stop has to name the hook and the reason, because the next step is a
        human deciding whether the host moved or the manifest is wrong.
        """
        fixture = self.three_dex_fixture()
        result = self.run_port(fixture, [CONTEXT_HOOK, MISSING_HOOK])
        self.assertIs(result.ok, False)
        self.assertEqual(result.stage_reached, "resolve")
        self.assertIn(MISSING_HOOK.hook_id, result.stopped_because)
        self.assertIn("LX/0Gone;", result.stopped_because)
        self.assertEqual(result.escalations, (MISSING_HOOK.hook_id,))
        self.assertEqual(self.commands, [])
        # The report is still written: the escalation is the useful artifact.
        self.assertTrue((self.base / "run" / "resolution.json").exists())

    def test_a_by_agent_hook_with_no_proposal_stops_the_run(self):
        """The two settings hooks have no mechanical fingerprint by design.

        Stopping here is the design working: nothing in the decode points at the
        host, so a run with no proposal has nothing to patch and must say so
        rather than skip the hook.
        """
        fixture = self.agent_fixture()
        result = self.run_port(fixture, [CONTEXT_HOOK, AGENT_HOOK])
        self.assertIs(result.ok, False)
        self.assertEqual(result.stage_reached, "resolve")
        self.assertIn(AGENT_HOOK.hook_id, result.stopped_because)
        self.assertIn("proposed host is required", result.stopped_because)

    def test_a_manifest_with_no_active_hook_is_not_a_complete_run(self):
        """An empty resolution set is not "nothing escalated".

        A manifest whose hooks are all retired would otherwise compose an empty
        operation list and build an APK with no hook applied at all, reported as
        a success.
        """
        fixture = self.three_dex_fixture()
        result = self.run_port(fixture, [RETIRED_HOOK])
        self.assertIs(result.ok, False)
        self.assertEqual(result.stage_reached, "resolve")
        self.assertEqual(result.stopped_because, "no active hook resolved")
        self.assertEqual(self.commands, [])

    def test_an_escalation_stops_the_run_whatever_the_later_stop_point(self):
        """The refusal is not a property of where the caller asked to stop."""
        for stage in ("gate", "compose", "build"):
            with self.subTest(stop_after=stage):
                fixture = self.three_dex_fixture()
                result = self.run_port(
                    fixture,
                    [CONTEXT_HOOK, MISSING_HOOK],
                    stop_after=stage,
                    out=f"run-{stage}",
                )
                self.assertIs(result.ok, False)
                self.assertEqual(result.stage_reached, "resolve")


# ------------------------------------------------------------ evidence gate


class EvidenceGateTests(DriverCase):
    """The gate before the build is the PRE_APPLY half of the ledger, and only that."""

    def ledger_for(self, fixture: Fixture, hooks: Sequence[Hook], proposals=None):
        """The ledger `port` would build at stage 4, without running stage 4."""
        report = self.resolve(hooks, fixture, proposals)
        ledger = EvidenceLedger(self.base / "ledger.jsonl")
        record_resolution_evidence(
            ledger, report, proposals or {}, fixture.decode, list(hooks)
        )
        return report, ledger

    def test_a_mechanically_resolved_hook_passes_the_pre_apply_gate(self):
        """Anchor uniqueness and register safety are all that CAN exist before a build.

        Requiring the static, runtime and differential items here would make the
        gate unsatisfiable: none of them can be produced without an APK, and the
        APK is what the gate is deciding whether to build.
        """
        fixture = self.three_dex_fixture()
        hooks = [CONTEXT_HOOK, ACTION_BAR_HOOK]
        _, ledger = self.ledger_for(fixture, hooks)

        readiness = ledger.report(PRE_APPLY)
        self.assertIs(readiness["complete"], True)
        self.assertEqual(readiness["escalations"], [])
        self.assertEqual(readiness["phase"], PRE_APPLY)
        recorded = {claim.kind for claim in ledger.claims}
        self.assertEqual(
            recorded, {EvidenceKind.ANCHOR_UNIQUE, EvidenceKind.REGISTERS_SAFE}
        )

    def test_the_same_hook_is_not_ready_for_release(self):
        """A pre-apply pass says nothing about whether the hook works.

        This is the distinction that three inert patches were shipped through:
        each passed everything derivable from the decode and was dead on the
        device. The static, runtime and differential items are exactly what was
        missing, and they are still missing here.
        """
        fixture = self.three_dex_fixture()
        hooks = [CONTEXT_HOOK, ACTION_BAR_HOOK]
        _, ledger = self.ledger_for(fixture, hooks)

        self.assertIs(ledger.report(PRE_APPLY)["complete"], True)
        release = ledger.report()
        self.assertIs(release["complete"], False)
        self.assertEqual(release["phase"], "release")
        for hook in hooks:
            with self.subTest(hook=hook.hook_id):
                missing = {
                    kind.value for kind in ledger.readiness(hook.hook_id).missing
                }
                self.assertEqual(
                    missing, {"static_verified", "runtime_probe", "differential"}
                )

    def test_a_passing_gate_reaches_the_build(self):
        """End to end: nothing derivable from the decode objects, so the run continues."""
        fixture = self.three_dex_fixture()
        result = self.run_port(fixture, [CONTEXT_HOOK, ACTION_BAR_HOOK, ENDPOINT_HOOK])
        self.assertIs(result.ok, True, result.stopped_because)
        self.assertEqual(result.stage_reached, "build")
        readiness = json.loads((self.base / "run" / "readiness.json").read_text())
        self.assertIs(readiness["complete"], True)
        self.assertEqual(readiness["phase"], PRE_APPLY)

    def test_the_build_says_the_apk_is_not_release_ready(self):
        """A reached build must not read as a finished port.

        The post-build items do not exist yet, and every inert patch this project
        shipped passed everything up to this point.
        """
        fixture = self.three_dex_fixture()
        self.run_port(fixture, [CONTEXT_HOOK, ACTION_BAR_HOOK])
        self.assertIn("not release-ready", self.printed)
        self.assertIn("post-build evidence", self.printed)

    def test_a_host_proposal_alone_does_not_satisfy_the_gate(self):
        """`--proposals` supplies a class to check, never corroboration.

        An agent-resolved hook needs agreement across independent proposers and
        an adversarial review, and a bare descriptor carries neither — so the run
        stops at the gate rather than at resolve, with the hook named.
        """
        fixture = self.agent_fixture()
        result = self.run_port(
            fixture,
            [CONTEXT_HOOK, AGENT_HOOK],
            proposals={AGENT_HOOK.hook_id: [SETTINGS]},
        )
        self.assertIs(result.ok, False)
        self.assertEqual(result.stage_reached, "gate")
        self.assertEqual(result.escalations, (AGENT_HOOK.hook_id,))
        self.assertIn("lack the evidence required before applying", result.stopped_because)

        readiness = json.loads((self.base / "run" / "readiness.json").read_text())
        missing = {
            status["kind"]
            for status in readiness["hooks"][AGENT_HOOK.hook_id]["statuses"]
            if not status["satisfied"]
        }
        self.assertEqual(missing, {"adversarial_verified", "proposer_agreement"})
        # The mechanically resolved hook in the same run is unaffected.
        self.assertIs(readiness["hooks"][CONTEXT_HOOK.hook_id]["ready"], True)

    def test_a_failing_gate_composes_nothing(self):
        """The patch source is what the applier writes into the tree.

        Composing it before the gate has passed leaves a directory a later run
        would refuse to overwrite, and one a human could mistake for approved.
        """
        fixture = self.agent_fixture()
        self.run_port(
            fixture,
            [CONTEXT_HOOK, AGENT_HOOK],
            proposals={AGENT_HOOK.hook_id: [SETTINGS]},
        )
        self.assertFalse((self.base / "run" / "patch-source").exists())
        self.assertEqual(self.commands, [])


class SkipEvidenceGateTests(DriverCase):
    """`--skip-evidence-gate` is the only way past a gate that refuses."""

    def failing_gate(self):
        return self.agent_fixture(), [CONTEXT_HOOK, AGENT_HOOK], {
            AGENT_HOOK.hook_id: [SETTINGS]
        }

    def test_skipping_the_gate_proceeds_to_the_build(self):
        """For bring-up on a target whose probes do not exist yet, never for a ship.

        The escape hatch has to work, or a new target cannot be brought up at
        all; it is the only thing in the pipeline that lets unproven evidence
        through, which is why the flag says so.
        """
        fixture, hooks, proposals = self.failing_gate()
        blocked = self.run_port(fixture, hooks, proposals=proposals, out="blocked")
        self.assertIs(blocked.ok, False)

        result = self.run_port(
            fixture, hooks, proposals=proposals, require_evidence=False, out="forced"
        )
        self.assertIs(result.ok, True, result.stopped_because)
        self.assertEqual(result.stage_reached, "build")
        # The readiness report still records the refusal it was told to ignore.
        readiness = json.loads((self.base / "forced" / "readiness.json").read_text())
        self.assertIs(readiness["complete"], False)
        self.assertEqual(
            [entry["hook_id"] for entry in readiness["escalations"]], [AGENT_HOOK.hook_id]
        )

    def test_no_other_argument_gets_past_a_failing_gate(self):
        """Every other knob leaves the refusal in place.

        A second way past the gate would be a second way to ship an unproven
        hook, and the flag that does it is the one thing a reviewer looks for in
        a command line.
        """
        fixture, hooks, proposals = self.failing_gate()
        variations: list[dict[str, Any]] = [
            {},
            {"stop_after": "gate"},
            {"stop_after": "compose"},
            {"stop_after": "build"},
            {"full_proposals": None},
            {"refutations": None},
            {"proposals": {**proposals, CONTEXT_HOOK.hook_id: [SHELL]}},
        ]
        for index, extra in enumerate(variations):
            with self.subTest(extra=sorted(extra)):
                arguments = {"proposals": proposals, **extra}
                result = self.run_port(fixture, hooks, out=f"v{index}", **arguments)
                self.assertIs(result.ok, False)
                self.assertEqual(result.stage_reached, "gate")

    def test_require_evidence_is_the_only_boolean_switch(self):
        """A second bypass flag would be invisible in a review of this test file.

        Pinned mechanically rather than by reading, so adding one fails here.
        """
        switches = [
            name
            for name, parameter in inspect.signature(port).parameters.items()
            if isinstance(parameter.default, bool)
        ]
        self.assertEqual(switches, ["require_evidence"])


# ------------------------------------------------------------ patch source


class ComposePatchSourceTests(DriverCase):
    """The exact directory `build.py` consumes: custom classes plus operations."""

    def operations(self) -> list[dict[str, Any]]:
        fixture = self.three_dex_fixture()
        report = self.resolve([CONTEXT_HOOK], fixture)
        return [report.resolutions[0].as_operation(CONTEXT_HOOK)]

    def test_the_custom_classes_are_copied_and_the_operations_written(self):
        """The classes are version-independent; only the operations are resolved.

        That split is the point of the manifest: the same `newCode/` ships to
        every version and the anchors are re-derived per version.
        """
        destination = self.base / "patch-source"
        operations = self.operations()
        compose_patch_source(destination, self.custom_code, operations)

        copied = destination / "newCode" / "com" / "dfinstagram" / "startapp.smali"
        self.assertTrue(copied.is_file())
        self.assertEqual(
            copied.read_text(encoding="utf-8"),
            (
                self.custom_code / "newCode" / "com" / "dfinstagram" / "startapp.smali"
            ).read_text(encoding="utf-8"),
        )

    def test_the_operations_file_is_in_the_shape_the_applier_reads(self):
        """`apply_anchored_patches.py` reads `{"version": 1, "operations": [...]}`.

        A different envelope is not a formatting difference: the applier reads
        `document["operations"]` and would fail, or worse, patch nothing.
        """
        destination = self.base / "patch-source"
        operations = self.operations()
        compose_patch_source(destination, self.custom_code, operations)

        document = json.loads(
            (destination / "patches" / "anchored_patches.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(document["version"], 1)
        self.assertEqual(document["operations"], operations)
        self.assertEqual(
            set(document["operations"][0]),
            {
                "id",
                "descriptor",
                "mode",
                "anchor",
                "expected_anchor_count",
                "marker",
                "expected_marker_count",
                "payload",
            },
        )

    def test_an_existing_destination_is_refused(self):
        """A second run must not merge into the first run's patch source.

        The applier reads whatever is on disk, so a stale `newCode/` or a stale
        operation list would be silently reused and built.
        """
        destination = self.base / "patch-source"
        destination.mkdir()
        with self.assertRaises(DriverError) as caught:
            compose_patch_source(destination, self.custom_code, self.operations())
        self.assertIn("refusing to overwrite", str(caught.exception))
        self.assertIn(str(destination), str(caught.exception))

    def test_custom_code_without_newcode_is_refused(self):
        """`--custom-code` pointed at the wrong directory must be named.

        Without the check the run composes a patch source with no DFInsta
        classes at all, and the build fails much later with a missing-class
        error naming nothing useful.
        """
        empty = self.base / "not-the-source"
        empty.mkdir()
        with self.assertRaises(DriverError) as caught:
            compose_patch_source(self.base / "dest", empty, self.operations())
        self.assertIn("newCode/", str(caught.exception))
        self.assertIn(str(empty), str(caught.exception))
        self.assertFalse((self.base / "dest").exists())

    def test_the_composed_source_matches_the_resolved_report(self):
        """Through `port`: the operations written are the ones the report resolved."""
        fixture = self.three_dex_fixture()
        hooks = [CONTEXT_HOOK, ACTION_BAR_HOOK]
        result = self.run_port(fixture, hooks, stop_after="compose")
        document = json.loads(
            (self.base / "run" / "patch-source" / "patches" / "anchored_patches.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(
            [operation["id"] for operation in document["operations"]],
            [hook.hook_id for hook in hooks],
        )
        self.assertEqual(
            result.artifacts["patch_source"], str(self.base / "run" / "patch-source")
        )


# ---------------------------------------------------------------- proposals


class LoadHostProposalsTests(DriverCase):
    """`--proposals`: the weak form, a bare host per hook."""

    def write(self, document: Any) -> Path:
        path = self.base / "proposals.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_no_file_is_no_proposals(self):
        """Every mechanical hook resolves without one, so absence is not an error."""
        self.assertEqual(load_host_proposals(None), {})

    def test_a_hook_to_descriptor_list_mapping_is_accepted(self):
        path = self.write({AGENT_HOOK.hook_id: [SETTINGS], "other": [SHELL, ACTION_BAR]})
        self.assertEqual(
            load_host_proposals(path),
            {AGENT_HOOK.hook_id: [SETTINGS], "other": [SHELL, ACTION_BAR]},
        )

    def test_a_document_that_is_not_an_object_is_refused(self):
        """A bare list is the shape `--full-proposals` takes, not this one.

        Without the check the first `.items()` raises AttributeError past every
        handler, and the CLI dies with a traceback instead of a stated reason.
        """
        for document in ([], [{"hook_id": AGENT_HOOK.hook_id}], "LX/0Di2;", 3):
            with self.subTest(document=document):
                with self.assertRaises(DriverError) as caught:
                    load_host_proposals(self.write(document))
                self.assertIn(
                    "must map hook_id to a list of descriptors", str(caught.exception)
                )

    def test_a_full_proposal_here_is_refused_and_redirected(self):
        """A proposal carrying an anchor and payload belongs in `--full-proposals`.

        Accepting it here would take the host and silently drop the anchor, the
        payload, the agreement and the adversarial review — the entire reason
        that route exists — while looking like it had been honoured.
        """
        path = self.write(
            {
                AGENT_HOOK.hook_id: {
                    "descriptor": SETTINGS,
                    "anchor": ["new-instance v0, LX/0Dn9;"],
                    "payload": ["    new-instance v0, Lcom/dfinstagram/SettingsEntry;"],
                }
            }
        )
        with self.assertRaises(DriverError) as caught:
            load_host_proposals(path)
        self.assertIn("--full-proposals", str(caught.exception))
        self.assertIn(AGENT_HOOK.hook_id, str(caught.exception))

    def test_a_list_of_anything_but_strings_is_refused(self):
        """A descriptor is a string; a nested object is a different document shape."""
        for value in ([{"descriptor": SETTINGS}], [SETTINGS, None], [3]):
            with self.subTest(value=value):
                with self.assertRaises(DriverError) as caught:
                    load_host_proposals(self.write({AGENT_HOOK.hook_id: value}))
                self.assertIn("list of descriptor strings", str(caught.exception))


# ------------------------------------------------------------------ results


class RunResultTests(DriverCase):
    """The run summary a caller serialises."""

    def test_to_dict_is_json_serialisable(self):
        """The result is written to logs and read by other processes.

        `report` holds dataclasses and enums, so a summary that leaked them
        would raise at the point of writing rather than at the point of the bug.
        """
        fixture = self.three_dex_fixture()
        result = self.run_port(fixture, [CONTEXT_HOOK, ACTION_BAR_HOOK])
        self.assertIsNotNone(result.report)

        encoded = json.dumps(result.to_dict())
        restored = json.loads(encoded)
        self.assertEqual(restored["stage_reached"], "build")
        self.assertIs(restored["ok"], True)
        self.assertEqual(restored["stopped_because"], "")
        self.assertEqual(restored["escalations"], [])
        self.assertEqual(restored["artifacts"]["custom_tree"], "smali_classes11")

    def test_a_stopped_result_serialises_its_reason(self):
        fixture = self.three_dex_fixture()
        result = self.run_port(fixture, [CONTEXT_HOOK, MISSING_HOOK])
        restored = json.loads(json.dumps(result.to_dict()))
        self.assertIs(restored["ok"], False)
        self.assertEqual(restored["escalations"], [MISSING_HOOK.hook_id])
        self.assertIn(MISSING_HOOK.hook_id, restored["stopped_because"])

    def test_ok_is_derived_from_the_stated_reason(self):
        """`ok` must not be a separate field that can disagree with the reason."""
        self.assertIs(RunResult("build").ok, True)
        self.assertIs(RunResult("gate", stopped_because="a hook lacks evidence").ok, False)


# -------------------------------------------------------------------- build


class BuildInvocationTests(DriverCase):
    """What the driver hands `tools/port_430/build.py`, computed per version."""

    def test_the_builder_is_given_the_computed_topology(self):
        """The three arguments that move between versions, plus the verifier choice.

        `--custom-tree` and `--replace-dex` are the two things the module
        docstring says silently produce a broken APK when wrong, and `generic` is
        the verifier that checks them against a per-run map instead of 430's
        hard-coded descriptors.
        """
        fixture = self.three_dex_fixture()
        result = self.run_port(fixture, [CONTEXT_HOOK, ACTION_BAR_HOOK, ENDPOINT_HOOK])
        self.assertEqual(result.stage_reached, "build")

        flags = self.build_argv()
        self.assertEqual(flags["--custom-tree"], "smali_classes11")
        self.assertEqual(flags["--replace-dex"], "classes.dex,classes3.dex,classes10.dex")
        self.assertEqual(flags["--verifier"], "generic")
        self.assertEqual(flags["--host-hooks"], str(self.base / "run" / "host-hooks.json"))
        self.assertEqual(flags["--output-apk"], str(self.base / "run" / "dfinsta.apk"))
        # The build decodes the stock APK for itself, into its own directory.
        self.assertNotEqual(flags["--work-tree"], str(fixture.decode))
        command = dict(self.commands)["build"]
        self.assertEqual(command[1], str(driver.BUILDER))
        self.assertNotIn(str(fixture.decode), command)

    def test_the_host_hooks_file_on_disk_is_the_derived_map(self):
        """The verifier reads the file, not the value in memory.

        A map computed correctly and written wrongly proves nothing, and the
        verifier cannot tell the difference between "asserted and found" and
        "never asserted".
        """
        fixture = self.three_dex_fixture()
        hooks = [CONTEXT_HOOK, ACTION_BAR_HOOK, ENDPOINT_HOOK]
        self.run_port(fixture, hooks)

        written = json.loads(Path(self.build_argv()["--host-hooks"]).read_text())
        expected = host_hook_map(self.resolve(hooks, fixture), fixture.index, hooks)
        self.assertEqual(written, expected)
        self.assertEqual(
            written,
            {
                "classes.dex": [["Lcom/dfinstagram/startapp;", "setContext"]],
                "classes3.dex": [["Lcom/dfinstagram/SettingsWrapper;", "<init>"]],
                "classes10.dex": [
                    ["Lcom/dfinstagram/adv_settings;", "noteEndpoint"],
                    ["Lcom/dfinstagram/hooks;", "replaceEndpoint"],
                ],
            },
        )
        # Every DEX the verifier is asked about is one the build actually grafts.
        self.assertEqual(
            set(written), set(self.build_argv()["--replace-dex"].split(","))
        )

    def test_building_without_a_framework_apk_is_refused(self):
        """Instagram 430+ cannot be decoded or assembled without the API 36 framework.

        Reaching apktool without it fails deep inside the build with a resource
        error, so the driver refuses while the message can still say which
        argument is missing.
        """
        fixture = self.three_dex_fixture()
        with self.assertRaises(DriverError) as caught:
            self.run_port(fixture, [CONTEXT_HOOK, ACTION_BAR_HOOK], framework_apk=None)
        self.assertIn("--framework-apk is required to build", str(caught.exception))
        self.assertEqual(self.commands, [])

    def test_a_build_with_nothing_to_prove_is_refused(self):
        """An empty host-hook map makes every hook assertion vacuously true.

        `tools/verify/verify_build.py` raises on an empty map for the same
        reason; refusing here means the run stops before an APK exists to be
        mistaken for a verified one.
        """
        fixture = self.make_decode(
            {SHELL: ("smali", CLEAN_SHELL)}, extra_trees=("smali_classes2",)
        )
        no_dfinsta = Hook(
            hook_id="probe_hook_with_no_dfinsta_reference",
            intent="a payload that names nothing of ours",
            tier="robust",
            strategy="insert a stock call",
            semantic_deps=(),
            hosts=(HostFingerprint("named", descriptor=SHELL),),
            anchor=("invoke-super {<app:reg>}, Landroid/app/Application;->onCreate()V",),
            payload=("    invoke-static {<app>}, LX/0Log;->A01(Ljava/lang/Object;)V",),
            marker="LX/0Log;->A01(Ljava/lang/Object;)V",
            expected_marker_count=1,
        )
        with self.assertRaises(DriverError) as caught:
            self.run_port(fixture, [no_dfinsta])
        self.assertIn("pass vacuously", str(caught.exception))
        self.assertEqual(self.commands, [])

    def test_the_cli_wires_the_skip_flag_through_to_the_gate(self):
        """`--skip-evidence-gate` is the only flag that changes what the gate does.

        Tested through `main()` because the flag's whole risk is that it is a
        command-line argument: a wiring mistake either disables the gate for
        everyone or makes the escape hatch silently ineffective.
        """
        fixture = self.agent_fixture()
        manifest = write_manifest(self.base / "hooks.json", [CONTEXT_HOOK, AGENT_HOOK])
        proposals = self.base / "hosts.json"
        proposals.write_text(json.dumps({AGENT_HOOK.hook_id: [SETTINGS]}), encoding="utf-8")

        def invoke(out: str, *extra: str) -> tuple[int, str, str]:
            stdout, stderr = io.StringIO(), io.StringIO()
            argv = [
                str(self.base / "stock.apk"),
                "--out", str(self.base / out),
                "--manifest", str(manifest),
                "--custom-code", str(self.custom_code),
                "--apktool", str(self.base / "apktool_2.9.3.jar"),
                "--framework-apk", str(self.base / "framework-res.apk"),
                "--proposals", str(proposals),
                "--reuse-decode", str(fixture.decode),
                "--reuse-index", str(fixture.index_dir),
                "--stop-after", "compose",
                *extra,
            ]
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main(argv)
            return code, stdout.getvalue(), stderr.getvalue()

        code, _, stderr = invoke("cli-gated")
        self.assertEqual(code, 1)
        self.assertIn("STOPPED at gate", stderr)
        self.assertFalse((self.base / "cli-gated" / "patch-source").exists())

        code, stdout, _ = invoke("cli-forced", "--skip-evidence-gate")
        self.assertEqual(code, 0)
        self.assertIn("reached stage: compose", stdout)
        self.assertTrue((self.base / "cli-forced" / "patch-source").is_dir())


# ----------------------------------------------------------------- mutants


class MutationTests(DriverCase):
    """Each guard, shown biting.

    A guard that never changes an answer is decoration. These re-apply the real
    inputs through the broken rule and assert the answer changes, so "the guard
    is present" and "the guard matters" stay separate claims.
    """

    def test_a_lexicographic_free_tree_would_overwrite_a_stock_dex(self):
        """Mutation: `max(trees)` instead of the numeric key.

        In production the custom classes are assembled into `classes10.dex`,
        which the stock APK already uses. The graft replaces a real DEX with the
        mod's classes, so the APK installs and every class that lived in
        `classes10.dex` is gone.
        """
        trees = ["smali"] + [f"smali_classes{n}" for n in range(2, 20)]
        decode = self.make_trees(trees, name="ig430")

        self.assertEqual(free_custom_tree(decode), "smali_classes20")
        self.assertNotIn("smali_classes20", trees)

        mutant = f"smali_classes{tree_order(max(smali_trees(decode))) + 1}"
        self.assertEqual(max(smali_trees(decode)), "smali_classes9")
        self.assertEqual(mutant, "smali_classes10")
        self.assertIn(mutant, trees, "the mutant lands on a tree the stock APK ships")
        self.assertNotEqual(mutant, free_custom_tree(decode))

    def test_dropping_already_applied_hosts_would_graft_fewer_dex_than_were_patched(self):
        """Mutation: filter `host_dex_entries` to RESOLVED only.

        In production a re-run over a decode this pipeline already patched builds
        an APK carrying the stock copy of the class the previous run patched. The
        hook is in the work tree and simply never reaches the output, and every
        stage reports success.
        """
        fixture = self.rerun_fixture()
        report = self.resolve([CONTEXT_HOOK, ACTION_BAR_HOOK], fixture)
        real = host_dex_entries(report, fixture.index)
        self.assertEqual(real, ["classes.dex", "classes3.dex"])

        mutant = sorted(
            {
                dex_name(tree_of(fixture.index.path_for(item.descriptor)))
                for item in report.resolutions
                if item.outcome is Outcome.RESOLVED
            }
        )
        self.assertEqual(mutant, ["classes3.dex"])
        self.assertNotIn("classes.dex", mutant)
        # The dropped DEX is exactly the one holding the already-applied hook.
        applied = next(
            item
            for item in report.resolutions
            if item.outcome is Outcome.ALREADY_APPLIED
        )
        self.assertEqual(
            dex_name(tree_of(fixture.index.path_for(applied.descriptor))), "classes.dex"
        )

    def test_gating_on_the_release_phase_would_make_a_build_impossible(self):
        """Mutation: `ledger.report()` instead of `ledger.report(PRE_APPLY)`.

        In production no build is ever produced: the static, runtime and
        differential items need an APK, the APK needs the build, and the build
        needs those items. The gate becomes unsatisfiable and the pipeline can
        never emit the artifact its own evidence is about.
        """
        fixture = self.three_dex_fixture()
        hooks = [CONTEXT_HOOK, ACTION_BAR_HOOK]
        report = self.resolve(hooks, fixture)
        ledger = EvidenceLedger(self.base / "mutant-ledger.jsonl")
        record_resolution_evidence(ledger, report, {}, fixture.decode, hooks)

        self.assertIs(ledger.report(PRE_APPLY)["complete"], True)
        mutant = ledger.report()
        self.assertIs(mutant["complete"], False)
        self.assertEqual(
            sorted(entry["hook_id"] for entry in mutant["escalations"]),
            sorted(hook.hook_id for hook in hooks),
        )
        # And the items it would demand cannot exist before a build.
        for entry in mutant["escalations"]:
            unmet = {
                status["kind"] for status in entry["statuses"] if not status["satisfied"]
            }
            self.assertEqual(
                unmet, {"static_verified", "runtime_probe", "differential"}
            )

    def test_overwriting_the_patch_source_would_silently_reuse_a_stale_one(self):
        """Mutation: merge into an existing destination instead of refusing.

        In production the second run's operations land beside the first run's,
        and any custom class the first run copied stays in the tree. The applier
        reads whatever is on disk, so the build is made from a mixture of two
        runs that nothing reports.
        """
        fixture = self.three_dex_fixture()
        operations = [
            self.resolve([CONTEXT_HOOK], fixture)
            .resolutions[0]
            .as_operation(CONTEXT_HOOK)
        ]
        destination = self.base / "patch-source"
        stale_classes = destination / "newCode" / "com" / "dfinstagram"
        stale_classes.mkdir(parents=True)
        (stale_classes / "removed_last_version.smali").write_text("stale", encoding="utf-8")
        (destination / "patches").mkdir()
        (destination / "patches" / "anchored_patches.json").write_text(
            json.dumps({"version": 1, "operations": [{"id": "an_older_run"}]}),
            encoding="utf-8",
        )

        with self.assertRaises(DriverError):
            compose_patch_source(destination, self.custom_code, operations)
        # Refused, so the stale source is exactly as it was: nothing merged.
        self.assertEqual(
            json.loads((destination / "patches" / "anchored_patches.json").read_text())[
                "operations"
            ],
            [{"id": "an_older_run"}],
        )

        # The mutant: the same body without the guard.
        shutil.copytree(
            self.custom_code / "newCode", destination / "newCode", dirs_exist_ok=True
        )
        (destination / "patches" / "anchored_patches.json").write_text(
            json.dumps({"version": 1, "operations": list(operations)}), encoding="utf-8"
        )
        copied = destination / "newCode" / "com" / "dfinstagram"
        survivors = sorted(path.name for path in copied.iterdir())
        self.assertIn("removed_last_version.smali", survivors)
        self.assertIn("startapp.smali", survivors)


# ------------------------------------------------------------- known gaps


class ReportedDefectTests(DriverCase):
    """Four defects this suite found in `driver.py`, and the fixes for them.

    Each docstring records what the defect would have cost, because that is the
    reason to keep the test rather than the reason it once failed.
    """

    def test_full_proposals_reach_the_gate_without_a_double_registration(self):
        """`assess_proposals` and `record_resolution_evidence` both registered the subject.

        `assess` registers the hook as proposed by the winning proposer;
        `record_resolution_evidence` then registered it again as proposed by
        `proposal:<hook_id>`, and the ledger refuses a second, different
        registration. That killed `--full-proposals` — the only route by which a
        `requires_proposal` hook can be satisfied — at stage 4 with an
        `EvidenceError` neither `port` nor `main` caught, so the CLI exited with
        a traceback rather than a stated reason.
        """
        fixture = self.agent_fixture()
        anchor = [
            "new-instance v0, LX/0Dn9;",
            f"iput-object v0, p0, {SETTINGS}->A0H:Landroid/view/View$OnLongClickListener;",
        ]
        payload = [
            "    new-instance v0, Lcom/dfinstagram/SettingsEntry;",
            f"    iput-object v0, p0, {SETTINGS}->A0H:"
            "Landroid/view/View$OnLongClickListener;",
        ]
        proposal = {
            "hook_id": AGENT_HOOK.hook_id,
            "proposer": "agent-a",
            "descriptor": SETTINGS,
            "anchor": anchor,
            "payload": payload,
            "rationale": "the options long-press listener",
        }
        path = self.base / "full-proposals.json"
        path.write_text(
            json.dumps(
                {
                    AGENT_HOOK.hook_id: [
                        proposal,
                        {**proposal, "proposer": "agent-b", "rationale": "same site"},
                    ]
                }
            ),
            encoding="utf-8",
        )

        early = self.run_port(
            fixture,
            [CONTEXT_HOOK, AGENT_HOOK],
            full_proposals=path,
            stop_after="resolve",
            out="assessed",
        )
        assessments = json.loads((self.base / "assessed" / "assessments.json").read_text())
        self.assertIs(assessments[AGENT_HOOK.hook_id]["resolved"], True)
        self.assertEqual(early.stage_reached, "resolve")

        # And the stage after it no longer dies: the ledger keeps the real
        # proposer that `assess` recorded.
        result = self.run_port(
            fixture,
            [CONTEXT_HOOK, AGENT_HOOK],
            full_proposals=path,
            stop_after="gate",
            out="gated",
        )
        self.assertEqual(result.stage_reached, "gate")
        readiness = json.loads((self.base / "gated" / "readiness.json").read_text())
        self.assertIn(AGENT_HOOK.hook_id, readiness["hooks"])

    def test_an_already_applied_hook_can_pass_the_pre_apply_gate(self):
        """It used to be impossible, so every re-run stopped at the gate.

        `record_resolution_evidence` skipped both claims for an already-applied
        hook, leaving the ledger with none at all — and a hook with no claims
        escalates with every required kind `not_exercised`. A re-run over a
        decode this pipeline patched, the case `resolve.py` exists to handle,
        could only proceed with `--skip-evidence-gate`.

        The fix is a provenance rather than a fabricated claim. Register liveness
        genuinely cannot be re-derived once the payload is in place, so
        `already_applied` does not require it; what it does require is real —
        the marker is this pipeline's own stamp, and its presence at exactly the
        expected count proves this exact payload is in this exact class.
        """
        fixture = self.rerun_fixture()
        result = self.run_port(fixture, [CONTEXT_HOOK, ACTION_BAR_HOOK])
        self.assertIs(result.ok, True)

        readiness = json.loads((self.base / "run" / "readiness.json").read_text())
        self.assertIs(readiness["hooks"][CONTEXT_HOOK.hook_id]["ready"], True)
        self.assertIs(readiness["hooks"][ACTION_BAR_HOOK.hook_id]["ready"], True)
        kinds = {
            status["kind"]: status
            for status in readiness["hooks"][CONTEXT_HOOK.hook_id]["statuses"]
        }
        # The anchor claim is real evidence and is present; liveness is not
        # required for this provenance, so it is not silently asserted either.
        self.assertEqual(kinds["anchor_unique"]["verdict"], "passed")
        self.assertNotIn("registers_safe", kinds)

    def test_an_already_applied_host_is_grafted_and_verified(self):
        """`host_dex_entries` kept already-applied hosts while `host_hook_map` dropped them.

        A DEX grafted because a previous run patched it carried no entry in the
        map, and the verifier iterates the map — so that DEX was replaced in the
        output APK with nothing asserted about its contents. The vacuous pass
        `tools/verify/verify_build.py` refuses globally, reintroduced per DEX.
        """
        fixture = self.rerun_fixture()
        hooks = [CONTEXT_HOOK, ACTION_BAR_HOOK]
        report = self.resolve(hooks, fixture)
        grafted = host_dex_entries(report, fixture.index)
        proven = host_hook_map(report, fixture.index, hooks)

        self.assertEqual(grafted, ["classes.dex", "classes3.dex"])
        self.assertEqual(sorted(proven), grafted)
        self.assertEqual(set(grafted) - set(proven), set())

        self.run_port(fixture, hooks, out="forced")
        flags = self.build_argv()
        self.assertEqual(flags["--replace-dex"], "classes.dex,classes3.dex")
        self.assertEqual(
            sorted(json.loads(Path(flags["--host-hooks"]).read_text())),
            ["classes.dex", "classes3.dex"],
        )

    def test_a_payload_that_only_writes_a_field_derives_a_host_hook(self):
        """`FIELD_TARGET`'s `iput` alternative could never match a real instruction.

        `[^,]+` cannot span the second register operand and every `iput*` has
        two, so `iput-object v0, p0, Lcom/dfinstagram/X;->f:T` did not match, and
        `sput`/`iget` were not in the alternation at all. Only `new-instance` and
        `sget` could ever fire, so a payload that stores an existing instance
        without constructing one derived no pair and the run died at the build
        with "no host hook could be derived".
        """
        fixture = self.make_decode(
            {ACTION_BAR: ("smali_classes3", listener_class(ACTION_BAR))},
            extra_trees=("smali", "smali_classes2"),
        )
        store_only = Hook(
            hook_id="store_probe_singleton",
            intent="hand the mod's listener over without constructing it",
            tier="ui",
            strategy="replace the field store",
            semantic_deps=(),
            hosts=(HostFingerprint("named", descriptor=ACTION_BAR),),
            anchor=ACTION_BAR_HOOK.anchor,
            payload=(
                "    iput-object <l>, <o>, Lcom/dfinstagram/SettingsWrapper;->A0H:"
                "Landroid/view/View$OnLongClickListener;",
            ),
            marker="Lcom/dfinstagram/SettingsWrapper;",
            expected_marker_count=1,
            mode="replace",
        )
        report = self.resolve([store_only], fixture)
        self.assertIs(report.resolutions[0].outcome, Outcome.RESOLVED)
        self.assertEqual(
            host_hook_map(report, fixture.index, [store_only]),
            {"classes3.dex": [["Lcom/dfinstagram/SettingsWrapper;", "<init>"]]},
        )

    def test_every_field_referencing_opcode_is_recognised(self):
        """The regex must cover both one- and two-register operand shapes."""
        cases = {
            "    iput-object v0, v1, Lcom/dfinstagram/A;->f:I": "Lcom/dfinstagram/A;",
            "    iget-object v2, p0, Lcom/dfinstagram/B;->g:I": "Lcom/dfinstagram/B;",
            "    sput-object v0, Lcom/dfinstagram/C;->h:I": "Lcom/dfinstagram/C;",
            "    sget-object v0, Lcom/dfinstagram/D;->i:I": "Lcom/dfinstagram/D;",
            "    new-instance v0, Lcom/dfinstagram/E;": "Lcom/dfinstagram/E;",
        }
        for line, expected in cases.items():
            with self.subTest(line=line.strip()):
                match = FIELD_TARGET.search(line)
                self.assertIsNotNone(match)
                self.assertEqual(match.group("descriptor"), expected)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

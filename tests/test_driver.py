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

The `Discovery*` classes cover stage 5a, and every one of them injects a fake
`AgentRunner`. **No test here may depend on a model, an API key or the `claude`
CLI**: the deterministic spine must run where none of the three exist, and a
suite that reached a network would be measuring the network. What they do
exercise for real is the sandbox — `proposer.build_sandbox` hardlinks the
synthetic decode into the scratch tree, so the refusals that keep the answers
physically absent are executed rather than described.

Two of them are worth reading before changing anything here. `DiscoveryStallTests`
pins the difference between the two ways discovery can fail: proposers who
disagree produce NO host, so the hook stays escalated and the run stops at
Resolve, while a verifier who refutes leaves the agreed host in place and a
failed claim beside it, so the hook resolves and stalls at the gate with the
finding attached. `DiscoveryBudgetTests` pins that a hook the cap never reached
is reported as *skipped* and never as *unresolved* — silent truncation reads as
"covered everything", which is the one lie a budget report must not tell.
"""

from __future__ import annotations

import contextlib
import inspect
import io
import json
import shutil
import tempfile
import threading
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from dfinsta_pipeline import driver
from dfinsta_pipeline.discovery import Discovery
from dfinsta_pipeline.driver import (
    ASSESS_ARGUMENTS,
    REPOSITORY,
    FIELD_TARGET,
    STAGES,
    AssessmentRequest,
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
    record_assessment,
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

        # Stage 10 writes two files that deliberately live next to the manifest
        # rather than in the run directory, because what a port learned and what
        # it cost are worth nothing unless they outlive the decode. `run_port`
        # redirects both into the scratch directory; this asserts no test ever
        # creates the real ones, because a suite that appends to a committed
        # ledger makes its own runs look like measured ports.
        committed = [RunPaths(self.base).decision_memory, RunPaths(self.base).cost_ledger]
        before = {path: path.exists() for path in committed}
        self.addCleanup(
            lambda: [
                self.assertIs(
                    path.exists(), state, f"the suite wrote to the committed {path}"
                )
                for path, state in before.items()
            ]
        )

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

    def run_paths(self, out: str, fixture: Fixture) -> RunPaths:
        """A run directory, with both stage-10 stores redirected into the scratch tree."""
        return RunPaths(
            self.base / out,
            fixture.decode,
            fixture.index_dir,
            memory_path=self.base / "decisions.jsonl",
            ledger_path=self.base / "agent_cost.jsonl",
        )

    def run_port(self, fixture: Fixture, hooks: Sequence[Hook], **kwargs) -> RunResult:
        """Call `port` over a reused decode and index, capturing what it printed.

        `static_evidence_root` defaults into the case's own temp tree, and that
        default is the point rather than a tidy-up. A labelled run publishes its
        `static_verified` claims to `manifest/static_evidence/` in the repository,
        so before the driver had this seam every test calling `run_port(...,
        version="439")` appended to a tracked file. `test_claim_attribution` does
        exactly that, and 36 rows naming three hooks that do not exist had reached
        `manifest/static_evidence/439.jsonl` and been committed. Overriding it per
        test would have worked and would have needed remembering; defaulting it
        here cannot be forgotten.
        """
        out = kwargs.pop("out", "run")
        kwargs.setdefault("static_evidence_root", self.base / "static_evidence")
        stream = io.StringIO()
        try:
            with contextlib.redirect_stdout(stream):
                return port(
                    apk=self.base / "stock.apk",
                    paths=self.run_paths(out, fixture),
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
            # No state root here, so `assess` skips and adds nothing. That the
            # stage is *reachable* and reports itself is what this pins; what it
            # produces when given somewhere to record is `AssessStageTests`.
            "assess": {"analysis_decode", "index"},
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

    def test_every_stage_has_a_stop_expectation(self):
        """Derived from STAGES, so a new stage cannot be added untested.

        The dict above is hand-written. `assess` was added to `STAGES` and this
        test is what makes forgetting to list the next one a failure rather than
        a silently narrower loop.
        """
        from dfinsta_pipeline.driver import STAGES

        listed = {
            "extract",
            "index",
            "assess",
            "resolve",
            "gate",
            "compose",
        }
        # `build` shells out to the builder, so it is covered by the build tests
        # rather than here; everything before it must be listed.
        self.assertEqual(listed, set(STAGES) - {"build"})

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


# ------------------------------------------------------------- host discovery


STAMP = "2026-08-02T12:00:00+00:00"
#: A second `by_agent` host, so the budget can be given more hooks than it covers.
SETTINGS_TWO = "LX/0Di3;"
#: Two classes no fixture contains: what a proposer names when it is wrong. Both
#: sort BEFORE the real host, so a three-way split whose "most common" answer is
#: picked by ranking would pick the real one and resolve — which is exactly the
#: rewrite `DiscoveryMutationTests` has to be able to see.
WRONG = "LX/0Aaa;"
ALSO_WRONG = "LX/0Bbb;"

AGENT_HOOK_TWO = Hook(
    hook_id="install_probe_settings_shortcut",
    intent="add the mod's shortcut to the profile action bar",
    tier="ui",
    strategy="replace the listener construction and the field store",
    semantic_deps=(),
    hosts=(HostFingerprint("by_agent", note="no literal and no stable type point here"),),
    anchor=AGENT_HOOK.anchor,
    payload=(
        "    new-instance <l>, Lcom/dfinstagram/SettingsShortcut;",
        "    iput-object <l>, <o>, <owner>->A0H:Landroid/view/View$OnLongClickListener;",
    ),
    marker="Lcom/dfinstagram/SettingsShortcut;",
    expected_marker_count=1,
    mode="replace",
)


def host_answer(descriptor: str, path: str = "smali_classes3/X/0Di2.smali", **extra) -> str:
    """One proposer's answer, in the shape `proposer.HOST_SCHEMA` asks for."""
    return json.dumps(
        {
            "descriptor": descriptor,
            "smali_path": path,
            "evidence": ["listed the tree", "read the class and followed the field"],
            **extra,
        }
    )


def verdict(refuted: bool, finding: str = "looked and could not break it") -> str:
    return json.dumps({"refuted": refuted, "finding": finding, "checked": ["the class"]})


class FakeAgent:
    """One scripted agent run. Records what it was asked, so a retry is visible."""

    def __init__(self, reply: Any, before: Any = None):
        self.reply = reply
        self.before = before
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.before is not None:
            self.before()
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class Agents:
    """A `discovery.RunnerFactory` over scripted replies, keyed by runner name.

    Every runner the driver builds is kept, so a test can assert what each was
    asked, that it was asked once, and — for the hooks a budget never reached —
    that nothing was asked at all. No network, no model, no `claude` CLI: the
    suite must not depend on any of the three.
    """

    def __init__(
        self,
        replies: Mapping[str, Any],
        before: Any = None,
        before_prefix: str = "proposer",
    ):
        self.replies = dict(replies)
        self.before = before
        self.before_prefix = before_prefix
        self.built: list[tuple[str, FakeAgent]] = []
        self.sandboxes: list[Path] = []

    def __call__(self, name: str, sandbox: Path) -> FakeAgent:
        if name not in self.replies:
            raise AssertionError(f"the driver built an unexpected runner {name!r}")
        self.sandboxes.append(sandbox)
        hold = self.before if name.startswith(self.before_prefix) else None
        agent = FakeAgent(self.replies[name], hold)
        self.built.append((name, agent))
        return agent

    @property
    def calls(self) -> int:
        return sum(len(agent.prompts) for _, agent in self.built)

    def prompts(self, prefix: str = "") -> list[str]:
        return [
            prompt
            for name, agent in self.built
            if name.startswith(prefix)
            for prompt in agent.prompts
        ]


def agreeing(descriptor: str = SETTINGS, refuted: bool = False) -> Agents:
    """Three proposers that agree, and one verifier that does not refute."""
    return Agents(
        {
            "proposer-1": host_answer(descriptor),
            "proposer-2": host_answer(descriptor),
            "proposer-3": host_answer(descriptor),
            "verifier-1": verdict(refuted),
        }
    )


class DiscoveryCase(DriverCase):
    """Shared wiring: a `by_agent` hook, scripted agents, and no network anywhere."""

    def discover(self, agents: Agents, **kwargs) -> Discovery:
        return Discovery(
            version=kwargs.pop("version", "439"),
            runner=agents,
            sandbox_root=kwargs.pop("sandbox_root", self.base / "sandbox-root"),
            **kwargs,
        )

    def run_discovery(
        self,
        agents: Agents,
        hooks: Sequence[Hook] = (CONTEXT_HOOK, AGENT_HOOK),
        fixture: Fixture | None = None,
        settings: Discovery | None = None,
        **kwargs,
    ) -> RunResult:
        fixture = fixture or self.agent_fixture()
        return self.run_port(
            fixture,
            list(hooks),
            discovery=settings or self.discover(agents),
            version=kwargs.pop("version", "439"),
            recorded_at=kwargs.pop("recorded_at", STAMP),
            **kwargs,
        )

    def evidence_claims(self, out: str = "run") -> list[dict[str, Any]]:
        path = self.base / out / "evidence.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def claims_of(self, kind: str, out: str = "run") -> list[dict[str, Any]]:
        return [claim for claim in self.evidence_claims(out) if claim["kind"] == kind]

    def discovery_json(self, out: str = "run") -> dict[str, Any]:
        return json.loads((self.base / out / "discovery.json").read_text())


class DiscoveryDefaultTests(DiscoveryCase):
    """Off unless asked for, and identical to yesterday's driver when it is off."""

    def test_discovery_is_off_by_default(self):
        """A flag that quietly costs money is the wrong default.

        Pinned on the signature rather than by reading the CLI, because the
        default is what every existing caller — the Temporal activity included —
        silently gets.
        """
        self.assertIsNone(inspect.signature(port).parameters["discovery"].default)

    def test_without_the_flag_the_driver_behaves_exactly_as_it_did(self):
        """The deterministic path is the default and nothing about it moved.

        Same stop, same stage, same escalation as before this stage existed, and
        no discovery artifact to suggest something was tried.
        """
        fixture = self.agent_fixture()
        result = self.run_port(fixture, [CONTEXT_HOOK, AGENT_HOOK])
        self.assertIs(result.ok, False)
        self.assertEqual(result.stage_reached, "resolve")
        self.assertEqual(result.escalations, (AGENT_HOOK.hook_id,))
        self.assertNotIn("discovery", result.artifacts)
        self.assertFalse((self.base / "run" / "discovery.json").exists())
        self.assertEqual(self.claims_of("proposer_agreement"), [])

    def test_discovery_only_runs_for_a_hook_that_is_missing_a_HOST(self):
        """Not for every escalation: the narrow question is the whole point.

        `agent_cost` files an escalation as needing a host, a capture or a whole
        patch, decided structurally. Discovery answers exactly the first, and the
        two stages must agree about which that is — a hook whose host is known
        and whose payload needs a value no anchor can bind is not a question k
        agents can be asked "which class".
        """
        from dfinsta_pipeline import agent_cost
        from dfinsta_pipeline.discovery import needs_a_host

        fixture = self.agent_fixture()
        report = self.resolve([CONTEXT_HOOK, AGENT_HOOK, MISSING_HOOK], fixture)
        for item in report.resolutions:
            with self.subTest(hook=item.hook_id):
                needs, _ = agent_cost._needs(item) if item.outcome is Outcome.NEEDS_AGENT else ((), "")
                self.assertIs(
                    needs_a_host(item),
                    agent_cost.NEED_HOST in needs,
                    f"{item.hook_id} is {item.outcome.value}: the two stages disagree "
                    "about whether an agent is being asked for a host",
                )
        # And concretely: the by_agent hook yes, the missing named host no.
        by_id = {item.hook_id: item for item in report.resolutions}
        self.assertIs(needs_a_host(by_id[AGENT_HOOK.hook_id]), True)
        self.assertIs(needs_a_host(by_id[MISSING_HOOK.hook_id]), False)
        self.assertIs(needs_a_host(by_id[CONTEXT_HOOK.hook_id]), False)


class DiscoveryAgreementTests(DiscoveryCase):
    """k proposers agree, a verifier fails to break it, and the hook resolves."""

    def test_two_of_three_agreeing_resolves_the_hook(self):
        """The holdout that justified building this was 2 of 3, not 3 of 3.

        Two proposers reached the hard settings host and the third failed
        outright, so unanimity is the wrong bar; what `host_agreement` requires
        is two DISTINCT proposers in the winning group, which is the condition a
        single confidently-wrong agent cannot satisfy.
        """
        agents = Agents(
            {
                "proposer-1": host_answer(SETTINGS),
                "proposer-2": host_answer(WRONG),
                "proposer-3": host_answer(SETTINGS),
                "verifier-1": verdict(refuted=False),
            }
        )
        result = self.run_discovery(agents)
        self.assertIs(result.ok, True, result.stopped_because)
        self.assertEqual(result.stage_reached, "build")
        self.assertEqual(self.discovery_json()["hosts"], {AGENT_HOOK.hook_id: SETTINGS})

        resolution = json.loads((self.base / "run" / "resolution.json").read_text())
        settled = {item["hook_id"]: item for item in resolution["resolutions"]}
        self.assertEqual(settled[AGENT_HOOK.hook_id]["outcome"], "resolved")
        self.assertEqual(settled[AGENT_HOOK.hook_id]["descriptor"], SETTINGS)

    def test_the_agreement_claim_is_filed_and_says_which_question_it_answers(self):
        """The evidence, not only the outcome: an outcome can be right for the wrong reason.

        A host agreement judged by the whole-patch shape is scored on an anchor
        nobody asked for and comes back `not_exercised` — a clean agreement filed
        as no agreement at all — so `asked` is checked, not just the verdict.
        """
        self.run_discovery(agreeing())
        claims = self.claims_of("proposer_agreement")
        self.assertEqual(len(claims), 1)
        claim = claims[0]
        self.assertEqual(claim["hook_id"], AGENT_HOOK.hook_id)
        self.assertEqual(claim["verdict"], "passed")
        self.assertEqual(claim["producer"], "statistics")
        self.assertEqual(claim["detail"]["asked"], "host")
        self.assertEqual(claim["detail"]["agreed"], 3)
        self.assertEqual(claim["detail"]["proposals"], 3)
        self.assertEqual(claim["detail"]["distinct_answers"], 1)
        # Produced by something that is not the proposer, which is the whole
        # reason the ledger distinguishes producers at all.
        self.assertNotIn("proposer", claim["actor"])

    def test_the_refutation_is_filed_as_its_own_claim(self):
        """`adversarial_verified` may only come from a verifier agent.

        It is the check that caught a shipped inert hook after three agreeing
        proposers and every static check said to ship it, so a run that agreed
        and never filed it must not read like one that survived review.
        """
        self.run_discovery(agreeing())
        claims = self.claims_of("adversarial_verified")
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["verdict"], "passed")
        self.assertEqual(claims[0]["producer"], "verifier_agent")
        self.assertEqual(claims[0]["actor"], "verifier-1")
        self.assertIn("could not break it", claims[0]["summary"])

    def test_the_gate_passes_only_because_all_four_pre_apply_items_exist(self):
        """A bare `--proposals` host reaches the same class and still stops here.

        The difference is entirely the two claims discovery files, so this is the
        assertion that discovery earned the pass rather than skipped the check.
        """
        self.run_discovery(agreeing())
        readiness = json.loads((self.base / "run" / "readiness.json").read_text())
        self.assertIs(readiness["complete"], True)
        statuses = {
            status["kind"]: status
            for status in readiness["hooks"][AGENT_HOOK.hook_id]["statuses"]
        }
        self.assertEqual(
            set(statuses),
            {"anchor_unique", "registers_safe", "adversarial_verified", "proposer_agreement"},
        )
        for kind, status in statuses.items():
            with self.subTest(kind=kind):
                self.assertIs(status["satisfied"], True)

    def test_the_hook_is_still_held_to_the_post_build_items(self):
        """Discovery answers "which class". It says nothing about whether it works."""
        self.run_discovery(agreeing())
        self.assertIn("not release-ready", self.printed)
        self.assertIn("post-build evidence", self.printed)


class DiscoveryIndependenceTests(DiscoveryCase):
    """What the agents are and are not shown, and that they are shown it at once."""

    def test_every_proposer_gets_the_same_prompt_and_no_other_answer(self):
        """Independence is a property of this loop, not of the agents.

        A runtime that threads one session through k invocations conditions
        proposer k on proposers 1..k-1, and that surfaces as *agreement* rather
        than as an error. Running them concurrently must not reintroduce it.
        """
        agents = Agents(
            {
                "proposer-1": host_answer(SETTINGS),
                "proposer-2": host_answer(WRONG),
                "proposer-3": host_answer(SETTINGS),
                "verifier-1": verdict(refuted=False),
            }
        )
        self.run_discovery(agents)
        asked = agents.prompts("proposer")
        self.assertEqual(len(asked), 3)
        self.assertEqual(len(set(asked)), 1)
        for prompt in asked:
            self.assertNotIn(SETTINGS, prompt)
            self.assertNotIn(WRONG, prompt)
        # Positive control: a descriptor CAN reach a prompt — the verifier is
        # shown one — so the absence above is a property of the proposer prompt
        # and not of a search that could never have succeeded.
        self.assertIn(SETTINGS, agents.prompts("verifier")[0])

    def test_no_proposer_is_ever_asked_twice(self):
        """A retried agent is a correlated one, and correlation reads as agreement."""
        agents = Agents(
            {
                "proposer-1": host_answer(SETTINGS),
                "proposer-2": "I could not work it out.",
                "proposer-3": RuntimeError("the agent runtime died"),
                "verifier-1": verdict(refuted=False),
            }
        )
        self.run_discovery(agents)
        for name, agent in agents.built:
            with self.subTest(agent=name):
                self.assertEqual(len(agent.prompts), 1)

    def test_the_verifier_sees_the_claim_and_never_the_rationale(self):
        """A verifier shown a fluent justification agrees with it.

        One holdout proposer justified a correct answer with a fabricated claim
        about register state; a reviewer reading both would have been reassured
        by exactly the wrong thing.
        """
        agents = agreeing()
        self.run_discovery(agents)
        checks = agents.prompts("verifier")
        self.assertEqual(len(checks), 1)
        self.assertIn(SETTINGS, checks[0])
        self.assertIn("REFUTE", checks[0])
        self.assertNotIn("listed the tree", checks[0])
        self.assertNotIn("followed the field", checks[0])
        # Positive control: the rationale exists and was carried this far, so
        # its absence above is a decision rather than an empty search.
        recorded = self.discovery_json()["hooks"][0]["run"]["proposals"][0]["evidence"]
        self.assertIn("listed the tree", recorded)

    def test_the_k_proposers_run_at_the_same_time(self):
        """Each answer takes minutes; in sequence a run costs k times what it needs to.

        Proved by making every proposer wait for the others: run one after
        another the barrier is never filled and the wait times out, so this
        cannot pass on a sequential loop.
        """
        barrier = threading.Barrier(3)
        agents = Agents(
            {
                "proposer-1": host_answer(SETTINGS),
                "proposer-2": host_answer(SETTINGS),
                "proposer-3": host_answer(SETTINGS),
                "verifier-1": verdict(refuted=False),
            },
            before=lambda: barrier.wait(timeout=10),
        )
        result = self.run_discovery(agents)
        self.assertIs(result.ok, True, result.stopped_because)
        self.assertEqual(self.discovery_json()["hosts"], {AGENT_HOOK.hook_id: SETTINGS})


class DiscoverySandboxTests(DiscoveryCase):
    """The answers must be physically absent, not merely forbidden."""

    def test_the_agents_are_pointed_at_the_sandbox_and_not_at_the_decode(self):
        """Forbidding a path is not removing it.

        This repository's own history holds the resolved anchor for every version
        ported so far, so a proposer working in the real tree is answering from
        the answer key. It gets a hardlinked copy with nothing else reachable
        from it.
        """
        agents = agreeing()
        fixture = self.agent_fixture()
        self.run_discovery(agents, fixture=fixture)
        sandbox = self.discovery_json()["sandbox"]
        self.assertTrue(sandbox)
        self.assertNotIn(str(driver.REPOSITORY), sandbox)
        for prompt in agents.prompts():
            self.assertIn(str(self.base / "sandbox-root"), prompt)
            self.assertNotIn(str(fixture.decode), prompt)

    def test_one_sandbox_serves_every_agent_and_is_removed_afterwards(self):
        """One per run, not one per agent, and not left behind."""
        agents = agreeing()
        self.run_discovery(agents)
        self.assertEqual(len(set(agents.sandboxes)), 1)
        self.assertEqual(len(agents.sandboxes), 4)
        self.assertFalse((self.base / "sandbox-root").exists())

    def test_a_sandbox_root_inside_the_repository_is_refused(self):
        """`build_sandbox` refuses it and the driver does not work around it."""
        agents = agreeing()
        settings = self.discover(agents, sandbox_root=driver.REPOSITORY / "work" / "sandbox")
        with self.assertRaises(DriverError) as caught:
            self.run_discovery(agents, settings=settings)
        self.assertIn("does not remove the answers", str(caught.exception))
        self.assertEqual(agents.calls, 0)

    def test_a_missing_agent_runtime_stops_before_anything_is_spent(self):
        """"No agent ran" and "the agents found nothing" mean opposite things.

        Without this the run hardlinks the decode, drops k proposers that each
        failed to start, and files a claim saying no proposals were produced —
        a runtime failure recorded as a finding about the app.
        """
        from dfinsta_pipeline import discovery as discovery_module

        original = discovery_module.find_spec
        self.addCleanup(setattr, discovery_module, "find_spec", original)
        discovery_module.find_spec = lambda name: None

        fixture = self.agent_fixture()
        settings = Discovery(version="439", sandbox_root=self.base / "unused")
        with self.assertRaises(DriverError) as caught:
            self.run_port(
                fixture,
                [CONTEXT_HOOK, AGENT_HOOK],
                discovery=settings,
                version="439",
                recorded_at=STAMP,
            )
        self.assertIn("no agent runtime", str(caught.exception))
        self.assertIn("not a finding about", str(caught.exception))
        self.assertFalse((self.base / "unused").exists())

    def test_an_injected_runner_needs_no_runtime_at_all(self):
        """The preflight must not fire for a caller that brought its own runner,
        or the suite would depend on a model, an API key and the `claude` CLI."""
        from dfinsta_pipeline import discovery as discovery_module

        original = discovery_module.find_spec
        self.addCleanup(setattr, discovery_module, "find_spec", original)
        discovery_module.find_spec = lambda name: None

        result = self.run_discovery(agreeing())
        self.assertIs(result.ok, True, result.stopped_because)

    def test_a_root_that_already_exists_is_refused_rather_than_reused(self):
        """A stale sandbox may hold an answer, so reuse is not an optimisation."""
        (self.base / "used").mkdir()
        (self.base / "used" / "someone-elses.txt").write_text("keep me", encoding="utf-8")
        agents = agreeing()
        with self.assertRaises(DriverError) as caught:
            self.run_discovery(
                agents, settings=self.discover(agents, sandbox_root=self.base / "used")
            )
        self.assertIn("refusing to reuse", str(caught.exception))
        self.assertEqual(agents.calls, 0)
        # And the refusal did not eat the directory it refused. Cleaning up after
        # a root this run never created turns a safety refusal into data loss,
        # which is worse than the reuse it was refusing.
        self.assertTrue((self.base / "used" / "someone-elses.txt").exists())


class DiscoveryStallTests(DiscoveryCase):
    """Disagreement and refutation both stop the run, by different doors."""

    def test_disagreeing_proposers_force_no_host_at_all(self):
        """Three answers, three classes: that is a finding for a human.

        Nothing here breaks the tie by ranking, so no host reaches the resolver
        and the hook stays exactly as escalated as it was — while the measured
        disagreement is on disk where the next person can read it.
        """
        agents = Agents(
            {
                "proposer-1": host_answer(SETTINGS),
                "proposer-2": host_answer(WRONG),
                "proposer-3": host_answer(ALSO_WRONG),
                "verifier-1": verdict(refuted=False),
            }
        )
        result = self.run_discovery(agents)
        self.assertIs(result.ok, False)
        self.assertEqual(result.escalations, (AGENT_HOOK.hook_id,))
        self.assertEqual(self.discovery_json()["hosts"], {})

        resolution = json.loads((self.base / "run" / "resolution.json").read_text())
        settled = {item["hook_id"]: item for item in resolution["resolutions"]}
        self.assertEqual(settled[AGENT_HOOK.hook_id]["outcome"], "needs_agent")
        self.assertIsNone(settled[AGENT_HOOK.hook_id]["descriptor"])
        # Nothing was composed and nothing was built.
        self.assertFalse((self.base / "run" / "patch-source").exists())
        self.assertEqual(self.commands, [])

    def test_the_disagreement_is_filed_as_evidence_rather_than_as_silence(self):
        """Absence is never a pass, so the measurement is recorded either way."""
        agents = Agents(
            {
                "proposer-1": host_answer(SETTINGS),
                "proposer-2": host_answer(WRONG),
                "proposer-3": host_answer(ALSO_WRONG),
                "verifier-1": verdict(refuted=False),
            }
        )
        self.run_discovery(agents)
        claims = self.claims_of("proposer_agreement")
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["verdict"], "inconclusive")
        self.assertEqual(claims[0]["detail"]["distinct_answers"], 3)
        self.assertEqual(claims[0]["detail"]["agreed"], 1)

    def test_a_refuting_verifier_stalls_the_hook_at_the_gate(self):
        """The agreed class is put forward; the objection is what stops it.

        Deliberately not "drop the host and report nothing": the human gets a
        readiness report naming the class AND the finding, which is strictly more
        than an unresolved hook tells them. What the gate will not do is let a
        failed `adversarial_verified` claim through.
        """
        result = self.run_discovery(agreeing(refuted=True))
        self.assertIs(result.ok, False)
        self.assertEqual(result.stage_reached, "gate")
        self.assertEqual(result.escalations, (AGENT_HOOK.hook_id,))

        readiness = json.loads((self.base / "run" / "readiness.json").read_text())
        statuses = {
            status["kind"]: status
            for status in readiness["hooks"][AGENT_HOOK.hook_id]["statuses"]
        }
        self.assertEqual(statuses["adversarial_verified"]["verdict"], "failed")
        self.assertIs(statuses["proposer_agreement"]["satisfied"], True)
        self.assertFalse((self.base / "run" / "patch-source").exists())
        self.assertEqual(self.commands, [])

    def test_a_refuted_hook_does_not_hide_which_class_was_agreed(self):
        """The finding is only actionable next to the claim it refutes."""
        self.run_discovery(agreeing(refuted=True))
        self.assertEqual(self.discovery_json()["hosts"], {AGENT_HOOK.hook_id: SETTINGS})
        self.assertEqual(
            self.discovery_json()["hooks"][0]["refuted_by"], ["verifier-1"]
        )
        failed = self.claims_of("adversarial_verified")
        self.assertEqual(failed[0]["verdict"], "failed")

    def test_a_verifier_that_produces_nothing_usable_has_not_cleared_anything(self):
        """"I could not check" must not read as "it is fine"."""
        agents = Agents(
            {
                "proposer-1": host_answer(SETTINGS),
                "proposer-2": host_answer(SETTINGS),
                "proposer-3": host_answer(SETTINGS),
                "verifier-1": "I ran out of turns.",
            }
        )
        result = self.run_discovery(agents)
        self.assertIs(result.ok, False)
        self.assertEqual(result.stage_reached, "gate")
        self.assertEqual(self.claims_of("adversarial_verified")[0]["verdict"], "failed")

    def test_no_proposal_at_all_leaves_the_hook_where_it_was(self):
        """Every proposer failing is a smaller sample, never a smaller requirement."""
        agents = Agents(
            {
                "proposer-1": "no json here",
                "proposer-2": RuntimeError("runtime died"),
                "proposer-3": "still no json",
                "verifier-1": verdict(refuted=False),
            }
        )
        result = self.run_discovery(agents)
        self.assertIs(result.ok, False)
        self.assertEqual(result.escalations, (AGENT_HOOK.hook_id,))
        agreement = self.claims_of("proposer_agreement")[0]
        self.assertEqual(agreement["verdict"], "not_exercised")
        # No proposal parsed, so the expensive adversarial check was never spent.
        self.assertEqual(agents.prompts("verifier"), [])
        self.assertEqual(self.claims_of("adversarial_verified"), [])


class DiscoveryDroppedProposerTests(DiscoveryCase):
    """A malformed answer is dropped, never repaired, and k-1 still decides."""

    def test_an_unparseable_proposer_is_dropped_and_the_other_two_decide(self):
        """One agent inventing a schema field is what happened on the first real run.

        The other two answers were the measurement, and a run that discarded them
        would have thrown away the result to protect a formality.
        """
        agents = Agents(
            {
                "proposer-1": host_answer(SETTINGS),
                "proposer-2": host_answer(SETTINGS, confidence=0.9),
                "proposer-3": host_answer(SETTINGS),
                "verifier-1": verdict(refuted=False),
            }
        )
        result = self.run_discovery(agents)
        self.assertIs(result.ok, True, result.stopped_because)
        self.assertEqual(self.discovery_json()["hosts"], {AGENT_HOOK.hook_id: SETTINGS})

        agreement = self.claims_of("proposer_agreement")[0]
        self.assertEqual(agreement["detail"]["proposals"], 2)
        self.assertEqual(agreement["detail"]["agreed"], 2)
        self.assertEqual(agreement["verdict"], "passed")

    def test_the_drop_is_recorded_with_the_reason(self):
        """A dropped agent and an agent that was never run must not look the same."""
        agents = Agents(
            {
                "proposer-1": host_answer(SETTINGS),
                "proposer-2": "I could not work it out.",
                "proposer-3": host_answer(SETTINGS),
                "verifier-1": verdict(refuted=False),
            }
        )
        self.run_discovery(agents)
        failures = self.discovery_json()["hooks"][0]["run"]["failures"]
        self.assertEqual(len(failures), 1)
        self.assertIn("proposer-2", failures[0])
        self.assertIn("ProposalError", failures[0])
        self.assertIn("proposer-2", self.printed)

    def test_a_single_surviving_answer_cannot_corroborate_itself(self):
        """k-1 is a smaller sample; k-2 leaves one voice, and one voice is not agreement."""
        agents = Agents(
            {
                "proposer-1": host_answer(SETTINGS),
                "proposer-2": "no json",
                "proposer-3": "no json either",
                "verifier-1": verdict(refuted=False),
            }
        )
        result = self.run_discovery(agents)
        self.assertIs(result.ok, False)
        self.assertEqual(self.discovery_json()["hosts"], {})
        self.assertEqual(self.claims_of("proposer_agreement")[0]["verdict"], "inconclusive")


class DiscoveryBudgetTests(DiscoveryCase):
    """What a run may spend, and what it must say when it stops spending."""

    def two_agent_hooks(self) -> Fixture:
        return self.make_decode(
            {
                SHELL: ("smali", CLEAN_SHELL),
                SETTINGS: ("smali_classes3", listener_class(SETTINGS)),
                SETTINGS_TWO: ("smali_classes3", listener_class(SETTINGS_TWO)),
            },
            extra_trees=("smali_classes2",),
            name="two-agent",
        )

    def test_the_cap_binds_and_says_so(self):
        """Silent truncation reads as "covered everything", which is the one lie a
        budget report must not tell."""
        agents = agreeing()
        settings = self.discover(agents, max_agent_calls=4)
        result = self.run_discovery(
            agents,
            hooks=(CONTEXT_HOOK, AGENT_HOOK, AGENT_HOOK_TWO),
            fixture=self.two_agent_hooks(),
            settings=settings,
        )
        self.assertIs(result.ok, False)
        report = self.discovery_json()
        self.assertIs(report["cap_bound"], True)
        self.assertEqual(report["skipped"], [AGENT_HOOK_TWO.hook_id])
        self.assertEqual(report["spent"], 4)
        self.assertIn("cap", report["notice"])
        # Said in the output, and in the sentence the caller prints.
        self.assertIn("SKIPPED", self.printed)
        self.assertIn("cap", result.stopped_because)

    def test_a_skipped_hook_is_not_reported_as_a_finding_about_the_app(self):
        """A hook the budget never reached is a budget stop, not a disagreement.

        Filing it as one would make the next person go looking for an ambiguity
        in the app that nobody ever measured.
        """
        agents = agreeing()
        self.run_discovery(
            agents,
            hooks=(CONTEXT_HOOK, AGENT_HOOK, AGENT_HOOK_TWO),
            fixture=self.two_agent_hooks(),
            settings=self.discover(agents, max_agent_calls=4),
        )
        skipped = [
            item for item in self.discovery_json()["hooks"] if not item["attempted"]
        ]
        self.assertEqual(len(skipped), 1)
        self.assertIsNone(skipped[0]["run"])
        self.assertIn("not attempted", skipped[0]["reason"])
        # Nothing was recorded about it, because nothing was measured about it.
        self.assertEqual(
            [claim for claim in self.evidence_claims()
             if claim["hook_id"] == AGENT_HOOK_TWO.hook_id],
            [],
        )

    def test_a_hook_is_attempted_whole_or_not_at_all(self):
        """Running 2 of 3 proposers and calling it "no agreement" reports a budget
        stop as a finding, so a hook the cap cannot fully cover is not started."""
        agents = agreeing()
        self.run_discovery(
            agents,
            hooks=(CONTEXT_HOOK, AGENT_HOOK, AGENT_HOOK_TWO),
            fixture=self.two_agent_hooks(),
            settings=self.discover(agents, max_agent_calls=6),
        )
        # 6 would cover one hook (4) and two thirds of the next. It covers one.
        self.assertEqual(self.discovery_json()["spent"], 4)
        self.assertEqual(agents.calls, 4)

    def test_the_default_cap_covers_the_two_ui_hooks_and_no_more(self):
        """The plan's rule is at most two generations per unresolved intent."""
        from dfinsta_pipeline.discovery import (
            DEFAULT_K,
            DEFAULT_MAX_AGENT_CALLS,
            DEFAULT_VERIFIERS,
        )

        self.assertEqual(DEFAULT_MAX_AGENT_CALLS, 2 * (DEFAULT_K + DEFAULT_VERIFIERS))

    def test_a_cap_that_could_never_run_a_hook_is_refused_up_front(self):
        """Every hook reported as skipped and no agent ever run is not a budget."""
        with self.assertRaises(ValueError) as caught:
            Discovery(version="439", k=3, verifiers=1, max_agent_calls=3)
        self.assertIn("every hook would be reported as skipped", str(caught.exception))

    def test_k_below_two_cannot_corroborate_anything(self):
        with self.assertRaises(ValueError) as caught:
            Discovery(version="439", k=1)
        self.assertIn("cannot corroborate", str(caught.exception))

    def test_discovery_needs_at_least_one_verifier(self):
        with self.assertRaises(ValueError) as caught:
            Discovery(version="439", verifiers=0)
        self.assertIn("adversarial verifier", str(caught.exception))

    def test_the_agreement_bar_is_a_share_and_is_not_a_command_line_flag(self):
        """Lowering the bar from a command line, at the moment a run refuses, is
        how a gate gets defeated — and it would leave a claim saying they agreed."""
        for bad in (0, -0.5, 1.5):
            with self.subTest(threshold=bad):
                with self.assertRaises(ValueError):
                    Discovery(version="439", threshold=bad)
        flags = io.StringIO()
        with contextlib.redirect_stdout(flags):
            with self.assertRaises(SystemExit):
                main(["--help"])
        self.assertNotIn("threshold", flags.getvalue())


class CostRecordingTests(DiscoveryCase):
    """Stage 10: what was spent is spent whether or not the port resolved.

    Including when the run *raised*. What a port cost is settled at resolve time
    and does not depend on anything downstream succeeding: two real Instagram 440
    runs resolved all seven hooks mechanically for zero agent invocations and
    recorded nothing at all, because the build failed afterwards on an
    apktool/aapt1 manifest incompatibility that has nothing to do with cost. A
    metric that only records successful ports cannot show a port getting cheaper
    while it is still getting harder to build.

    The receipt is deliberately narrow, and each half is pinned separately below:
    the error still leaves `port` (it is a receipt, not a rescue), a run with no
    version label still records nothing, and a failure that happened before a
    resolution report existed records nothing either — there is no cost to file
    when nothing was resolved.
    """

    def recorder(self) -> list[tuple]:
        calls: list[tuple] = []
        original = driver.record_run
        self.addCleanup(setattr, driver, "record_run", original)
        driver.record_run = lambda *args, **kwargs: calls.append((args, kwargs))
        return calls

    def fail_the_build(self, code: int = 1) -> None:
        """Make the builder subprocess fail, at the seam `setUp` already stubs.

        The 440 failure was apktool exiting non-zero, so this raises exactly what
        the real `run_command` raises for that — a `DriverError` carrying no
        report, because `run_command` has never seen one. Attaching the report is
        the build stage's job, which is the thing under test.
        """

        def failing(command: Sequence[Any], label: str) -> None:
            self._record_command(command, label)
            if label == "build":
                raise DriverError(f"{label} failed with exit code {code}")

        driver.run_command = failing

    def test_record_run_is_called_once_with_the_callers_timestamp(self):
        """Nothing in that layer reads a clock, so a replay rewrites the same line."""
        calls = self.recorder()
        fixture = self.three_dex_fixture()
        result = self.run_port(
            fixture, [CONTEXT_HOOK, ACTION_BAR_HOOK], version="439", recorded_at=STAMP
        )
        self.assertEqual(len(calls), 1)
        (report, version, stamp), kwargs = calls[0]
        self.assertIs(report, result.report)
        self.assertEqual(version, "439")
        self.assertEqual(stamp, STAMP)
        self.assertEqual(kwargs["memory_path"], self.base / "decisions.jsonl")
        self.assertEqual(kwargs["ledger_path"], self.base / "agent_cost.jsonl")

    def test_the_records_actually_reach_the_two_files(self):
        """A call made and a file not written proves nothing, and looks identical."""
        fixture = self.three_dex_fixture()
        self.run_port(
            fixture, [CONTEXT_HOOK, ACTION_BAR_HOOK], version="439", recorded_at=STAMP
        )
        ledger = self.base / "agent_cost.jsonl"
        self.assertTrue(ledger.exists())
        records = [json.loads(line) for line in ledger.read_text().splitlines() if line]
        self.assertEqual({record["kind"] for record in records}, {"hook_cost"})
        self.assertEqual(
            {record["record"]["hook_id"] for record in records},
            {CONTEXT_HOOK.hook_id, ACTION_BAR_HOOK.hook_id},
        )
        for record in records:
            self.assertEqual(record["record"]["recorded_at"], STAMP)
            self.assertEqual(record["record"]["version"], "439")
        self.assertTrue((self.base / "decisions.jsonl").exists())

    def test_a_blocked_port_still_records_what_it_spent(self):
        """A port that recorded what it learned and forgot what it paid is the state
        the cost ledger exists to end, and a blocked port is where it paid most."""
        calls = self.recorder()
        fixture = self.agent_fixture()
        result = self.run_port(
            fixture, [CONTEXT_HOOK, AGENT_HOOK], version="439", recorded_at=STAMP
        )
        self.assertIs(result.ok, False)
        self.assertEqual(len(calls), 1)

    def test_a_discovered_host_is_recorded_as_an_agent_invocation(self):
        """The number stage 10 exists to drive down is agent invocations, so the
        route a discovered hook took must not read as mechanical."""
        self.run_discovery(agreeing())
        records = [
            json.loads(line)["record"]
            for line in (self.base / "agent_cost.jsonl").read_text().splitlines()
            if line
        ]
        by_hook = {record["hook_id"]: record for record in records}
        self.assertEqual(by_hook[AGENT_HOOK.hook_id]["route"], "agent_proposal")
        self.assertEqual(by_hook[AGENT_HOOK.hook_id]["agent_for"], ["host"])
        self.assertEqual(by_hook[CONTEXT_HOOK.hook_id]["route"], "mechanical")

    def test_a_run_with_no_version_records_nothing_and_says_so(self):
        """Refused rather than guessed: decision memory is keyed by (hook, version)
        and half a key files a record nothing can retrieve."""
        calls = self.recorder()
        fixture = self.three_dex_fixture()
        self.run_port(fixture, [CONTEXT_HOOK, ACTION_BAR_HOOK])
        self.assertEqual(calls, [])
        self.assertIn("nothing recorded", self.printed)
        self.assertFalse((self.base / "agent_cost.jsonl").exists())

    def test_a_version_with_no_timestamp_is_refused(self):
        fixture = self.three_dex_fixture()
        with self.assertRaises(DriverError) as caught:
            self.run_port(fixture, [CONTEXT_HOOK], version="439")
        self.assertIn("--recorded-at", str(caught.exception))

    def test_discovery_without_a_version_is_refused_before_any_agent_runs(self):
        """Discovery is the expensive route. Spending without recording what was
        spent is exactly the state the ledger exists to end."""
        agents = agreeing()
        fixture = self.agent_fixture()
        with self.assertRaises(DriverError) as caught:
            self.run_port(
                fixture,
                [CONTEXT_HOOK, AGENT_HOOK],
                discovery=self.discover(agents),
                recorded_at=STAMP,
            )
        self.assertIn("--version", str(caught.exception))
        self.assertEqual(agents.calls, 0)

    def test_the_stage_ten_stores_default_to_the_committed_paths(self):
        """They outlive the run directory on purpose: a trend measured into a
        temporary directory is a trend nobody can compare against."""
        paths = RunPaths(self.base / "run")
        self.assertEqual(paths.cost_ledger, driver.REPOSITORY / "manifest" / "agent_cost.jsonl")
        self.assertEqual(
            paths.decision_memory, driver.REPOSITORY / "manifest" / "decisions.jsonl"
        )

    # ------------------------------------------------- the failed-run receipt

    def test_a_driver_error_carries_no_report_unless_one_is_given(self):
        """The attribute is always there and is `None` until a stage attaches one.

        Every `raise DriverError(...)` in the module predates the report and none
        of them were touched, so `error.report` has to be safe to read on all of
        them — `port` reads it on the way out of any failure at all.
        """
        plain = DriverError("x")
        self.assertIsNone(plain.report)
        self.assertEqual(plain.args, ("x",))

        report = {"resolutions": []}
        carried = DriverError("x", report=report)
        self.assertIs(carried.report, report)
        # The message is untouched: the build stage re-raises as
        # `DriverError(*error.args, report=report)`, and a report that landed in
        # `args` would rewrite what the CLI prints as the reason the run failed.
        self.assertEqual(carried.args, ("x",))
        self.assertEqual(str(carried), "x")
        # Keyword-only, so a second positional argument is a second message and
        # never a silently accepted report.
        self.assertIsNone(DriverError("x", report).report)

    def test_a_failing_build_carries_the_resolution_report_out_of_the_stages(self):
        """The build stage attaches what resolve produced; `run_command` cannot.

        This is the whole mechanism: the cost is known at stage 3 and the failure
        happens at stage 6, so unless the report rides out on the exception there
        is nothing left to record by the time anyone can catch it.
        """
        self.fail_the_build()
        fixture = self.three_dex_fixture()
        with self.assertRaises(DriverError) as caught:
            # No version, so `port` cannot have touched this error on its way
            # out: it is the object `_run_stages` raised.
            self.run_port(fixture, [CONTEXT_HOOK, ACTION_BAR_HOOK])
        self.assertIn("build failed with exit code 1", str(caught.exception))
        self.assertIn("build", [label for label, _ in self.commands])

        report = caught.exception.report
        self.assertIsNotNone(report, "the resolution report did not survive the failure")
        self.assertEqual(
            [item.hook_id for item in report.resolutions],
            [CONTEXT_HOOK.hook_id, ACTION_BAR_HOOK.hook_id],
        )

    def test_a_failed_build_records_what_the_port_cost_and_still_raises(self):
        """Both halves, in one test, because either alone is the wrong behaviour.

        Recorded and swallowed is a pipeline that reports a broken build as a
        finished port. Raised and unrecorded is the 440 state this exists to end.
        It is a receipt, not a rescue.
        """
        self.fail_the_build()
        fixture = self.three_dex_fixture()
        ledger = self.base / "agent_cost.jsonl"

        with self.assertRaises(DriverError) as caught:
            self.run_port(
                fixture,
                [CONTEXT_HOOK, ACTION_BAR_HOOK],
                version="440",
                recorded_at=STAMP,
            )
        self.assertIn("build failed with exit code 1", str(caught.exception))

        self.assertTrue(ledger.exists(), "the run cost what it cost and filed nothing")
        records = [
            json.loads(line)["record"] for line in ledger.read_text().splitlines() if line
        ]
        self.assertEqual({record["version"] for record in records}, {"440"})
        self.assertEqual(
            {record["hook_id"] for record in records},
            {CONTEXT_HOOK.hook_id, ACTION_BAR_HOOK.hook_id},
        )
        for record in records:
            self.assertEqual(record["recorded_at"], STAMP)
            self.assertIs(record["needed_agent"], False)
        self.assertTrue((self.base / "decisions.jsonl").exists())
        self.assertIn("recorded what this failed run cost", self.printed)

    def test_a_failed_build_with_no_version_records_nothing_and_still_raises(self):
        """Half a key files a record nothing can retrieve, failed run or not.

        Both stage-10 stores are keyed by (hook, version), so an unlabelled run
        is the one case where recording is worse than not recording — and the
        failure is still reported either way.
        """
        self.fail_the_build()
        fixture = self.three_dex_fixture()
        with self.assertRaises(DriverError) as caught:
            self.run_port(fixture, [CONTEXT_HOOK, ACTION_BAR_HOOK])
        self.assertIn("build failed with exit code 1", str(caught.exception))
        self.assertFalse((self.base / "agent_cost.jsonl").exists())
        self.assertFalse((self.base / "decisions.jsonl").exists())
        self.assertNotIn("[cost]", self.printed)

    def test_a_completed_run_and_a_failed_one_each_record_exactly_once(self):
        """The receipt is an extra call site, so the success path must not change.

        A second write per port would double every count the ledger reports, and
        the trend it exists to show — agent invocations per port — is a count.
        """
        calls = self.recorder()
        fixture = self.three_dex_fixture()
        hooks = [CONTEXT_HOOK, ACTION_BAR_HOOK]

        result = self.run_port(
            fixture, hooks, version="440", recorded_at=STAMP, out="built"
        )
        self.assertIs(result.ok, True, result.stopped_because)
        self.assertEqual(result.stage_reached, "build")
        self.assertEqual(len(calls), 1, "the completed run recorded more than once")

        self.fail_the_build()
        with self.assertRaises(DriverError):
            self.run_port(
                fixture, hooks, version="440", recorded_at=STAMP, out="failed"
            )
        self.assertEqual(
            len(calls), 2, "the failed run recorded twice, or did not record at all"
        )

    def test_a_failure_before_the_report_exists_records_nothing(self):
        """The receipt covers only stages that got as far as resolving.

        A run that stopped at extract resolved nothing, so there is no cost to
        file — and `record_run` would be handed a report that does not exist.
        Recording here would file a port that never happened under a version
        label, which is the trend reading its own noise.
        """
        paths = RunPaths(
            self.base / "early",
            memory_path=self.base / "decisions.jsonl",
            ledger_path=self.base / "agent_cost.jsonl",
        )
        # Nothing is reused, so stage 1 extracts — into a directory that is
        # already there, which it refuses rather than overwrite.
        paths.analysis_decode.mkdir(parents=True)

        stream = io.StringIO()
        with self.assertRaises(DriverError) as caught:
            with contextlib.redirect_stdout(stream):
                port(
                    apk=self.base / "stock.apk",
                    paths=paths,
                    hooks=[CONTEXT_HOOK],
                    apktool=self.base / "apktool_2.9.3.jar",
                    framework_apk=self.base / "framework-res.apk",
                    custom_code=self.custom_code,
                    version="440",
                    recorded_at=STAMP,
                )
        self.assertIn("refusing to overwrite", str(caught.exception))
        self.assertIsNone(caught.exception.report)
        self.assertFalse((self.base / "agent_cost.jsonl").exists())
        self.assertFalse((self.base / "decisions.jsonl").exists())
        self.assertNotIn("[cost]", stream.getvalue())
        # And it refused before shelling out to anything.
        self.assertEqual(self.commands, [])


class DiscoveryCliTests(DiscoveryCase):
    """The flag surface, exercised through `main` because that is where it lives."""

    def test_the_cli_wires_every_discovery_knob_through(self):
        built: list[Discovery] = []
        original = driver.discover_hosts
        self.addCleanup(setattr, driver, "discover_hosts", original)

        def capture(report, hooks, decode, ledger, settings, skip=(), log=print):
            built.append(settings)
            return original(report, hooks, decode, ledger, settings, skip, log)

        driver.discover_hosts = capture

        fixture = self.agent_fixture()
        manifest = write_manifest(self.base / "hooks.json", [CONTEXT_HOOK, AGENT_HOOK])
        agents = agreeing()
        # The CLI builds its own `Discovery`, so the fake runner is injected at
        # the point of construction. No model, no API key, no `claude` CLI: the
        # suite must not be able to reach a network even by accident.
        original_class = driver.Discovery
        self.addCleanup(setattr, driver, "Discovery", original_class)
        driver.Discovery = lambda **kwargs: original_class(runner=agents, **kwargs)

        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(
                [
                    str(self.base / "stock.apk"),
                    "--out", str(self.base / "cli"),
                    "--manifest", str(manifest),
                    "--custom-code", str(self.custom_code),
                    "--apktool", str(self.base / "apktool.jar"),
                    "--framework-apk", str(self.base / "framework-res.apk"),
                    "--reuse-decode", str(fixture.decode),
                    "--reuse-index", str(fixture.index_dir),
                    "--stop-after", "gate",
                    "--version", "439",
                    "--recorded-at", STAMP,
                    "--decision-memory", str(self.base / "decisions.jsonl"),
                    "--cost-ledger", str(self.base / "agent_cost.jsonl"),
                    "--discover-hosts",
                    "--discover-k", "3",
                    "--discover-verifiers", "1",
                    "--discover-model", "some-model",
                    "--max-agent-calls", "6",
                    "--sandbox-root", str(self.base / "cli-sandbox"),
                ]
            )
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertEqual(len(built), 1)
        settings = built[0]
        self.assertEqual(settings.version, "439")
        self.assertEqual(settings.k, 3)
        self.assertEqual(settings.verifiers, 1)
        self.assertEqual(settings.model, "some-model")
        self.assertEqual(settings.max_agent_calls, 6)
        self.assertEqual(settings.sandbox_root, self.base / "cli-sandbox")
        self.assertIs(settings.keep_sandbox, False)

    def test_the_cli_refuses_discovery_without_a_version(self):
        fixture = self.agent_fixture()
        manifest = write_manifest(self.base / "hooks.json", [CONTEXT_HOOK, AGENT_HOOK])
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(
                [
                    str(self.base / "stock.apk"),
                    "--out", str(self.base / "cli-noversion"),
                    "--manifest", str(manifest),
                    "--reuse-decode", str(fixture.decode),
                    "--reuse-index", str(fixture.index_dir),
                    "--discover-hosts",
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("--version", stderr.getvalue())


class DiscoveryMutationTests(DiscoveryCase):
    """The same guards, re-attacked from the direction a plausible rewrite takes."""

    def test_counting_raw_proposals_would_record_a_forged_unanimity(self):
        """Mutation: hand `agreement_claim` the proposals instead of the votes.

        It reads as the same list and is not: the claim counts what it is given,
        so one agent answering three times would be filed as three independent
        proposers reaching one answer — a manufactured consensus, recorded as
        measured evidence, by an edit of one word.
        """
        from dfinsta_pipeline.discovery import file_host_evidence
        from dfinsta_pipeline.proposals import HostProposal, host_agreement
        from dfinsta_pipeline.proposer import HostRun

        repeated = [
            HostProposal(AGENT_HOOK.hook_id, "proposer-1", SETTINGS),
            HostProposal(AGENT_HOOK.hook_id, "proposer-1", SETTINGS),
            HostProposal(AGENT_HOOK.hook_id, "proposer-1", SETTINGS),
        ]
        run = HostRun(AGENT_HOOK.hook_id, tuple(repeated), ())
        agreement = host_agreement(repeated)
        ledger = EvidenceLedger(self.base / "forged.jsonl")
        file_host_evidence(ledger, AGENT_HOOK.hook_id, run, agreement)

        claim = ledger.claims_for(AGENT_HOOK.hook_id, EvidenceKind.PROPOSER_AGREEMENT)[0]
        self.assertEqual(claim.detail["proposals"], 1)
        self.assertEqual(claim.detail["agreed"], 1)
        self.assertEqual(claim.verdict.value, "inconclusive")
        self.assertIs(agreement.agreed, False)

    def test_accepting_a_refuted_host_would_build_what_a_verifier_broke(self):
        """Mutation: treat `refuted` as advisory and carry on.

        The refutation is the check that caught a shipped inert hook after three
        agreeing proposers and every static check said to ship it. Today's code
        reaches the gate and stops there; a build command appearing in this test
        means the objection became a log line.
        """
        result = self.run_discovery(agreeing(refuted=True))
        self.assertEqual(result.stage_reached, "gate")
        self.assertEqual(self.commands, [])
        self.assertFalse((self.base / "run" / "dfinsta.apk").exists())

    def test_supplying_the_plurality_host_would_be_discovery_deciding_a_tie(self):
        """Mutation: when nobody agrees, take the most common answer anyway.

        With three distinct answers the "most common" is whichever sorts first,
        so this would resolve the hook on one agent's word and file an
        inconclusive agreement claim beside it — a gate refusing something the
        resolver already acted on.
        """
        agents = Agents(
            {
                "proposer-1": host_answer(SETTINGS),
                "proposer-2": host_answer(WRONG),
                "proposer-3": host_answer(ALSO_WRONG),
                "verifier-1": verdict(refuted=False),
            }
        )
        self.run_discovery(agents)
        resolution = json.loads((self.base / "run" / "resolution.json").read_text())
        settled = {item["hook_id"]: item for item in resolution["resolutions"]}
        self.assertIsNone(settled[AGENT_HOOK.hook_id]["descriptor"])
        self.assertEqual(self.discovery_json()["hosts"], {})

    def test_skipping_the_evidence_and_only_re_resolving_would_pass_the_gate_blind(self):
        """Mutation: supply the agreed host and file nothing.

        The hook would resolve, the anchor and register claims would be recorded
        by the ordinary path, and the gate would then be judging an agent-proposed
        hook on mechanical evidence alone. The two claims below are the whole
        difference between that and a reviewed answer.
        """
        self.run_discovery(agreeing())
        kinds = {claim["kind"] for claim in self.evidence_claims()
                 if claim["hook_id"] == AGENT_HOOK.hook_id}
        self.assertEqual(
            kinds,
            {"anchor_unique", "registers_safe", "proposer_agreement", "adversarial_verified"},
        )

    def test_dispatching_a_verifier_before_the_proposals_would_spend_on_nothing(self):
        """Mutation: build every runner and fire them all at once.

        The verifier's prompt does not exist until there is a claim to refute, so
        an eager batch spends a real invocation on a question nobody has yet.
        """
        agents = Agents(
            {
                "proposer-1": "no json here",
                "proposer-2": "no json here either",
                "proposer-3": "nor here",
                "verifier-1": verdict(refuted=False),
            }
        )
        self.run_discovery(agents)
        self.assertEqual(agents.prompts("verifier"), [])
        self.assertEqual(self.discovery_json()["spent"], 3)

    def test_re_registering_a_discovered_hook_would_kill_the_run(self):
        """Mutation: let `record_resolution_evidence` register it a second time.

        Discovery names the agent that actually proposed the host, so a second
        registration under a synthetic proposer both changes which evidence is
        required and disagrees about who may produce it. The ledger refuses,
        loudly, which is why the hook is passed as already registered.
        """
        self.run_discovery(agreeing())
        subjects = {
            claim["hook_id"] for claim in self.evidence_claims()
        }
        self.assertIn(AGENT_HOOK.hook_id, subjects)
        # The claim that would have been refused: an actor equal to the proposer.
        for claim in self.evidence_claims():
            if claim["hook_id"] == AGENT_HOOK.hook_id:
                self.assertNotIn(claim["actor"], {"proposer-1", "proposer-2", "proposer-3"})


class AssessStageTests(DriverCase):
    """Stage 4a's producer, which nothing scheduled until now.

    The whole downstream chain — record, raise the gate, answer it through the
    submission client, consume the rulings into the manifest — was complete and
    green while its first link was a human remembering to run
    `assessment_record record` after the driver finished. `record` had zero
    callers outside its own `main`, so a port that produced no assessment looked
    exactly like a port that produced one.
    """

    def assess_arguments(self, **overrides) -> dict[str, str]:
        arguments = {
            "--state-root": str(self.base / "state"),
            "--assessment-run-id": "driver-assess-test",
            "--actor": "tester",
            "--owner-token": "token-1",
        }
        arguments.update(overrides)
        return arguments

    def test_the_stage_records_an_assessment_a_gate_client_can_find(self):
        """The check that matters is reachability from the run id, not that a
        file appeared: the feature gate client takes a run id and nothing else.
        """
        from dfinsta_pipeline import activities, assessment_record
        from dfinsta_pipeline.feature_gate import derive_feature_gate_request

        fixture = self.three_dex_fixture()
        # A real index and the repository manifest, both read-only: a synthetic
        # fixture produces no coverage gaps at all, so `record` correctly refuses
        # and the happy path would never be exercised. Recording goes to a scratch
        # state root and the ruling store is redirected, so nothing in the
        # repository is written.
        real_index = REPOSITORY / "work" / "440-clean" / "index"
        if not (real_index / "api_surface.json").is_file():
            self.skipTest(f"no real index at {real_index}")

        # A COPY of the manifest with the url_block hook's `semantic_deps`
        # emptied, and that is the point rather than convenience. This test used
        # the shipped manifest directly, so it depended on the project still
        # having at least one unblocked consumption surface — and on 2026-08-08 a
        # real ruling blocked the last six, `candidate_ids` correctly refused "no
        # candidates", and this test broke for a reason that had nothing to do
        # with what it checks. A test whose premise is a product decision fails
        # the day the decision is made.
        manifest = json.loads(
            (REPOSITORY / "manifest" / "hooks.json").read_text(encoding="utf-8")
        )
        for entry in manifest["hooks"]:
            if entry.get("strategy") == "url_block":
                entry["semantic_deps"] = []
        manifest_path = self.base / "assess-manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        state_root = self.base / "state"
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            result = port(
                apk=self.base / "stock.apk",
                paths=RunPaths(
                    self.base / "run-assess",
                    fixture.decode,
                    real_index,
                    memory_path=self.base / "decisions.jsonl",
                    ledger_path=self.base / "agent_cost.jsonl",
                ),
                hooks=[CONTEXT_HOOK],
                apktool=self.base / "apktool_2.9.3.jar",
                framework_apk=self.base / "framework-res.apk",
                custom_code=fixture.custom_code,
                stop_after="assess",
                assessment=AssessmentRequest(
                    state_root=state_root,
                    run_id="driver-assess-test",
                    allowed_actor="tester",
                    owner_token="token-1",
                    manifest_path=manifest_path,
                    rulings_path=self.base / "rulings.jsonl",
                ),
            )
        self.printed = stream.getvalue()
        self.assertEqual(result.stage_reached, "assess")
        self.assertIn("assessment", result.artifacts)
        self.assertTrue(result.artifacts["assessment"].startswith("cas://sha256/"))
        self.assertEqual(result.artifacts["assessment_run_id"], "driver-assess-test")

        activities.configure_runtime(state_root, read_only=True)
        try:
            configured = activities.runtime()
            recorded = assessment_record.resolve_with(
                configured.ledger, configured.store, "driver-assess-test"
            )
            self.assertEqual(recorded.allowed_actor, "tester")
            request = derive_feature_gate_request(
                recorded.run_id,
                recorded.assessment,
                recorded.policy_revision,
                recorded.allowed_actor,
                recorded.candidate_ids,
            )
            self.assertEqual(request.gate_id, "driver-assess-test-feature-assessment-gate")
        finally:
            activities._runtime = None

    def test_a_run_without_a_state_root_skips_the_stage_loudly(self):
        """An offline port is a real mode and must not start needing a ledger.

        But a stage that skipped in silence would be indistinguishable from one
        that ran, which is the failure this whole stage exists to end.
        """
        fixture = self.three_dex_fixture()
        result = self.run_port(fixture, [CONTEXT_HOOK], stop_after="assess", out="run-skip")
        self.assertEqual(result.stage_reached, "assess")
        self.assertNotIn("assessment", result.artifacts)
        self.assertIn("[assess] skipped", self.printed)
        for flag in ASSESS_ARGUMENTS:
            self.assertIn(flag, self.printed)

    def test_three_of_the_four_arguments_is_refused_by_name(self):
        """Not a partial success: a run that looks like it recorded and did not."""
        for omitted in ASSESS_ARGUMENTS:
            with self.subTest(omitted=omitted):
                arguments = self.assess_arguments()
                arguments.pop(omitted)
                argv = ["x.apk", "--out", str(self.base / "unused")]
                for name, value in arguments.items():
                    argv += [name, value]
                stream = io.StringIO()
                with contextlib.redirect_stderr(stream):
                    code = main(argv)
                self.assertEqual(code, 2)
                self.assertIn(omitted, stream.getvalue())
                self.assertIn("Pass none of them to skip", stream.getvalue())

    def test_a_recording_refusal_arrives_as_a_driver_error(self):
        """`RecordError` reaching the operator would be a traceback from a module
        they did not invoke, which this project treats as a defect in itself.
        """
        request = AssessmentRequest(
            state_root=self.base / "state",
            run_id="driver-assess-test",
            allowed_actor="tester",
            owner_token="token-1",
            manifest_path=self.base / "missing-manifest.json",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(DriverError) as raised:
                record_assessment(request, self.base / "no-such-index")
        self.assertIn("could not record the assessment", str(raised.exception))

    def test_the_request_defaults_to_the_repository_manifest(self):
        """The same default `assessment_record.main` uses, so the driver and the
        hand-run command assess the same thing.
        """
        request = AssessmentRequest(
            state_root=Path("/srv/state"),
            run_id="r",
            allowed_actor="a",
            owner_token="t",
        )
        self.assertEqual(request.manifest.name, "hooks.json")
        self.assertEqual(request.manifest.parent.name, "manifest")
        override = Path("/srv/other.json")
        self.assertEqual(
            AssessmentRequest(Path("/srv/state"), "r", "a", "t", override).manifest, override
        )

    def test_the_stage_runs_between_index_and_resolve(self):
        """It reads the index stage 2 wrote and must not wait for a resolution."""
        self.assertEqual(STAGES.index("assess"), STAGES.index("index") + 1)
        self.assertEqual(STAGES.index("resolve"), STAGES.index("assess") + 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

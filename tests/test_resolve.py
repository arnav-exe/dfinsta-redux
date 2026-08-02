"""Tests for Stage 5: resolving the whole manifest against one decoded APK.

Everything here is synthetic. A real decode is 181,000 smali files and a real
index is ~63 MB, so the fixtures are a handful of hand-written classes plus an
index written to describe exactly them — `write_index` emits the same three
files `tools/indexer/build_index.py` does, in the same shapes `hook_index.py`
reads. The hooks are built with `hook_manifest.Hook(...)` rather than loaded
from `manifest/hooks.json`, so these tests pin the resolver's behaviour rather
than today's manifest content.

The centre of the file is `OutcomePrecedenceTests`: one test per rule in
`_classify`, worst-first. The order of those rules is the whole safety argument
of the stage — on a decode this pipeline already patched, the decoy candidates
still match the anchor while only the real host carries the marker, so ranking
"resolved" above "already applied" would patch a second, wrong class on every
re-run. `MutationTests` closes that loop by showing what each guard is holding
back: it re-applies the same rules to the same recorded candidates in a broken
order and asserts the answer changes.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from unittest import mock

from dfinsta_pipeline.hook_index import HookIndex, IndexUnusable
from dfinsta_pipeline.hook_manifest import (
    Hook,
    HostFingerprint,
    ManifestError,
    load_manifest,
    render,
)
from dfinsta_pipeline.runtime_identity import instrument
from dfinsta_pipeline.resolve import (
    CandidateReport,
    HookResolution,
    Outcome,
    main,
    resolve_hook,
    resolve_manifest,
    scan_for_anchor,
    search_hosts,
)

INDEX_SCHEMA_VERSION = 1

SHELL = "Lcom/instagram/app/InstagramAppShell;"
# The Reels request builder and a decoy that carries the same endpoint strings.
# The decoy sorts FIRST so that no test can pass merely because the real host
# happened to be the first candidate considered.
DECOY = "LX/01aB;"
HOST = "LX/04tC;"
# The two UI candidates: one a previous run half-patched, one untouched.
HALF_PATCHED = "LX/0DnT;"
CLEAN_TWIN = "LX/0DnV;"


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

ENDPOINT_HOOK = Hook(
    hook_id="replace_probe_endpoint",
    intent="route the outgoing Reels path through the mod",
    tier="robust",
    strategy="replace the const-string that builds the path",
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
        "    # dfinsta_probe_endpoint",
        '    const-string <r>, "clips/discover/"',
        "    invoke-static {<r>}, Lcom/dfinstagram/hooks;->replaceEndpoint"
        "(Ljava/lang/String;)Ljava/lang/String;",
        "    move-result-object <r>",
    ),
    # A comment, like the real replace-mode markers: baksmali deletes unreferenced
    # labels, and `significant()` filters comments out, so this also pins that the
    # marker is counted against the RAW text.
    marker="# dfinsta_probe_endpoint",
    expected_marker_count=1,
    mode="replace",
)

SETTINGS_HOOK = Hook(
    hook_id="install_probe_long_click",
    intent="swap the options-menu listener for the mod's",
    tier="ui",
    strategy="replace the listener construction",
    semantic_deps=(),
    hosts=(HostFingerprint("by_agent", note="nothing mechanical points at the class"),),
    anchor=(
        "new-instance <l:reg>, <cls:type>",
        "invoke-direct {<l>, <a:reg>}, <cls>-><init>(I)V",
    ),
    payload=(
        "    new-instance <l>, Lcom/dfinstagram/SettingsWrapper;",
        "    invoke-direct {<l>, <a>}, Lcom/dfinstagram/SettingsWrapper;-><init>(I)V",
    ),
    # Two marker occurrences, like the real UI hook: that is what makes a
    # half-applied patch expressible at all.
    marker="Lcom/dfinstagram/SettingsWrapper;",
    expected_marker_count=2,
    mode="replace",
)

# The same marker and count as SETTINGS_HOOK, a different anchor: the shape of
# the two real action-bar hooks, which are alternate implementations of one
# feature. It exists only for the shared-marker gap in `KnownGapTests`.
SETTINGS_HOOK_SIBLING = Hook(
    hook_id="install_probe_long_click_actionbar",
    intent="the same swap, in the other action bar implementation",
    tier="ui",
    strategy="replace the listener construction",
    semantic_deps=(),
    hosts=(HostFingerprint("by_agent", note="nothing mechanical points at the class"),),
    anchor=(
        "new-instance <l:reg>, <cls:type>",
        "invoke-direct {<l>, <a:reg>, <b:reg>}, <cls>-><init>(ILjava/lang/Object;)V",
    ),
    payload=(
        "    new-instance <l>, Lcom/dfinstagram/SettingsWrapper;",
        "    invoke-direct {<l>, <a>, <b>}, Lcom/dfinstagram/SettingsWrapper;"
        "-><init>(ILjava/lang/Object;)V",
    ),
    marker="Lcom/dfinstagram/SettingsWrapper;",
    expected_marker_count=2,
    mode="replace",
)

# The same site as SETTINGS_HOOK, found by the anchor itself rather than by an
# agent's proposal. Its own marker, because a marker is a per-hook stamp and two
# hooks sharing one make each read the other's patch as its own.
ANCHOR_HOOK = Hook(
    hook_id="install_probe_long_click_by_anchor",
    intent="swap the options-menu listener for the mod's",
    tier="ui",
    strategy="replace the listener construction",
    semantic_deps=(),
    hosts=(
        HostFingerprint(
            "by_anchor", note="the anchor matches exactly one class in the decode"
        ),
    ),
    anchor=(
        "new-instance <l:reg>, <cls:type>",
        "invoke-direct {<l>, <a:reg>}, <cls>-><init>(I)V",
    ),
    payload=(
        "    new-instance <l>, <cls>",
        "    # dfinsta_probe_by_anchor",
        "    invoke-static {}, Lcom/dfinstagram/probe;->h_probe()V",
        "    invoke-direct {<l>, <a>}, <cls>-><init>(I)V",
    ),
    marker="# dfinsta_probe_by_anchor",
    expected_marker_count=1,
    mode="replace",
)

RETIRED_HOOK = Hook(
    hook_id="retired_probe_hook",
    intent="a hook this version no longer needs",
    tier="robust",
    strategy="none",
    semantic_deps=(),
    hosts=(HostFingerprint("named", descriptor="LX/0Gone;", note="gone"),),
    anchor=("nop",),
    payload=("    invoke-static {}, Lcom/dfinstagram/hooks;->retired()V",),
    marker="Lcom/dfinstagram/hooks;->retired()V",
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
    """A clean request builder: the anchor matches here exactly once."""
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


def patched_endpoint_class(descriptor: str, register: str = "v1") -> str:
    """The same class after this pipeline patched it.

    `replace` mode re-emits the anchor line, so a patched host STILL contains a
    line the anchor matches. That is not a curiosity of the fixture; it is the
    reason the marker has to be checked before the anchor.
    """
    return endpoint_class(descriptor, register).replace(
        f'    const-string {register}, "clips/discover/"',
        "\n".join(render(line, {"r": register}) for line in ENDPOINT_HOOK.payload),
    )


def analytics_class(descriptor: str) -> str:
    """Carries the endpoint strings — analytics maps do — but not the anchor shape."""
    return (
        f".class public {descriptor}\n"
        ".super Ljava/lang/Object;\n"
        "\n"
        ".method public static A00()V\n"
        "    .locals 1\n"
        "\n"
        '    const-string v0, "clips/discover/stream/"\n'
        "\n"
        "    invoke-static {v0}, LX/0Log;->A01(Ljava/lang/String;)V\n"
        "\n"
        "    return-void\n"
        ".end method\n"
    )


def listener_class(descriptor: str, listener: str = "LX/0Dn9;") -> str:
    """The UI class the settings hook attaches to, untouched."""
    return (
        f".class public {descriptor}\n"
        ".super Ljava/lang/Object;\n"
        "\n"
        ".method public A0G(I)V\n"
        "    .locals 2\n"
        "\n"
        "    .line 88\n"
        f"    new-instance v0, {listener}\n"
        "\n"
        f"    invoke-direct {{v0, p1}}, {listener}-><init>(I)V\n"
        "\n"
        "    return-void\n"
        ".end method\n"
    )


def half_patched_listener_class(descriptor: str) -> str:
    """One of the hook's two marker lines landed: a run died mid-patch."""
    return listener_class(descriptor).replace(
        "new-instance v0, LX/0Dn9;",
        "new-instance v0, Lcom/dfinstagram/SettingsWrapper;",
    )


def patched_listener_class(descriptor: str) -> str:
    """The same class after the settings hook landed: both markers present."""
    return listener_class(descriptor, "Lcom/dfinstagram/SettingsWrapper;")


def twice_matching_listener_class(descriptor: str) -> str:
    """One class, two sites the anchor matches. Distinct from two classes matching."""
    return listener_class(descriptor).replace(
        "    return-void",
        "    new-instance v1, LX/0DnA;\n"
        "\n"
        "    invoke-direct {v1, p1}, LX/0DnA;-><init>(I)V\n"
        "\n"
        "    return-void",
    )


def anchor_patched_listener_class(descriptor: str) -> str:
    """The host after ANCHOR_HOOK landed, which the anchor no longer matches.

    `replace` splices the DFInsta lines BETWEEN the two anchor lines, so they stop
    being adjacent in `significant()`'s view and the pattern that found this class
    can no longer find it. Written out rather than rendered from the payload
    because that is precisely the state being tested: the marker has to be a
    second way into the host, or a re-run over a decode this pipeline already
    patched reports NOT_FOUND where every other fingerprint kind reports
    ALREADY_APPLIED.
    """
    return listener_class(descriptor).replace(
        "    invoke-direct {v0, p1}, LX/0Dn9;-><init>(I)V",
        "    # dfinsta_probe_by_anchor\n"
        "\n"
        "    invoke-static {}, Lcom/dfinstagram/probe;->h_probe()V\n"
        "\n"
        "    invoke-direct {v0, p1}, LX/0Dn9;-><init>(I)V",
    )


# ------------------------------------------------------------------ fixtures


def smali_path(descriptor: str) -> str:
    """`LX/04tC;` -> `smali_classes2/LX/04tC.smali`, the layout apktool emits."""
    return "smali_classes2/" + descriptor[1:-1] + ".smali"


@dataclass(frozen=True)
class Fixture:
    """A fake decode and the index built from it."""

    decode: Path
    index_dir: Path
    index: HookIndex


def write_index(
    index_dir: Path,
    decode: Path,
    classes: Mapping[str, str],
    api_paths: Mapping[str, Sequence[str]] | None = None,
    *,
    decode_path: str | None = None,
) -> Path:
    """Emit the three files the indexer writes, for a handful of classes.

    `classes` maps descriptor -> decode-relative path. The format is the one
    documented in `tools/indexer/build_index.py`: `structural.jsonl` is the
    header as line one followed by one row per class, and `api_surface.json`
    carries the literal -> classes map the by_literal search reads.
    """
    index_dir.mkdir(parents=True, exist_ok=True)
    header = {
        "kind": "dfinsta.index.header",
        "schema_version": INDEX_SCHEMA_VERSION,
        "generator": "tests/test_resolve.py",
        # `assert_matches` compares against the RESOLVED path, so anything else
        # here is an index bound to a different decode and must be refused.
        "decode_path": (
            decode_path if decode_path is not None else str(Path(decode).resolve())
        ),
        "decode_name": Path(decode).name,
        "content_hash": "sha256:" + "ab" * 32,
        "resource_types_indexed": ["drawable"],
    }
    rows = [
        {
            "kind": "dfinsta.index.class",
            "descriptor": descriptor,
            "path": path,
            "tree": path.split("/", 1)[0],
            "super": "Ljava/lang/Object;",
            "interfaces": [],
            "methods": [],
            "obfuscated": descriptor.startswith("LX/"),
        }
        for descriptor, path in classes.items()
    ]
    (index_dir / "structural.jsonl").write_text(
        "\n".join([json.dumps(header)] + [json.dumps(row) for row in rows]) + "\n",
        encoding="utf-8",
    )
    (index_dir / "api_surface.json").write_text(
        json.dumps(
            {
                "header": header,
                "api_paths": {
                    literal: list(descriptors)
                    for literal, descriptors in (api_paths or {}).items()
                },
                "resources": {"drawable": {"ic_probe": "0x7f080001"}},
                "resource_names_by_id": {"0x7f080001": "drawable/ic_probe"},
                "stable_types": {
                    descriptor: path
                    for descriptor, path in classes.items()
                    if not descriptor.startswith("LX/")
                },
            }
        ),
        encoding="utf-8",
    )
    (index_dir / "header.json").write_text(
        json.dumps(header, indent=2) + "\n", encoding="utf-8"
    )
    return index_dir


def hook_to_dict(hook: Hook) -> dict[str, Any]:
    """The manifest form of a hook, for the tests that go through `main()`."""
    return {
        "hook_id": hook.hook_id,
        "intent": hook.intent,
        "tier": hook.tier,
        "strategy": hook.strategy,
        "semantic_deps": list(hook.semantic_deps),
        "hosts": [
            {
                "kind": host.kind,
                "descriptor": host.descriptor,
                "literal": host.literal,
                "co_literals": list(host.co_literals),
                "note": host.note,
            }
            for host in hook.hosts
        ],
        "anchor": list(hook.anchor),
        "payload": list(hook.payload),
        "marker": hook.marker,
        "expected_marker_count": hook.expected_marker_count,
        "mode": hook.mode,
        "expected_anchor_count": hook.expected_anchor_count,
        "constraints": list(hook.constraints),
        "status": hook.status,
    }


def write_manifest(path: Path, hooks: Iterable[Hook]) -> Path:
    entries = []
    for hook in hooks:
        entry = hook_to_dict(hook)
        # load_manifest -> assert_instrumented now rejects any ACTIVE hook whose
        # payload does not announce its own runtime identity, so every manifest
        # this helper writes must carry that call. instrument() is idempotent and
        # the single source of the exact line, so these fixtures cannot drift from
        # the generator; retired hooks are exempt, exactly as the loader is.
        if hook.status == "active":
            entry["payload"] = list(instrument(entry["payload"], hook.hook_id))
        entries.append(entry)
    path.write_text(
        json.dumps({"schema_version": 1, "hooks": entries}),
        encoding="utf-8",
    )
    return path


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    """Call `main()` directly, capturing what it printed."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class FixtureCase(unittest.TestCase):
    """Base: a scratch directory every fixture is built under."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)

    def make_fixture(
        self,
        classes: Mapping[str, str | None],
        api_paths: Mapping[str, Sequence[str]] | None = None,
        *,
        name: str = "decode",
        decode_path: str | None = None,
    ) -> Fixture:
        """A decode holding `classes` (descriptor -> smali text) and its index.

        A `None` body indexes the class without writing the file, which is how
        a stale index — one naming a class this decode does not have — is
        expressed.
        """
        decode = self.base / name
        paths = {descriptor: smali_path(descriptor) for descriptor in classes}
        for descriptor, text in classes.items():
            if text is None:
                continue
            target = decode / paths[descriptor]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        decode.mkdir(parents=True, exist_ok=True)
        index_dir = write_index(
            self.base / f"{name}-index",
            decode,
            paths,
            api_paths,
            decode_path=decode_path,
        )
        return Fixture(decode, index_dir, HookIndex.load(index_dir))

    def resolve(
        self, hook: Hook, fixture: Fixture, proposals: Sequence[str] = ()
    ) -> HookResolution:
        return resolve_hook(hook, fixture.index, fixture.decode, proposals)

    # Fixtures the precedence tests share, each named for the decode state it
    # represents rather than for its contents.

    def clean_endpoint_decode(self) -> Fixture:
        """Two candidate classes, neither patched: the first-run state."""
        return self.make_fixture(
            {DECOY: endpoint_class(DECOY, "v3"), HOST: endpoint_class(HOST)},
            {"clips/discover/": [DECOY, HOST], "clips/homecoming/": [DECOY, HOST]},
        )

    def rerun_endpoint_decode(self) -> Fixture:
        """The re-run state: the real host is patched, the decoy still matches."""
        return self.make_fixture(
            {DECOY: endpoint_class(DECOY, "v3"), HOST: patched_endpoint_class(HOST)},
            {"clips/discover/": [DECOY, HOST], "clips/homecoming/": [DECOY, HOST]},
        )

    def half_patched_settings_decode(self) -> Fixture:
        """A run died mid-patch in one class; its twin is untouched and matches."""
        return self.make_fixture(
            {
                HALF_PATCHED: half_patched_listener_class(HALF_PATCHED),
                CLEAN_TWIN: listener_class(CLEAN_TWIN),
            }
        )


class FixtureSanityTests(FixtureCase):
    """The fixtures must mean what every other test assumes they mean."""

    def test_the_applied_shell_carries_exactly_the_expected_marker_count(self):
        self.assertEqual(
            APPLIED_SHELL.count(CONTEXT_HOOK.marker), CONTEXT_HOOK.expected_marker_count
        )
        self.assertEqual(CLEAN_SHELL.count(CONTEXT_HOOK.marker), 0)

    def test_the_patched_endpoint_class_carries_exactly_one_marker(self):
        patched = patched_endpoint_class(HOST)
        self.assertEqual(patched.count(ENDPOINT_HOOK.marker), 1)
        self.assertEqual(endpoint_class(HOST).count(ENDPOINT_HOOK.marker), 0)
        # And it still holds a line the anchor matches, which is the whole point.
        self.assertIn('    const-string v1, "clips/discover/"', patched)

    def test_the_half_patched_listener_holds_one_of_two_markers(self):
        half = half_patched_listener_class(HALF_PATCHED)
        self.assertEqual(half.count(SETTINGS_HOOK.marker), 1)
        self.assertEqual(SETTINGS_HOOK.expected_marker_count, 2)
        self.assertEqual(listener_class(CLEAN_TWIN).count(SETTINGS_HOOK.marker), 0)
        self.assertEqual(
            patched_listener_class(HALF_PATCHED).count(SETTINGS_HOOK.marker), 2
        )

    def test_the_decoy_sorts_before_the_real_host(self):
        # `descriptors_with_all_literals` returns sorted descriptors, so this is
        # what stops "already applied won" from being "the host came first".
        self.assertLess(DECOY, HOST)

    def test_the_index_the_helper_writes_is_accepted_by_for_decode(self):
        fixture = self.clean_endpoint_decode()
        index = HookIndex.for_decode(fixture.index_dir, fixture.decode)
        self.assertEqual(index.class_count(), 2)
        self.assertEqual(index.path_for(HOST), smali_path(HOST))
        self.assertEqual(index.decode_path, str(fixture.decode.resolve()))
        self.assertTrue(index.content_hash)
        self.assertEqual(
            index.descriptors_with_all_literals(ENDPOINT_HOOK.hosts[0].required_literals),
            (DECOY, HOST),
        )


class SearchHostsTests(FixtureCase):
    """What each fingerprint kind proposes, and the evidence it proposes it on."""

    def test_named_returns_the_descriptor_when_the_index_has_it(self):
        fixture = self.make_fixture({SHELL: CLEAN_SHELL})
        search = search_hosts(CONTEXT_HOOK, CONTEXT_HOOK.hosts[0], fixture.index)
        self.assertEqual(search.kind, "named")
        self.assertEqual(search.candidates, (SHELL,))
        self.assertEqual(search.evidence, {"descriptor": SHELL})
        self.assertEqual(search.reason, "")

    def test_named_reports_a_missing_stable_type_as_a_real_change(self):
        # A named host is non-obfuscated, so its disappearance is a genuine app
        # change and must not read like an index that failed to look hard enough.
        fixture = self.make_fixture({HOST: endpoint_class(HOST)})
        search = search_hosts(CONTEXT_HOOK, CONTEXT_HOOK.hosts[0], fixture.index)
        self.assertEqual(search.candidates, ())
        self.assertIn(SHELL, search.reason)
        self.assertIn("does not exist in this version", search.reason)
        self.assertIn("not a lookup failure", search.reason)

    def test_by_literal_returns_only_the_co_located_classes(self):
        fixture = self.make_fixture(
            {DECOY: endpoint_class(DECOY), HOST: endpoint_class(HOST)},
            {
                "clips/discover/": [DECOY, HOST, "LX/0Ana;"],
                "clips/homecoming/": [HOST],
            },
        )
        search = search_hosts(ENDPOINT_HOOK, ENDPOINT_HOOK.hosts[0], fixture.index)
        self.assertEqual(search.kind, "by_literal")
        self.assertEqual(search.candidates, (HOST,))
        self.assertEqual(search.reason, "")

    def test_by_literal_evidence_records_the_per_literal_and_co_located_counts(self):
        # The counts are the argument for co-location: each literal on its own is
        # in several classes, and only their intersection picks the host out.
        fixture = self.make_fixture(
            {DECOY: endpoint_class(DECOY), HOST: endpoint_class(HOST)},
            {
                "clips/discover/": [DECOY, HOST, "LX/0Ana;"],
                "clips/homecoming/": [HOST, "LX/0Pre;"],
            },
        )
        search = search_hosts(ENDPOINT_HOOK, ENDPOINT_HOOK.hosts[0], fixture.index)
        self.assertEqual(
            search.evidence["classes_per_literal"],
            {"clips/discover/": 3, "clips/homecoming/": 2},
        )
        self.assertEqual(search.evidence["co_located"], 1)
        self.assertEqual(
            search.evidence["literals"], ["clips/discover/", "clips/homecoming/"]
        )
        self.assertNotIn("not_indexed", search.evidence)

    def test_by_literal_distinguishes_an_unindexed_literal_from_no_co_location(self):
        """"Never indexed" and "no longer together" need different repairs.

        The index only holds API-path-shaped strings, so a literal missing from
        it is usually a manifest authoring error, whereas literals that all exist
        but are no longer in one class is a real version change. Collapsing both
        into "not found" sends whoever reads it hunting the wrong thing.
        """
        not_indexed = self.make_fixture(
            {HOST: endpoint_class(HOST)}, {"clips/discover/": [HOST]}
        )
        search = search_hosts(ENDPOINT_HOOK, ENDPOINT_HOOK.hosts[0], not_indexed.index)
        self.assertEqual(search.candidates, ())
        self.assertEqual(search.evidence["not_indexed"], ["clips/homecoming/"])
        self.assertIn("absent from the index", search.reason)
        self.assertIn("never indexed", search.reason)

        split = self.make_fixture(
            {DECOY: endpoint_class(DECOY), HOST: endpoint_class(HOST)},
            {"clips/discover/": [HOST], "clips/homecoming/": [DECOY]},
            name="split",
        )
        search = search_hosts(ENDPOINT_HOOK, ENDPOINT_HOOK.hosts[0], split.index)
        self.assertEqual(search.candidates, ())
        self.assertNotIn("not_indexed", search.evidence)
        self.assertIn("no single class contains all of", search.reason)
        self.assertIn("no longer co-located", search.reason)
        self.assertEqual(search.evidence["co_located"], 0)
        self.assertEqual(
            search.evidence["classes_per_literal"],
            {"clips/discover/": 1, "clips/homecoming/": 1},
        )

    def test_by_agent_with_no_proposal_says_one_is_required(self):
        fixture = self.make_fixture({HALF_PATCHED: listener_class(HALF_PATCHED)})
        search = search_hosts(SETTINGS_HOOK, SETTINGS_HOOK.hosts[0], fixture.index)
        self.assertEqual(search.kind, "by_agent")
        self.assertEqual(search.candidates, ())
        self.assertEqual(search.evidence, {"proposed": [], "not_in_index": []})
        self.assertIn(SETTINGS_HOOK.hook_id, search.reason)
        self.assertIn("a proposed host is required", search.reason)

    def test_by_agent_keeps_only_the_proposals_this_version_actually_has(self):
        fixture = self.make_fixture({CLEAN_TWIN: listener_class(CLEAN_TWIN)})
        search = search_hosts(
            SETTINGS_HOOK,
            SETTINGS_HOOK.hosts[0],
            fixture.index,
            proposals=(CLEAN_TWIN, "LX/0Stale;"),
        )
        self.assertEqual(search.candidates, (CLEAN_TWIN,))
        self.assertEqual(
            search.evidence,
            {"proposed": [CLEAN_TWIN, "LX/0Stale;"], "not_in_index": ["LX/0Stale;"]},
        )
        self.assertIn("recycled", search.reason)

    def test_by_agent_with_every_proposal_present_has_no_complaint(self):
        fixture = self.make_fixture(
            {
                CLEAN_TWIN: listener_class(CLEAN_TWIN),
                HALF_PATCHED: listener_class(HALF_PATCHED),
            }
        )
        search = search_hosts(
            SETTINGS_HOOK,
            SETTINGS_HOOK.hosts[0],
            fixture.index,
            proposals=(HALF_PATCHED, CLEAN_TWIN),
        )
        # Proposal order is preserved: it is the agent's ranking, not the index's.
        self.assertEqual(search.candidates, (HALF_PATCHED, CLEAN_TWIN))
        self.assertEqual(search.evidence["not_in_index"], [])
        self.assertEqual(search.reason, "")

    def test_a_search_is_json_serialisable(self):
        fixture = self.clean_endpoint_decode()
        search = search_hosts(ENDPOINT_HOOK, ENDPOINT_HOOK.hosts[0], fixture.index)
        payload = json.loads(json.dumps(search.to_dict()))
        self.assertEqual(payload["kind"], "by_literal")
        self.assertEqual(payload["candidates"], [DECOY, HOST])


class ByAnchorSearchTests(FixtureCase):
    """`by_anchor`: the host is the class the anchor matches, and nothing else says so.

    The kind exists because two hooks in `manifest/hooks.json` had no fingerprint
    at all and cost an agent invocation per port, while their anchors were
    measured to match exactly one class in the whole decode on both 430 and 439.
    So the anchor is promoted from "the site inside the host" to "the host as
    well" — and with that comes a *second* uniqueness claim, over the whole
    decode, which these tests keep separate from the old one about matching once
    inside the winning class.
    """

    def decode_with(self, **classes: str) -> Fixture:
        return self.make_fixture(classes)

    def search(self, fixture: Fixture, hook: Hook = ANCHOR_HOOK):
        return search_hosts(hook, hook.hosts[0], fixture.index, decode=fixture.decode)

    def clean_anchor_decode(self) -> Fixture:
        """One class the anchor matches, and two it does not."""
        return self.make_fixture(
            {
                DECOY: analytics_class(DECOY),
                HOST: endpoint_class(HOST),
                CLEAN_TWIN: listener_class(CLEAN_TWIN),
            }
        )

    def test_the_anchor_selects_the_one_class_it_matches(self):
        fixture = self.clean_anchor_decode()
        search = self.search(fixture)
        self.assertEqual(search.kind, "by_anchor")
        self.assertEqual(search.candidates, (CLEAN_TWIN,))
        self.assertEqual(search.reason, "")
        # And that candidate goes the whole way, so the kind is not merely a
        # search that reports something nothing downstream can use.
        resolution = self.resolve(ANCHOR_HOOK, fixture)
        self.assertIs(resolution.outcome, Outcome.RESOLVED)
        self.assertEqual(resolution.descriptor, CLEAN_TWIN)

    def test_the_evidence_records_what_the_prefilter_cost_and_what_it_kept(self):
        # A gate has to be able to see that the decode really was searched and how
        # much of it survived, or "one class matched" is indistinguishable from
        # "one class was looked at".
        fixture = self.clean_anchor_decode()
        search = self.search(fixture)
        self.assertEqual(
            search.evidence,
            {
                "prefilter": "invoke-direct {",
                "classes_scanned": 3,
                "classes_prefiltered": 1,
                "anchor_matched": [CLEAN_TWIN],
                "carrying_marker": [],
            },
        )
        self.assertEqual(json.loads(json.dumps(search.to_dict()))["kind"], "by_anchor")

    def test_a_decode_the_anchor_matches_nowhere_is_not_found(self):
        fixture = self.make_fixture({DECOY: analytics_class(DECOY), HOST: endpoint_class(HOST)})
        resolution = self.resolve(ANCHOR_HOOK, fixture)
        self.assertIs(resolution.outcome, Outcome.NOT_FOUND)
        # And it stops the port. Zero matches is the answer a by_anchor hook gives
        # when its one and only fingerprint failed; letting it through would emit
        # an operation list quietly missing this hook.
        self.assertTrue(resolution.escalates)
        self.assertIn("the anchor matches no class in this decode", resolution.reason)
        self.assertIn("nothing left to fall back on", resolution.reason)
        self.assertEqual(resolution.searches[0].evidence["classes_scanned"], 2)
        # The positive control: the same hook, the same two decoys, plus the host.
        # Without it "found nothing" could mean the scan never ran.
        with_host = self.clean_anchor_decode()
        self.assertIs(self.resolve(ANCHOR_HOOK, with_host).outcome, Outcome.RESOLVED)

    def test_two_classes_matching_the_anchor_escalate_as_ambiguous_naming_both(self):
        """No tiebreak, deliberately.

        The whole value of the kind is that the anchor is unique across the
        decode. Picking a winner among several would keep the port moving on the
        version where that stopped being true, and the wrong class would be
        patched with something that assembles and verifies.
        """
        fixture = self.make_fixture(
            {
                CLEAN_TWIN: listener_class(CLEAN_TWIN),
                HALF_PATCHED: listener_class(HALF_PATCHED, "LX/0DnB;"),
            }
        )
        search = self.search(fixture)
        self.assertEqual(search.candidates, (HALF_PATCHED, CLEAN_TWIN))
        self.assertIn("unique across the WHOLE decode", search.reason)

        resolution = self.resolve(ANCHOR_HOOK, fixture)
        self.assertIs(resolution.outcome, Outcome.AMBIGUOUS)
        self.assertIsNone(resolution.descriptor)
        for descriptor in (CLEAN_TWIN, HALF_PATCHED):
            self.assertIn(descriptor, resolution.reason)

    def test_matching_twice_inside_one_class_is_a_different_failure(self):
        """The two uniqueness claims are separate and must fail separately.

        One class matching twice is the anchor being ambiguous *inside the host* —
        the claim `expected_anchor_count` has always made, and the reason the
        profile-bar anchor grew from three lines to five. Two classes matching
        once each is the new claim, about the decode. Reporting either as the
        other would send whoever reads it to fix the wrong end of the pattern.
        """
        fixture = self.make_fixture({CLEAN_TWIN: twice_matching_listener_class(CLEAN_TWIN)})
        search = self.search(fixture)
        self.assertEqual(search.candidates, (CLEAN_TWIN,))  # ONE class, cross-decode
        self.assertEqual(search.reason, "")

        resolution = self.resolve(ANCHOR_HOOK, fixture)
        self.assertIs(resolution.outcome, Outcome.UNRESOLVED)
        self.assertIn("anchor matched 2 times, expected 1", resolution.reason)
        self.assertIn("ambiguous in this class", resolution.reason)

    def test_an_already_patched_host_is_still_found_by_its_marker(self):
        """ALREADY_APPLIED must survive a search that depends on the anchor.

        `by_anchor` is the first fingerprint kind whose host search uses the
        anchor, and a `replace` payload splices lines through the middle of it. So
        the class this pipeline already patched no longer matches, and without the
        marker as a second route the stage would report NOT_FOUND on every re-run
        over a patched decode — turning the one outcome that means "there is
        nothing to do" into an escalation.
        """
        fixture = self.make_fixture(
            {CLEAN_TWIN: anchor_patched_listener_class(CLEAN_TWIN)}
        )
        search = self.search(fixture)
        self.assertEqual(search.evidence["anchor_matched"], [])  # control
        self.assertEqual(search.evidence["carrying_marker"], [CLEAN_TWIN])
        self.assertEqual(search.candidates, (CLEAN_TWIN,))

        resolution = self.resolve(ANCHOR_HOOK, fixture)
        self.assertIs(resolution.outcome, Outcome.ALREADY_APPLIED)
        self.assertEqual(resolution.descriptor, CLEAN_TWIN)

    def test_searching_by_anchor_without_a_decode_is_a_caller_error(self):
        # The index holds API-path literals and class paths; "which classes match
        # this instruction pattern" is neither, so there is nothing to degrade to.
        fixture = self.clean_anchor_decode()
        with self.assertRaises(ManifestError) as caught:
            search_hosts(ANCHOR_HOOK, ANCHOR_HOOK.hosts[0], fixture.index)
        self.assertIn("resolved against the decode", str(caught.exception))
        self.assertIn("caller contract violation", str(caught.exception))


class OutcomePrecedenceTests(FixtureCase):
    """One test per rule in `_classify`, in the order `_classify` applies them."""

    def test_a_partial_marker_conflicts_even_though_another_candidate_resolves(self):
        """Rule 1. A half-patched decode is a hard stop, not a ranking input.

        The clean twin resolves perfectly here, so every later rule would have an
        answer to give. None of them may: a marker at the wrong count means a
        previous run died mid-patch, and no safe conclusion can be drawn through
        that state — the decode has to be re-extracted.
        """
        fixture = self.half_patched_settings_decode()
        resolution = self.resolve(
            SETTINGS_HOOK, fixture, proposals=(HALF_PATCHED, CLEAN_TWIN)
        )
        self.assertIs(resolution.outcome, Outcome.CONFLICT)
        self.assertTrue(resolution.escalates)
        self.assertIn(HALF_PATCHED, resolution.reason)
        self.assertIn("1/2", resolution.reason)
        self.assertIn(SETTINGS_HOOK.marker, resolution.reason)
        self.assertIn("re-extracted", resolution.reason)
        # The candidate that would have won is still on the record, and still
        # marked resolved: the report says what was considered, not just the verdict.
        by_descriptor = {item.descriptor: item for item in resolution.candidates}
        self.assertEqual(set(by_descriptor), {HALF_PATCHED, CLEAN_TWIN})
        self.assertTrue(by_descriptor[CLEAN_TWIN].resolved)
        self.assertEqual(by_descriptor[HALF_PATCHED].marker_count, 1)

    def test_the_marker_in_two_candidates_conflicts(self):
        """Rule 1b. The same hook applied twice is the other half-patched shape.

        Two classes carrying a full marker means one of them is a wrong host that
        was patched anyway; picking either would ship the wrong one.
        """
        fixture = self.make_fixture(
            {
                DECOY: patched_endpoint_class(DECOY, "v3"),
                HOST: patched_endpoint_class(HOST),
            },
            {"clips/discover/": [DECOY, HOST], "clips/homecoming/": [DECOY, HOST]},
        )
        resolution = self.resolve(ENDPOINT_HOOK, fixture)
        self.assertIs(resolution.outcome, Outcome.CONFLICT)
        self.assertTrue(resolution.escalates)
        self.assertIn(DECOY, resolution.reason)
        self.assertIn(HOST, resolution.reason)
        self.assertIn("applied twice", resolution.reason)
        self.assertIsNone(resolution.descriptor)

    def test_already_applied_beats_resolved_and_names_the_real_host(self):
        """Rule 2, and the reason this module exists.

        On a re-run the decoys still match the anchor cleanly while only the real
        host carries the marker. If "resolved" were ranked first the stage would
        hand the applier the decoy — a second, wrong class patched on every
        single re-run, forever, with no error anywhere.

        Escalation is asserted too: a re-run over an already-patched decode is
        the normal idempotent case, not a failure.
        """
        fixture = self.rerun_endpoint_decode()
        resolution = self.resolve(ENDPOINT_HOOK, fixture)
        self.assertIs(resolution.outcome, Outcome.ALREADY_APPLIED)
        self.assertEqual(resolution.descriptor, HOST)
        self.assertIs(resolution.escalates, False)
        self.assertIs(resolution.outcome.escalates, False)
        self.assertIn("already carries the patch", resolution.reason)
        # The decoy really does resolve; that is what makes the ordering load-bearing.
        by_descriptor = {item.descriptor: item for item in resolution.candidates}
        self.assertTrue(by_descriptor[DECOY].resolved)
        self.assertTrue(by_descriptor[HOST].already_applied)
        self.assertFalse(by_descriptor[HOST].resolved)
        # And nothing is emitted for it: there is no work left to do.
        with self.assertRaises(ManifestError):
            resolution.as_operation(ENDPOINT_HOOK)

    def test_one_matching_candidate_resolves_into_a_runnable_operation(self):
        """Rule 3. The ordinary success path, end to end."""
        fixture = self.make_fixture({SHELL: CLEAN_SHELL})
        resolution = self.resolve(CONTEXT_HOOK, fixture)
        self.assertIs(resolution.outcome, Outcome.RESOLVED)
        self.assertFalse(resolution.escalates)
        self.assertEqual(resolution.descriptor, SHELL)
        self.assertIn("matched the anchor exactly once", resolution.reason)

        assert resolution.resolution is not None
        self.assertTrue(resolution.resolution.resolved)
        self.assertEqual(resolution.resolution.bindings, {"app": "p0"})
        # The applier needs the file, and only the Resolve stage knows which one:
        # `resolve_in_source` cannot fill this in, so `_classify` does.
        self.assertEqual(resolution.resolution.smali_path, smali_path(SHELL))
        self.assertTrue((fixture.decode / resolution.resolution.smali_path).is_file())

        operation = resolution.as_operation(CONTEXT_HOOK)
        self.assertEqual(
            set(operation),
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
        self.assertEqual(operation["id"], CONTEXT_HOOK.hook_id)
        self.assertEqual(operation["descriptor"], SHELL)
        self.assertEqual(operation["mode"], "insert_after")
        self.assertEqual(operation["marker"], CONTEXT_HOOK.marker)
        self.assertEqual(operation["expected_marker_count"], 1)
        self.assertEqual(operation["expected_anchor_count"], 1)
        self.assertEqual(
            operation["anchor"],
            ["invoke-super {p0}, Landroid/app/Application;->onCreate()V"],
        )
        self.assertEqual(
            operation["payload"],
            [
                "",
                "    invoke-static {p0}, Lcom/dfinstagram/startapp;->setContext"
                "(Landroid/app/Application;)V",
            ],
        )

    def test_the_same_hook_resolves_against_a_different_register(self):
        # The point of the manifest layer: one entry, several versions. Pinned
        # here because the Resolve stage is what carries the binding through to
        # the emitted operation.
        fixture = self.make_fixture({SHELL: CLEAN_SHELL.replace("{p0}", "{v4}")})
        resolution = self.resolve(CONTEXT_HOOK, fixture)
        self.assertIs(resolution.outcome, Outcome.RESOLVED)
        assert resolution.resolution is not None
        self.assertEqual(resolution.resolution.bindings, {"app": "v4"})
        self.assertIn(
            "invoke-static {v4}", resolution.as_operation(CONTEXT_HOOK)["payload"][1]
        )

    def test_two_matching_candidates_are_ambiguous_rather_than_first_wins(self):
        """Rule 4. Order is not evidence.

        Both classes match the anchor, so the fingerprint does not discriminate;
        picking the first would be a coin flip dressed up as a result.
        """
        fixture = self.clean_endpoint_decode()
        resolution = self.resolve(ENDPOINT_HOOK, fixture)
        self.assertIs(resolution.outcome, Outcome.AMBIGUOUS)
        self.assertTrue(resolution.escalates)
        self.assertIsNone(resolution.descriptor)
        self.assertIn(DECOY, resolution.reason)
        self.assertIn(HOST, resolution.reason)
        self.assertIn("matched in 2 candidate classes", resolution.reason)
        self.assertIn("does not discriminate", resolution.reason)

    def test_candidates_that_none_match_are_unresolved_and_named(self):
        """Rule 5. "Found nothing" and "found the wrong things" differ.

        The reason has to name the candidates, because the next step is a human
        or an agent looking at exactly those classes.
        """
        fixture = self.make_fixture(
            {DECOY: analytics_class(DECOY), HOST: analytics_class(HOST)},
            {"clips/discover/": [DECOY, HOST], "clips/homecoming/": [DECOY, HOST]},
        )
        resolution = self.resolve(ENDPOINT_HOOK, fixture)
        self.assertIs(resolution.outcome, Outcome.UNRESOLVED)
        self.assertTrue(resolution.escalates)
        self.assertIn("2 candidate host(s) found", resolution.reason)
        self.assertIn(DECOY, resolution.reason)
        self.assertIn(HOST, resolution.reason)
        self.assertIn("anchor pattern did not match", resolution.reason)
        self.assertEqual(len(resolution.candidates), 2)

    def test_a_candidate_the_decode_cannot_read_is_recorded_not_crashed_on(self):
        # A stale structural index names a class this decode does not have. The
        # index is a search accelerator, never evidence, so this degrades to "not
        # matched" with the path in the reason rather than an OSError out of the stage.
        fixture = self.make_fixture(
            {DECOY: None, HOST: analytics_class(HOST)},
            {"clips/discover/": [DECOY, HOST], "clips/homecoming/": [DECOY, HOST]},
        )
        resolution = self.resolve(ENDPOINT_HOOK, fixture)
        self.assertIs(resolution.outcome, Outcome.UNRESOLVED)
        unreadable = next(
            item for item in resolution.candidates if item.descriptor == DECOY
        )
        self.assertIn("the decode cannot read it", unreadable.reason)
        self.assertIn(smali_path(DECOY), unreadable.reason)
        self.assertFalse(unreadable.resolved)

    def test_a_named_host_missing_from_the_index_is_not_found(self):
        """Rule 6a. No candidate at all, for a host that is supposed to be stable."""
        fixture = self.make_fixture({HOST: endpoint_class(HOST)})
        resolution = self.resolve(CONTEXT_HOOK, fixture)
        self.assertIs(resolution.outcome, Outcome.NOT_FOUND)
        self.assertTrue(resolution.escalates)
        self.assertEqual(resolution.candidates, ())
        self.assertIn(SHELL, resolution.reason)
        self.assertIn("does not exist in this version", resolution.reason)

    def test_by_literal_with_no_co_located_class_is_not_found(self):
        """Rule 6b. Each literal still exists, but nothing holds them together."""
        fixture = self.make_fixture(
            {DECOY: endpoint_class(DECOY), HOST: endpoint_class(HOST)},
            {"clips/discover/": [HOST], "clips/homecoming/": [DECOY]},
        )
        resolution = self.resolve(ENDPOINT_HOOK, fixture)
        self.assertIs(resolution.outcome, Outcome.NOT_FOUND)
        self.assertEqual(resolution.candidates, ())
        self.assertIn("no single class contains all of", resolution.reason)
        self.assertIn("clips/homecoming/", resolution.reason)
        # The evidence survives onto the resolution, so a gate can see the counts.
        self.assertEqual(resolution.searches[0].evidence["co_located"], 0)

    def test_a_by_agent_hook_with_no_proposal_needs_an_agent(self):
        """Rule 7. Nothing mechanical points at the host, so this is not a failure
        of search — it is a request for the one input the stage cannot compute."""
        fixture = self.make_fixture({HALF_PATCHED: listener_class(HALF_PATCHED)})
        resolution = self.resolve(SETTINGS_HOOK, fixture)
        self.assertIs(resolution.outcome, Outcome.NEEDS_AGENT)
        self.assertTrue(resolution.escalates)
        self.assertIn("no mechanical fingerprint", resolution.reason)
        self.assertIn(SETTINGS_HOOK.hook_id, resolution.reason)

    def test_a_proposal_that_exists_and_matches_resolves_like_any_candidate(self):
        # A proposed host is checked against the decode exactly like a
        # mechanically-found one; the agent chooses where to look, never whether
        # the anchor matched.
        fixture = self.make_fixture({CLEAN_TWIN: listener_class(CLEAN_TWIN)})
        resolution = self.resolve(SETTINGS_HOOK, fixture, proposals=(CLEAN_TWIN,))
        self.assertIs(resolution.outcome, Outcome.RESOLVED)
        self.assertEqual(resolution.descriptor, CLEAN_TWIN)
        self.assertEqual(resolution.candidates[0].found_by, "by_agent")
        assert resolution.resolution is not None
        self.assertEqual(
            resolution.resolution.bindings, {"l": "v0", "cls": "LX/0Dn9;", "a": "p1"}
        )
        self.assertEqual(resolution.resolution.smali_path, smali_path(CLEAN_TWIN))
        operation = resolution.as_operation(SETTINGS_HOOK)
        self.assertEqual(
            operation["payload"],
            [
                "    new-instance v0, Lcom/dfinstagram/SettingsWrapper;",
                "    invoke-direct {v0, p1}, Lcom/dfinstagram/SettingsWrapper;"
                "-><init>(I)V",
            ],
        )

    def test_a_proposal_absent_from_this_version_does_not_resolve(self):
        """Obfuscated names are recycled, so a carried-over descriptor is a trap.

        `LX/0Stale;` may well name a real class in the version the agent read it
        from. Here it names nothing, and the stage must say so rather than treat
        a proposal as evidence.
        """
        fixture = self.make_fixture({CLEAN_TWIN: listener_class(CLEAN_TWIN)})
        resolution = self.resolve(SETTINGS_HOOK, fixture, proposals=("LX/0Stale;",))
        self.assertIsNot(resolution.outcome, Outcome.RESOLVED)
        self.assertTrue(resolution.escalates)
        self.assertEqual(resolution.candidates, ())
        self.assertIn("LX/0Stale;", resolution.reason)
        self.assertIn("recycled", resolution.reason)
        self.assertIn("do not exist in this version", resolution.reason)

    def test_every_candidate_considered_is_recorded_with_its_own_reason(self):
        # A gate has to see what was considered and why it lost, not a score.
        fixture = self.make_fixture(
            {DECOY: analytics_class(DECOY), HOST: endpoint_class(HOST)},
            {"clips/discover/": [DECOY, HOST], "clips/homecoming/": [DECOY, HOST]},
        )
        resolution = self.resolve(ENDPOINT_HOOK, fixture)
        self.assertIs(resolution.outcome, Outcome.RESOLVED)
        self.assertEqual(resolution.descriptor, HOST)
        loser = next(item for item in resolution.candidates if item.descriptor == DECOY)
        self.assertFalse(loser.resolved)
        self.assertEqual(loser.found_by, "by_literal")
        self.assertEqual(loser.path, smali_path(DECOY))
        self.assertEqual(loser.reason, "anchor pattern did not match")


class HookResolutionTests(FixtureCase):
    def test_as_operation_raises_for_every_non_resolved_outcome(self):
        # There is exactly one outcome that has an operation behind it. Anything
        # else reaching the applier would be a fabricated patch.
        for outcome in Outcome:
            if outcome is Outcome.RESOLVED:
                continue
            with self.subTest(outcome=outcome.value):
                resolution = HookResolution("h", outcome, reason="because")
                with self.assertRaises(ManifestError) as caught:
                    resolution.as_operation(CONTEXT_HOOK)
                message = str(caught.exception)
                self.assertIn("h", message)
                self.assertIn(outcome.value, message)
                self.assertIn("because", message)

    def test_as_operation_raises_when_resolved_carries_no_resolution(self):
        # Both halves of the guard matter: the outcome alone is a label.
        resolution = HookResolution("h", Outcome.RESOLVED, reason="claimed")
        with self.assertRaises(ManifestError):
            resolution.as_operation(CONTEXT_HOOK)

    def test_only_resolved_and_already_applied_avoid_escalation(self):
        escalating = {outcome for outcome in Outcome if outcome.escalates}
        self.assertEqual(
            escalating,
            {
                Outcome.CONFLICT,
                Outcome.AMBIGUOUS,
                Outcome.UNRESOLVED,
                Outcome.NOT_FOUND,
                Outcome.NEEDS_AGENT,
            },
        )

    def test_to_dict_carries_the_verdict_and_the_losers(self):
        fixture = self.rerun_endpoint_decode()
        payload = self.resolve(ENDPOINT_HOOK, fixture).to_dict()
        self.assertEqual(payload["hook_id"], ENDPOINT_HOOK.hook_id)
        self.assertEqual(payload["outcome"], "already_applied")
        self.assertIs(payload["escalates"], False)
        self.assertEqual(payload["descriptor"], HOST)
        self.assertEqual(
            [item["descriptor"] for item in payload["candidates"]], [DECOY, HOST]
        )
        self.assertEqual(len(payload["searches"]), 1)


class ResolveReportTests(FixtureCase):
    """The whole-manifest view a gate reads."""

    def full_decode(self) -> Fixture:
        """One hook resolvable, one already applied, one needing an agent."""
        return self.make_fixture(
            {
                SHELL: CLEAN_SHELL,
                DECOY: endpoint_class(DECOY, "v3"),
                HOST: patched_endpoint_class(HOST),
                CLEAN_TWIN: listener_class(CLEAN_TWIN),
            },
            {"clips/discover/": [HOST], "clips/homecoming/": [HOST]},
        )

    def test_complete_means_every_hook_is_ready_or_already_applied(self):
        fixture = self.full_decode()
        report = resolve_manifest(
            [CONTEXT_HOOK, ENDPOINT_HOOK], fixture.index, fixture.decode
        )
        self.assertTrue(report.complete)
        self.assertEqual(report.escalations, ())
        self.assertEqual(
            [item.outcome for item in report.resolutions],
            [Outcome.RESOLVED, Outcome.ALREADY_APPLIED],
        )

    def test_escalations_lists_exactly_the_hooks_that_need_attention(self):
        fixture = self.full_decode()
        report = resolve_manifest(
            [CONTEXT_HOOK, ENDPOINT_HOOK, SETTINGS_HOOK], fixture.index, fixture.decode
        )
        self.assertFalse(report.complete)
        self.assertEqual(
            [item.hook_id for item in report.escalations], [SETTINGS_HOOK.hook_id]
        )

    def test_operations_skips_the_hooks_that_are_already_applied(self):
        # An already-applied hook has no work left; emitting an operation for it
        # would make the applier re-patch a class that already carries the marker.
        fixture = self.full_decode()
        report = resolve_manifest(
            [CONTEXT_HOOK, ENDPOINT_HOOK], fixture.index, fixture.decode
        )
        operations = report.operations([CONTEXT_HOOK, ENDPOINT_HOOK])
        self.assertEqual([item["id"] for item in operations], [CONTEXT_HOOK.hook_id])
        self.assertEqual(operations[0]["descriptor"], SHELL)

    def test_operations_refuses_to_emit_a_partial_list(self):
        """A short operation list is a build silently missing hooks.

        Nothing downstream re-counts them, so the quiet failure would be a
        shipped APK where a hook simply is not there.
        """
        fixture = self.full_decode()
        hooks = [CONTEXT_HOOK, ENDPOINT_HOOK, SETTINGS_HOOK]
        report = resolve_manifest(hooks, fixture.index, fixture.decode)
        with self.assertRaises(ManifestError) as caught:
            report.operations(hooks)
        message = str(caught.exception)
        self.assertIn("cannot emit operations", message)
        self.assertIn(SETTINGS_HOOK.hook_id, message)

    def test_operations_is_empty_but_valid_when_everything_is_already_applied(self):
        fixture = self.make_fixture(
            {DECOY: analytics_class(DECOY), HOST: patched_endpoint_class(HOST)},
            {"clips/discover/": [HOST], "clips/homecoming/": [HOST]},
        )
        report = resolve_manifest([ENDPOINT_HOOK], fixture.index, fixture.decode)
        self.assertTrue(report.complete)
        self.assertEqual(report.operations([ENDPOINT_HOOK]), [])

    def test_to_dict_survives_a_json_round_trip(self):
        # A gate renders this; anything unserialisable in it is a crash at the
        # end of a long run rather than here.
        fixture = self.full_decode()
        report = resolve_manifest(
            [CONTEXT_HOOK, ENDPOINT_HOOK, SETTINGS_HOOK], fixture.index, fixture.decode
        )
        payload = json.loads(json.dumps(report.to_dict()))
        self.assertEqual(payload["decode"], str(fixture.decode.resolve()))
        self.assertEqual(payload["index_decode"], str(fixture.decode.resolve()))
        self.assertEqual(payload["index_content_hash"], fixture.index.content_hash)
        self.assertIs(payload["complete"], False)
        self.assertEqual(
            payload["counts"], {"resolved": 1, "already_applied": 1, "needs_agent": 1}
        )
        self.assertEqual(len(payload["resolutions"]), 3)
        self.assertEqual(
            payload["resolutions"][1]["searches"][0]["evidence"]["classes_per_literal"],
            {"clips/discover/": 1, "clips/homecoming/": 1},
        )


class ResolveManifestTests(FixtureCase):
    def test_a_retired_hook_is_not_resolved_at_all(self):
        # Retired hooks stay in the manifest as history. Resolving them would
        # escalate on a host this version was never expected to have.
        fixture = self.make_fixture({SHELL: CLEAN_SHELL})
        report = resolve_manifest(
            [CONTEXT_HOOK, RETIRED_HOOK], fixture.index, fixture.decode
        )
        self.assertEqual(
            [item.hook_id for item in report.resolutions], [CONTEXT_HOOK.hook_id]
        )
        self.assertTrue(report.complete)

    def test_an_index_built_from_another_decode_is_refused(self):
        """Descriptors are recycled, so a cross-decode lookup is confidently wrong.

        `resolve_manifest` re-asserts the binding even when the caller loaded the
        index itself, because that is the one mistake the index cannot detect
        after the fact.
        """
        other = self.base / "other-decode"
        other.mkdir()
        fixture = self.make_fixture(
            {SHELL: CLEAN_SHELL}, decode_path=str(other.resolve())
        )
        with self.assertRaises(IndexUnusable) as caught:
            resolve_manifest([CONTEXT_HOOK], fixture.index, fixture.decode)
        message = str(caught.exception)
        self.assertIn(str(other.resolve()), message)
        self.assertIn(str(fixture.decode.resolve()), message)
        self.assertIn("recycled", message)

    def test_proposals_are_routed_to_the_hook_that_asked_for_them(self):
        fixture = self.make_fixture(
            {SHELL: CLEAN_SHELL, CLEAN_TWIN: listener_class(CLEAN_TWIN)}
        )
        report = resolve_manifest(
            [CONTEXT_HOOK, SETTINGS_HOOK],
            fixture.index,
            fixture.decode,
            {SETTINGS_HOOK.hook_id: [CLEAN_TWIN]},
        )
        self.assertTrue(report.complete)
        self.assertEqual(report.resolutions[1].descriptor, CLEAN_TWIN)

    def test_a_proposal_for_another_hook_is_not_borrowed(self):
        # Proposals are keyed by hook_id; a proposal meant for the UI hook must
        # not turn into a candidate for anything else.
        fixture = self.make_fixture({CLEAN_TWIN: listener_class(CLEAN_TWIN)})
        report = resolve_manifest(
            [SETTINGS_HOOK],
            fixture.index,
            fixture.decode,
            {"some_other_hook": [CLEAN_TWIN]},
        )
        self.assertIs(report.resolutions[0].outcome, Outcome.NEEDS_AGENT)

    def test_the_report_records_which_decode_and_index_produced_it(self):
        fixture = self.make_fixture({SHELL: CLEAN_SHELL})
        report = resolve_manifest([CONTEXT_HOOK], fixture.index, fixture.decode)
        self.assertEqual(report.decode, str(fixture.decode.resolve()))
        self.assertEqual(report.index_decode, str(fixture.decode.resolve()))
        self.assertEqual(report.index_content_hash, fixture.index.content_hash)

    def test_a_string_decode_path_is_accepted(self):
        fixture = self.make_fixture({SHELL: CLEAN_SHELL})
        report = resolve_manifest([CONTEXT_HOOK], fixture.index, str(fixture.decode))
        self.assertTrue(report.complete)


class CliTests(FixtureCase):
    """`main()` is what a gate actually invokes, so its exit codes are contract."""

    def test_exit_zero_when_every_hook_is_ready_or_applied(self):
        fixture = self.make_fixture({SHELL: CLEAN_SHELL})
        manifest = write_manifest(self.base / "hooks.json", [CONTEXT_HOOK])
        code, out, err = run_cli(
            [
                str(fixture.decode),
                "--index",
                str(fixture.index_dir),
                "--manifest",
                str(manifest),
            ]
        )
        self.assertEqual(code, 0)
        self.assertIn("resolved", out)
        self.assertIn(SHELL, out)
        self.assertEqual(err, "")

    def test_exit_one_when_a_hook_escalates(self):
        fixture = self.make_fixture({CLEAN_TWIN: listener_class(CLEAN_TWIN)})
        manifest = write_manifest(self.base / "hooks.json", [SETTINGS_HOOK])
        code, out, _ = run_cli(
            [
                str(fixture.decode),
                "--index",
                str(fixture.index_dir),
                "--manifest",
                str(manifest),
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("needs_agent", out)
        # The escalation prints its reason: the exit code alone is not actionable.
        self.assertIn("a proposed host is required", out)

    def test_exit_two_when_the_index_belongs_to_another_decode(self):
        # Distinct from 1 on purpose: nothing was resolved, so the run says
        # nothing about the hooks at all.
        other = self.base / "other-decode"
        other.mkdir()
        fixture = self.make_fixture(
            {SHELL: CLEAN_SHELL}, decode_path=str(other.resolve())
        )
        manifest = write_manifest(self.base / "hooks.json", [CONTEXT_HOOK])
        code, _, err = run_cli(
            [
                str(fixture.decode),
                "--index",
                str(fixture.index_dir),
                "--manifest",
                str(manifest),
            ]
        )
        self.assertEqual(code, 2)
        self.assertIn("recycled", err)

    def test_exit_two_when_the_index_directory_is_incomplete(self):
        fixture = self.make_fixture({SHELL: CLEAN_SHELL})
        (fixture.index_dir / "header.json").unlink()
        manifest = write_manifest(self.base / "hooks.json", [CONTEXT_HOOK])
        code, _, err = run_cli(
            [
                str(fixture.decode),
                "--index",
                str(fixture.index_dir),
                "--manifest",
                str(manifest),
            ]
        )
        self.assertEqual(code, 2)
        self.assertIn("incomplete", err)

    def test_operations_are_written_only_when_the_report_is_complete(self):
        fixture = self.make_fixture({SHELL: CLEAN_SHELL})
        manifest = write_manifest(self.base / "hooks.json", [CONTEXT_HOOK])
        operations = self.base / "operations.json"
        report_json = self.base / "report.json"
        code, _, _ = run_cli(
            [
                str(fixture.decode),
                "--index",
                str(fixture.index_dir),
                "--manifest",
                str(manifest),
                "--json",
                str(report_json),
                "--operations",
                str(operations),
            ]
        )
        self.assertEqual(code, 0)
        emitted = json.loads(operations.read_text(encoding="utf-8"))
        self.assertEqual([item["id"] for item in emitted], [CONTEXT_HOOK.hook_id])
        self.assertEqual(emitted[0]["descriptor"], SHELL)
        self.assertTrue(json.loads(report_json.read_text(encoding="utf-8"))["complete"])

    def test_operations_are_refused_while_anything_escalates(self):
        """The file must not exist at all, not exist-and-be-short.

        A later stage reading a partial operations file has no way to know a
        hook is missing from it.
        """
        fixture = self.make_fixture({SHELL: CLEAN_SHELL})
        manifest = write_manifest(self.base / "hooks.json", [CONTEXT_HOOK, SETTINGS_HOOK])
        operations = self.base / "operations.json"
        report_json = self.base / "report.json"
        code, _, err = run_cli(
            [
                str(fixture.decode),
                "--index",
                str(fixture.index_dir),
                "--manifest",
                str(manifest),
                "--json",
                str(report_json),
                "--operations",
                str(operations),
            ]
        )
        self.assertEqual(code, 1)
        self.assertFalse(operations.exists())
        self.assertIn("refusing to write operations", err)
        self.assertIn("1 hook(s) still escalate", err)
        # The full report is still written: it is what says why.
        self.assertFalse(json.loads(report_json.read_text(encoding="utf-8"))["complete"])

    def test_proposals_are_read_from_the_file_the_agent_writes(self):
        fixture = self.make_fixture({CLEAN_TWIN: listener_class(CLEAN_TWIN)})
        manifest = write_manifest(self.base / "hooks.json", [SETTINGS_HOOK])
        proposals = self.base / "proposals.json"
        proposals.write_text(
            json.dumps({SETTINGS_HOOK.hook_id: [CLEAN_TWIN]}), encoding="utf-8"
        )
        code, out, _ = run_cli(
            [
                str(fixture.decode),
                "--index",
                str(fixture.index_dir),
                "--manifest",
                str(manifest),
                "--proposals",
                str(proposals),
            ]
        )
        self.assertEqual(code, 0)
        self.assertIn(CLEAN_TWIN, out)

    def test_a_retired_hook_in_the_manifest_does_not_block_the_run(self):
        fixture = self.make_fixture({SHELL: CLEAN_SHELL})
        manifest = write_manifest(self.base / "hooks.json", [CONTEXT_HOOK, RETIRED_HOOK])
        operations = self.base / "operations.json"
        code, out, _ = run_cli(
            [
                str(fixture.decode),
                "--index",
                str(fixture.index_dir),
                "--manifest",
                str(manifest),
                "--operations",
                str(operations),
            ]
        )
        self.assertEqual(code, 0)
        self.assertNotIn(RETIRED_HOOK.hook_id, out)
        self.assertEqual(
            [item["id"] for item in json.loads(operations.read_text(encoding="utf-8"))],
            [CONTEXT_HOOK.hook_id],
        )


def partial_markers(
    candidates: Iterable[CandidateReport], expected: int
) -> list[CandidateReport]:
    """`_classify`'s rule 1, lifted out so a mutant can be run without it."""
    return [
        item
        for item in candidates
        if item.marker_count
        and not item.already_applied
        and item.marker_count != expected
    ]


def applied(candidates: Iterable[CandidateReport]) -> list[CandidateReport]:
    """`_classify`'s rule 2."""
    return [item for item in candidates if item.already_applied]


def resolved(candidates: Iterable[CandidateReport]) -> list[CandidateReport]:
    """`_classify`'s rule 3."""
    return [item for item in candidates if item.resolved]


class MutationTests(FixtureCase):
    """Each guard, shown biting.

    A guard that never changes an answer is decoration. These re-apply the real
    rules to the real recorded candidates in a broken order — which is exactly
    what the corresponding source mutation would do — and assert the answer
    changes. If one of these stops distinguishing, the fixture above has drifted
    and the precedence tests are passing for free.
    """

    def test_checking_resolved_before_already_applied_would_patch_a_decoy(self):
        """Mutation: swap rules 2 and 3.

        In production that patches a second, wrong class on every re-run of an
        already-patched decode, forever, and reports success while doing it.
        """
        fixture = self.rerun_endpoint_decode()
        resolution = self.resolve(ENDPOINT_HOOK, fixture)
        self.assertIs(resolution.outcome, Outcome.ALREADY_APPLIED)
        self.assertEqual(resolution.descriptor, HOST)

        # The mutant, run over the same candidates the real classifier recorded.
        winners = resolved(resolution.candidates)
        self.assertEqual([item.descriptor for item in winners], [DECOY])
        self.assertNotEqual(winners[0].descriptor, resolution.descriptor)
        # It would look like a clean success: one candidate, cleanly matched.
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0].reason, "")

    def test_dropping_the_partial_marker_check_would_patch_a_half_patched_decode(self):
        """Mutation: delete rule 1.

        In production the run then proceeds against a decode a previous run left
        half-written: the clean twin is patched, the mangled class is left as it
        is, and the build ships with one hook applied to two classes and neither
        of them consistent.
        """
        fixture = self.half_patched_settings_decode()
        resolution = self.resolve(
            SETTINGS_HOOK, fixture, proposals=(HALF_PATCHED, CLEAN_TWIN)
        )
        self.assertIs(resolution.outcome, Outcome.CONFLICT)
        self.assertEqual(
            [item.descriptor for item in partial_markers(resolution.candidates, 2)],
            [HALF_PATCHED],
        )

        # Without rule 1 nothing is already applied, one candidate resolves, and
        # the next rule in line returns RESOLVED against the clean twin.
        self.assertEqual(applied(resolution.candidates), [])
        winners = resolved(resolution.candidates)
        self.assertEqual([item.descriptor for item in winners], [CLEAN_TWIN])
        # The shipped stage names no host and stops; the mutant would hand the
        # applier a target on a decode it has no business writing to.
        self.assertIsNone(resolution.descriptor)
        self.assertNotEqual(winners[0].descriptor, resolution.descriptor)

    def test_operations_without_the_escalation_guard_would_emit_a_short_list(self):
        """Mutation: drop the raise from `ResolveReport.operations`.

        In production that is a build missing a hook nobody was told about — the
        list is well-formed, just shorter than the manifest.
        """
        fixture = self.make_fixture(
            {SHELL: CLEAN_SHELL, CLEAN_TWIN: listener_class(CLEAN_TWIN)}
        )
        hooks = [CONTEXT_HOOK, SETTINGS_HOOK]
        report = resolve_manifest(hooks, fixture.index, fixture.decode)
        with self.assertRaises(ManifestError):
            report.operations(hooks)

        by_id = {hook.hook_id: hook for hook in hooks}
        mutant = [
            item.as_operation(by_id[item.hook_id])
            for item in report.resolutions
            if item.outcome is Outcome.RESOLVED
        ]
        self.assertEqual(len(report.resolutions), 2)
        self.assertEqual([item["id"] for item in mutant], [CONTEXT_HOOK.hook_id])
        # Silently short: nothing in the emitted list mentions the missing hook.
        self.assertNotIn(SETTINGS_HOOK.hook_id, json.dumps(mutant))

    def test_dropping_the_by_anchor_prefilter_selects_exactly_the_same_classes(self):
        """Mutation: delete the prefilter. The answer must not move.

        This is the only optimisation in the stage that decides what gets looked
        at, and it fails in the direction that is hardest to notice: a fragment
        that is too narrow discards the host, the search comes back empty, and the
        report reads "the anchor matches no class in this decode" — which is
        indistinguishable from Instagram having moved the site. So the shipped
        path and the exhaustive one are run against the same decode and required
        to agree, and the prefilter is required to actually be discarding
        something, or the comparison is between two identical scans.
        """
        fixture = self.make_fixture(
            {
                DECOY: analytics_class(DECOY),
                HOST: endpoint_class(HOST),
                CLEAN_TWIN: listener_class(CLEAN_TWIN),
                HALF_PATCHED: anchor_patched_listener_class(HALF_PATCHED),
            }
        )
        fast = scan_for_anchor(ANCHOR_HOOK, fixture.decode)
        with mock.patch("dfinsta_pipeline.resolve.anchor_prefilter", return_value=""):
            exhaustive = scan_for_anchor(ANCHOR_HOOK, fixture.decode)

        self.assertEqual(fast.matched, exhaustive.matched)
        self.assertEqual(fast.carrying_marker, exhaustive.carrying_marker)
        self.assertEqual(fast.candidates, (HALF_PATCHED, CLEAN_TWIN))
        # The mutation has to be doing something, or agreement is free.
        self.assertEqual(fast.scanned, exhaustive.scanned)
        self.assertLess(fast.survivors, fast.scanned)
        self.assertEqual(exhaustive.survivors, exhaustive.scanned)
        self.assertEqual(exhaustive.prefilter, "")

    def test_the_already_applied_fixture_would_resolve_if_the_marker_were_absent(self):
        # The counterweight to the first mutation: the ordering only matters
        # because the marker is what distinguishes the host. Strip the patch and
        # the same decode is genuinely ambiguous, not already applied.
        fixture = self.clean_endpoint_decode()
        resolution = self.resolve(ENDPOINT_HOOK, fixture)
        self.assertIs(resolution.outcome, Outcome.AMBIGUOUS)
        self.assertEqual(applied(resolution.candidates), [])
        self.assertEqual(len(resolved(resolution.candidates)), 2)


class ReportedDefectTests(FixtureCase):
    """Regression tests for four defects this suite found and the module then fixed.

    Each docstring records what the defect would have cost in production, because
    that is the reason to keep the test rather than the reason it once failed.
    """

    def test_two_hooks_sharing_one_marker_are_refused_up_front(self):
        """A shared marker used to make one hook silently vanish from the build.

        A marker is a per-hook idempotence stamp. `manifest/hooks.json` shipped
        two hooks carrying the same one — `install_settings_long_click` and
        `install_settings_long_click_actionbar`, alternate implementations of a
        single feature. Applying either made the other read as already applied:
        it never escalated, emitted no operation, and the report still said the
        run was complete. A build quietly missing a hook is the exact outcome
        this stage exists to prevent, so the hook set is now refused before any
        resolution is attempted.

        The collision ran the other way too: a class holding both patches carried
        4 markers against an expected 2, so a correctly patched decode reported
        CONFLICT and demanded a re-extract.
        """
        fixture = self.make_fixture({CLEAN_TWIN: patched_listener_class(CLEAN_TWIN)})
        self.assertEqual(SETTINGS_HOOK.marker, SETTINGS_HOOK_SIBLING.marker)
        hooks = [SETTINGS_HOOK, SETTINGS_HOOK_SIBLING]
        with self.assertRaises(ManifestError) as caught:
            resolve_manifest(
                hooks,
                fixture.index,
                fixture.decode,
                {hook.hook_id: [CLEAN_TWIN] for hook in hooks},
            )
        message = str(caught.exception)
        self.assertIn(SETTINGS_HOOK.hook_id, message)
        self.assertIn(SETTINGS_HOOK_SIBLING.hook_id, message)
        self.assertIn("marker", message)

    def test_the_shipped_manifest_has_no_two_hooks_sharing_a_marker(self):
        """The real manifest, not just a synthetic one, must satisfy the rule."""
        manifest = Path(__file__).resolve().parents[1] / "manifest" / "hooks.json"
        if not manifest.is_file():
            self.skipTest("manifest/hooks.json is unavailable")
        hooks = load_manifest(manifest)
        markers = [hook.marker for hook in hooks]
        self.assertEqual(len(markers), len(set(markers)))

    def test_a_manifest_with_no_active_hook_is_not_a_complete_run(self):
        """Zero hooks resolved used to report success, with exit code 0.

        `complete` was "nothing escalated", which is trivially true when nothing
        resolved. An all-retired manifest produced an empty operations file and a
        clean exit, so a gate keying off the exit code would have let through a
        build with no hooks applied at all.
        """
        fixture = self.make_fixture({SHELL: CLEAN_SHELL})
        report = resolve_manifest([RETIRED_HOOK], fixture.index, fixture.decode)
        self.assertEqual(report.resolutions, ())
        self.assertFalse(report.complete)

        manifest = write_manifest(self.base / "hooks.json", [RETIRED_HOOK])
        operations = self.base / "operations.json"
        code, _, _ = run_cli(
            [
                str(fixture.decode),
                "--index",
                str(fixture.index_dir),
                "--manifest",
                str(manifest),
                "--operations",
                str(operations),
            ]
        )
        self.assertEqual(code, 1)
        self.assertFalse(operations.exists())

    def test_the_marker_count_behind_a_conflict_is_serialised(self):
        """The JSON report is what a gate renders.

        The number that produced a CONFLICT used to reach it only inside the
        prose reason, so nothing downstream could key on "1 of 2 markers present"
        without parsing English.
        """
        fixture = self.half_patched_settings_decode()
        resolution = self.resolve(
            SETTINGS_HOOK, fixture, proposals=(HALF_PATCHED, CLEAN_TWIN)
        )
        self.assertIs(resolution.outcome, Outcome.CONFLICT)
        recorded = next(
            item for item in resolution.candidates if item.descriptor == HALF_PATCHED
        )
        self.assertEqual(recorded.marker_count, 1)
        self.assertEqual(recorded.to_dict()["marker_count"], 1)
        rendered = json.loads(json.dumps(resolution.to_dict()))
        self.assertEqual(
            next(
                item
                for item in rendered["candidates"]
                if item["descriptor"] == HALF_PATCHED
            )["marker_count"],
            1,
        )

    def test_operations_names_the_hook_it_was_not_given(self):
        """It used to fail with a bare `KeyError` from a dict lookup.

        Passing only the active hooks — the natural subset, and what the stage
        itself resolved — was enough to trigger it, and the traceback named the
        lookup rather than saying which hook was missing from the call.
        """
        fixture = self.make_fixture({SHELL: CLEAN_SHELL})
        report = resolve_manifest([CONTEXT_HOOK], fixture.index, fixture.decode)
        with self.assertRaises(ManifestError) as caught:
            report.operations([])
        self.assertIn(CONTEXT_HOOK.hook_id, str(caught.exception))


# ----------------------------------------------------------------- real decodes

ROOT = Path(__file__).resolve().parents[1]
DECODE_439 = ROOT / "work" / "439-explore" / "stock-439"
DECODE_430 = ROOT / "work" / "430-clean-build-v2" / "stock-430"
INDEX_439 = ROOT / "work" / "index-439"
INDEX_430 = ROOT / "work" / "index-430"

#: The hosts two ports established by hand, and the numbers this stage now has to
#: reach without one. `install_settings_long_click` is the ProfileActionBar
#: variant, `..._actionbar` the legacy IgActionBar one; Instagram picks between
#: them at runtime, which is why both ship.
KNOWN_UI_HOSTS = {
    "439": (
        INDEX_439,
        DECODE_439,
        {
            "install_settings_long_click": "LX/0DnT;",
            "install_settings_long_click_actionbar": "LX/0Di2;",
        },
    ),
    "430": (
        INDEX_430,
        DECODE_430,
        {
            "install_settings_long_click": "LX/077K;",
            "install_settings_long_click_actionbar": "LX/06X7;",
        },
    ),
}

HAVE_REAL_DECODES = all(
    path.is_dir() for path in (DECODE_439, DECODE_430, INDEX_439, INDEX_430)
)

BY_ANCHOR = HostFingerprint(
    "by_anchor", note="the class whose body matches this hook's own anchor"
)


@unittest.skipUnless(HAVE_REAL_DECODES, "work/ decodes are absent (gitignored)")
class RealDecodeByAnchorTests(unittest.TestCase):
    """The measurement the kind exists for, against the two real decodes.

    Everything else in this file is synthetic, on purpose. This is not: the claim
    being made is that these two anchors — the manifest's, unmodified — pick out
    one class each in 181,421, and that the class is the one two hand-done ports
    arrived at. A fixture cannot say anything about that, because a fixture
    contains only the classes it was written to contain.

    Only the host fingerprint is swapped for `by_anchor`. The anchor, payload,
    marker and capture suppliers are the shipped ones, so what passes here is the
    thing that would ship.
    """

    #: (version, hook_id) -> the resolution, computed once. Each of the four is a
    #: full pass over a 1 GB decode, so re-resolving per test would cost the suite
    #: 25 seconds to answer two questions about the same four results.
    resolutions: dict[tuple[str, str], HookResolution] = {}
    class_counts: dict[str, int] = {}

    @classmethod
    def setUpClass(cls) -> None:
        hooks = {
            hook.hook_id: hook
            for hook in load_manifest(ROOT / "manifest" / "hooks.json")
        }
        for version, (index_dir, decode, hosts) in KNOWN_UI_HOSTS.items():
            index = HookIndex.for_decode(index_dir, decode)
            cls.class_counts[version] = index.class_count()
            for hook_id in hosts:
                hook = dataclasses.replace(hooks[hook_id], hosts=(BY_ANCHOR,))
                cls.resolutions[version, hook_id] = resolve_hook(hook, index, decode)

    def cases(self):
        for version, (_, _, hosts) in KNOWN_UI_HOSTS.items():
            for hook_id, expected in hosts.items():
                yield version, hook_id, expected, self.resolutions[version, hook_id]

    def test_each_ui_anchor_selects_exactly_the_known_host_on_both_versions(self):
        for version, hook_id, expected, result in self.cases():
            with self.subTest(version=version, hook=hook_id):
                evidence = result.searches[0].evidence
                # Claim one: the anchor is unique across the whole decode.
                self.assertEqual(evidence["anchor_matched"], [expected])
                self.assertEqual(evidence["classes_scanned"], self.class_counts[version])
                # Claim two: it matches once inside that class, which is what
                # RESOLVED needs and what `expected_anchor_count` has always meant.
                self.assertIs(result.outcome, Outcome.RESOLVED, result.reason)
                self.assertEqual(result.descriptor, expected)

    def test_the_prefilter_discards_almost_the_whole_decode(self):
        """The reason the search is seconds rather than minutes.

        An upper bound rather than the measured counts (986 and 26 of 181,421 on
        439), because the exact number is a property of one extracted artifact
        while "the prefilter is doing its job" is the property worth protecting.
        A prefilter that stopped narrowing would still be correct, and would make
        every port pay minutes per hook for it.
        """
        for version, hook_id, _, result in self.cases():
            with self.subTest(version=version, hook=hook_id):
                evidence = result.searches[0].evidence
                self.assertTrue(evidence["prefilter"])
                self.assertLess(
                    evidence["classes_prefiltered"], evidence["classes_scanned"] // 50
                )


if __name__ == "__main__":
    unittest.main()

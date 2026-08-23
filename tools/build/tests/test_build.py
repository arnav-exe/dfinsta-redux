import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch


TOOLS = Path(__file__).resolve().parents[1]
REPOSITORY = TOOLS.parents[1]
SOURCE = REPOSITORY / "tests/fixtures/dfinsta_source_430"
sys.path.insert(0, str(TOOLS))

from build import GRAFT_NAMES, graft_apk, main, sha256_tree
from prepare_tree import prepare, sanitise_manifest_for_aapt1
from verify_apk_430 import (
    FORBIDDEN_CUSTOM_SYMBOLS,
    REQUIRED_CUSTOM_SYMBOLS,
    expected_dex_names,
    payload_comparison,
    signature_context,
    verify,
    verify_structural_hooks,
)


class SourcePolicyTests(unittest.TestCase):
    def test_has_only_the_four_approved_custom_classes(self) -> None:
        smali_files = sorted((SOURCE / "newCode").rglob("*.smali"))
        descriptors = {
            re.search(
                r"^\.class .* (L[^;]+;)$",
                path.read_text(encoding="utf-8"),
                re.MULTILINE,
            ).group(1)
            for path in smali_files
        }

        self.assertEqual(len(smali_files), 4)
        self.assertEqual(descriptors, set(REQUIRED_CUSTOM_SYMBOLS))

    def test_has_no_manifest_or_resource_payload(self) -> None:
        self.assertFalse(any(path.is_file() for path in (SOURCE / "newRes").rglob("*")))
        self.assertFalse(any(path.is_file() for path in (SOURCE / "manifest").rglob("*")))

    def test_custom_source_excludes_forbidden_dependencies_and_fixed_app_ids(self) -> None:
        content = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((SOURCE / "newCode").rglob("*.smali"))
        )

        for symbol in FORBIDDEN_CUSTOM_SYMBOLS:
            with self.subTest(symbol=symbol):
                self.assertNotIn(symbol, content)
        self.assertNotIn("Ljava/lang/System;->exit", content)
        self.assertIsNone(re.search(r"\b0x7f[0-9a-f]+\b", content))

    def test_dialog_has_exact_labels_keys_defaults_and_apply(self) -> None:
        wrapper = (SOURCE / "newCode/com/dfinstagram/SettingsWrapper.smali").read_text(
            encoding="utf-8"
        )
        labels = re.findall(r'const-string v5, "(Disable [^"]+)"', wrapper)
        keys = re.findall(r'const-string v[05], "(disable_[^"]+)"', wrapper)

        self.assertEqual(
            labels,
            [
                "Disable feed",
                "Disable Explore",
                "Disable Reels",
                "Disable Stories",
                "Disable profile ads",
            ],
        )
        self.assertEqual(
            set(keys),
            {
                "disable_feed",
                "disable_explore",
                "disable_reels",
                "disable_stories",
                "disable_adds",
            },
        )
        self.assertEqual(wrapper.count("->getBoolean(Ljava/lang/String;Z)Z"), 5)
        self.assertEqual(wrapper.count("invoke-interface {v1, v5, v6}"), 5)
        self.assertIn("const/4 v6, 0x1", wrapper)
        self.assertEqual(wrapper.count("->putBoolean(Ljava/lang/String;Z)"), 1)
        self.assertEqual(wrapper.count("SharedPreferences$Editor;->apply()V"), 1)
        self.assertIn('const-string v1, "com.instagram"', wrapper)
        self.assertIn("Landroid/app/AlertDialog$Builder;", wrapper)
        self.assertIn("Landroid/content/DialogInterface$OnMultiChoiceClickListener;", wrapper)
        self.assertIn('const-string v0, "Close"', wrapper)
        self.assertIn("restart required", wrapper)

    def test_preference_reader_uses_true_default(self) -> None:
        reader = (SOURCE / "newCode/com/dfinstagram/dfinstagram.smali").read_text(
            encoding="utf-8"
        )
        self.assertIn('const-string v1, "com.instagram"', reader)
        self.assertRegex(
            reader,
            r"const/4 v1, 0x1\s+invoke-interface \{v0, p0, v1\}.*->getBoolean",
        )

    def test_reels_hook_uses_live_430_homecoming_path(self) -> None:
        hooks = (SOURCE / "newCode/com/dfinstagram/hooks.smali").read_text(
            encoding="utf-8"
        )

        self.assertIn('const-string v1, "/api/v1/clips/homecoming/"', hooks)
        self.assertNotIn('const-string v1, "/api/v1/clips/home/"', hooks)
        self.assertIn(
            "replaceReelsEndpoint(Ljava/lang/String;)Ljava/lang/String;", hooks
        )
        self.assertIn('const-string p0, ""', hooks)

    def test_profile_ads_replaces_retired_shopping_rule(self) -> None:
        hooks = (SOURCE / "newCode/com/dfinstagram/hooks.smali").read_text(
            encoding="utf-8"
        )

        self.assertIn('const-string v1, "/profile_ads/get_profile_ads/"', hooks)
        self.assertIn('const-string v1, "disable_adds"', hooks)
        self.assertNotIn("minishop", hooks)
        self.assertNotIn("disable_shopping", hooks)


class PrepareAndPatchTests(unittest.TestCase):
    def test_prepare_only_adds_classes20(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stock = root / "stock"
            source = root / "source"
            output = root / "output"
            (stock / "res/raw").mkdir(parents=True)
            (stock / "AndroidManifest.xml").write_text("stock", encoding="utf-8")
            (stock / "res/raw/item").write_bytes(b"resource")
            (source / "newCode/pkg").mkdir(parents=True)
            (source / "newCode/pkg/Only.smali").write_text("class", encoding="utf-8")

            prepare(stock, source, output)

            self.assertEqual((output / "AndroidManifest.xml").read_bytes(), b"stock")
            self.assertEqual((output / "res/raw/item").read_bytes(), b"resource")
            self.assertEqual((output / "smali_classes20/pkg/Only.smali").read_bytes(), b"class")

    def test_has_seven_exact_430_anchored_patches(self) -> None:
        manifest = json.loads(
            (SOURCE / "patches/anchored_patches.json").read_text(encoding="utf-8")
        )
        operations = {operation["id"]: operation for operation in manifest["operations"]}

        self.assertEqual(
            set(operations),
            {
                "set_app_context",
                "tigon_url_block",
                "install_settings_long_click",
                "install_settings_long_click_actionbar",
                "replace_reels_discover_endpoint",
                "replace_reels_homecoming_endpoint",
                "replace_reels_stream_endpoint",
            },
        )
        self.assertEqual(
            {
                operations[operation_id]["marker"]
                for operation_id in (
                    "set_app_context",
                    "tigon_url_block",
                    "install_settings_long_click",
                )
            },
            {
                "Lcom/dfinstagram/startapp;->setContext(Landroid/app/Application;)V",
                "Lcom/dfinstagram/hooks;->throwIfBlocked(Ljava/net/URI;)V",
                "Lcom/dfinstagram/SettingsWrapper;",
            },
        )
        self.assertEqual(
            operations["tigon_url_block"]["anchor"],
            [":try_start_0", "iget-object v1, p1, LX/05ez;->A08:Ljava/net/URI;"],
        )
        self.assertEqual(
            operations["install_settings_long_click"]["anchor"],
            [
                "new-instance v0, LX/0417;",
                "invoke-direct {v0, v3, p2, v6, p3}, LX/0417;-><init>(ILjava/lang/Object;Ljava/lang/Object;Ljava/lang/Object;)V",
                "invoke-static {v0, v6}, LX/00ZY;->A00(Landroid/view/View$OnClickListener;Landroid/view/View;)V",
            ],
        )
        payload = operations["install_settings_long_click"]["payload"]
        self.assertIn("    instance-of v0, p3, LX/077N;", payload)
        self.assertIn(
            "    invoke-virtual {v6, v0}, Landroid/view/View;->setOnLongClickListener(Landroid/view/View$OnLongClickListener;)V",
            payload,
        )
        actionbar = operations["install_settings_long_click_actionbar"]
        self.assertEqual(actionbar["descriptor"], "LX/06X7;")
        self.assertEqual(actionbar["mode"], "insert_after")
        self.assertEqual(actionbar["marker"], "Lcom/dfinstagram/SettingsWrapper;")
        self.assertEqual(
            actionbar["anchor"],
            [
                "iput-object v11, v1, LX/09rb;->A0F:Landroid/graphics/drawable/Drawable;",
                "const v0, 0x7f134a0e",
                "iput v0, v1, LX/09rb;->A06:I",
                "iput-object v14, v1, LX/09rb;->A0G:Landroid/view/View$OnClickListener;",
                "iput-object v13, v1, LX/09rb;->A0H:Landroid/view/View$OnLongClickListener;",
            ],
        )
        self.assertIn(
            "    new-instance v0, Lcom/dfinstagram/SettingsWrapper;",
            actionbar["payload"],
        )
        self.assertIn(
            "    iput-object v0, v1, LX/09rb;->A0H:Landroid/view/View$OnLongClickListener;",
            actionbar["payload"],
        )
        for operation_id, register, endpoint in (
            ("replace_reels_discover_endpoint", "v8", "clips/discover/"),
            ("replace_reels_homecoming_endpoint", "v9", "clips/homecoming/"),
            ("replace_reels_stream_endpoint", "v9", "clips/discover/stream/"),
        ):
            operation = operations[operation_id]
            self.assertEqual(operation["descriptor"], "LX/05t2;")
            self.assertEqual(operation["anchor"], [f'const-string {register}, "{endpoint}"'])
            self.assertIn(
                f"    invoke-static {{{register}}}, Lcom/dfinstagram/hooks;->replaceReelsEndpoint(Ljava/lang/String;)Ljava/lang/String;",
                operation["payload"],
            )


class ManifestSanitiserTests(unittest.TestCase):
    """Only the `<queries>` children aapt1 cannot compile are dropped.

    Instagram 440 added `<provider android:authorities="..."/>` inside `<queries>`,
    where matching is by authority so `android:name` is absent and must be. aapt1
    predates `<queries>` and validates its children by the rules for `<application>`,
    where the name is required, so the whole apktool build died on it. 439's manifest
    had 21 providers and every one carried a name, so 440 is the first time it bit.

    Editing this manifest is safe only because of the graft: the work tree is
    compiled to produce the *intermediate* APK and `build.graft_apk` takes nothing
    but DEX entries from it — `AndroidManifest.xml`, `resources.arsc` and `res/`
    are copied byte-for-byte out of the stock archive.
    """

    TOKEN_HANDOFF = '<provider android:authorities="com.facebook.pages.app.ig4work.tokenhandoff"/>'
    NAMED_PROVIDER = (
        '<provider android:name="com.instagram.contentprovider.Shared" '
        'android:authorities="com.instagram.android.shared"/>'
    )

    @staticmethod
    def manifest(queries: str = "", application: str = "") -> str:
        """A manifest shaped like apktool's output, with the two blocks filled in."""
        return (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<manifest xmlns:android="http://schemas.android.com/apk/res/android" '
            'package="com.instagram.android">\n'
            "    <queries>\n"
            f"{queries}"
            "    </queries>\n"
            '    <application android:label="Instagram">\n'
            f"{application}"
            "    </application>\n"
            "</manifest>\n"
        )

    def test_removes_a_nameless_queries_provider_and_names_it_with_a_reason(self) -> None:
        text = self.manifest(queries=f"        {self.TOKEN_HANDOFF}\n")

        cleaned, removed = sanitise_manifest_for_aapt1(text)

        self.assertNotIn("<provider", cleaned)
        self.assertEqual([item["element"] for item in removed], [self.TOKEN_HANDOFF])
        self.assertIn("aapt1", removed[0]["reason"])
        self.assertIn("android:name", removed[0]["reason"])

    def test_keeps_a_queries_provider_that_already_has_a_name(self) -> None:
        text = self.manifest(queries=f"        {self.NAMED_PROVIDER}\n")

        cleaned, removed = sanitise_manifest_for_aapt1(text)

        self.assertEqual(cleaned, text)
        self.assertEqual(removed, [])

    def test_leaves_a_nameless_provider_outside_queries_for_aapt1_to_reject(self) -> None:
        """Under `<application>` a nameless provider is genuinely malformed.

        Dropping it would hide a broken manifest behind a fix for a tooling
        limitation, so the build must still fail loudly on it.
        """
        malformed = '<provider android:authorities="com.instagram.android.broken"/>'
        text = self.manifest(
            queries=f"        {self.TOKEN_HANDOFF}\n",
            application=f"        {malformed}\n",
        )

        cleaned, removed = sanitise_manifest_for_aapt1(text)

        # Positive control: the same shape inside <queries> is removed by this very
        # call, so the survival below is the confinement to <queries> doing its job
        # and not a pattern that has stopped matching anything at all.
        self.assertEqual([item["element"] for item in removed], [self.TOKEN_HANDOFF])
        self.assertIn(malformed, cleaned)

    def test_a_manifest_with_nothing_to_remove_comes_back_byte_identical(self) -> None:
        text = self.manifest(
            queries='        <package android:name="com.facebook.katana"/>\n',
            application=f"        {self.NAMED_PROVIDER}\n",
        )

        cleaned, removed = sanitise_manifest_for_aapt1(text)

        self.assertEqual(cleaned, text)
        self.assertEqual(removed, [])

    def test_every_queries_block_is_scrubbed(self) -> None:
        first = '<provider android:authorities="com.facebook.first.tokenhandoff"/>'
        second = '<provider android:authorities="com.facebook.second.tokenhandoff"/>'
        text = (
            "<manifest>\n"
            "    <queries>\n"
            '        <package android:name="com.facebook.katana"/>\n'
            f"        {first}\n"
            "    </queries>\n"
            "    <queries>\n"
            f"        {second}\n"
            '        <package android:name="com.google.ar.core"/>\n'
            "    </queries>\n"
            "</manifest>\n"
        )

        cleaned, removed = sanitise_manifest_for_aapt1(text)

        self.assertEqual([item["element"] for item in removed], [first, second])
        self.assertNotIn("<provider", cleaned)
        self.assertEqual(cleaned.count("<package"), 2)

    def test_every_other_queries_child_survives(self) -> None:
        text = self.manifest(
            queries=(
                "        <intent>\n"
                '            <action android:name="android.intent.action.VIEW"/>\n'
                '            <data android:scheme="*"/>\n'
                "        </intent>\n"
                "        <intent>\n"
                '            <action android:name="android.intent.action.SENDTO"/>\n'
                '            <data android:scheme="mailto"/>\n'
                "        </intent>\n"
                "        <intent>\n"
                '            <action android:name="android.intent.action.MAIN"/>\n'
                "        </intent>\n"
                '        <package android:name="com.facebook.katana"/>\n'
                '        <package android:name="com.google.ar.core"/>\n'
                f"        {self.TOKEN_HANDOFF}\n"
            )
        )

        cleaned, removed = sanitise_manifest_for_aapt1(text)

        self.assertEqual(len(removed), 1)
        for element, expected in (("<intent>", 3), ("<action", 3), ("<data", 2), ("<package", 2)):
            with self.subTest(element=element):
                self.assertEqual(text.count(element), expected)
                self.assertEqual(cleaned.count(element), expected)

    def test_attribute_order_does_not_decide_whether_a_provider_is_removed(self) -> None:
        trailing_attribute = (
            '<provider android:authorities="com.facebook.pages.app.ig4work.tokenhandoff" '
            'android:exported="false"/>'
        )
        name_last = (
            '<provider android:exported="false" '
            'android:name="com.instagram.contentprovider.Shared"/>'
        )
        text = self.manifest(queries=f"        {trailing_attribute}\n        {name_last}\n")

        cleaned, removed = sanitise_manifest_for_aapt1(text)

        self.assertEqual([item["element"] for item in removed], [trailing_attribute])
        self.assertIn(name_last, cleaned)

    def test_removes_an_open_close_provider_whole_leaving_no_dangling_closer(self) -> None:
        """Matching the opening tag alone would leave a bare `</provider>` behind.

        apktool self-closes empty elements, so this is not the form 440 ships, but
        a dangling closer turns a tooling limitation into malformed XML and a
        different, more confusing build failure.
        """
        open_close = (
            '<provider android:authorities="com.facebook.pages.app.ig4work.tokenhandoff">'
            "</provider>"
        )
        text = self.manifest(
            queries=(
                '        <package android:name="com.facebook.katana"/>\n'
                f"        {open_close}\n"
                '        <package android:name="com.google.ar.core"/>\n'
            )
        )

        cleaned, removed = sanitise_manifest_for_aapt1(text)

        self.assertEqual([item["element"] for item in removed], [open_close])
        self.assertNotIn("provider", cleaned)
        self.assertEqual(cleaned.count("<package"), 2)

    def test_removes_an_open_close_provider_together_with_its_body(self) -> None:
        with_body = (
            '<provider android:authorities="com.facebook.pages.app.ig4work.tokenhandoff">'
            '<meta-data android:name="q"/>'
            "</provider>"
        )
        text = self.manifest(
            queries=(
                f"        {with_body}\n"
                '        <package android:name="com.facebook.katana"/>\n'
            )
        )

        cleaned, removed = sanitise_manifest_for_aapt1(text)

        self.assertEqual([item["element"] for item in removed], [with_body])
        self.assertNotIn("provider", cleaned)
        # The body leaves with the element that owned it, and a child's
        # android:name does not make the parent look named.
        self.assertNotIn("<meta-data", cleaned)
        self.assertEqual(cleaned.count("<package"), 1)

    def test_two_adjacent_nameless_providers_are_two_separate_removals(self) -> None:
        """A lazy body runs from the first `<provider` to the second's closer.

        That reports one removal and deletes both, so the count is what bites: a
        body forbidden from containing another provider tag cannot reach across.
        """
        first = '<provider android:authorities="com.facebook.first.tokenhandoff"/>'
        second = '<provider android:authorities="com.facebook.second.tokenhandoff"></provider>'
        text = self.manifest(queries=f"        {first}{second}\n")

        cleaned, removed = sanitise_manifest_for_aapt1(text)

        self.assertEqual(len(removed), 2)
        self.assertEqual([item["element"] for item in removed], [first, second])
        self.assertNotIn("provider", cleaned)

    def test_a_named_open_close_provider_after_a_nameless_one_is_not_swallowed(self) -> None:
        """The over-deletion that matters: a lazy body eats a declaration that was fine."""
        nameless = '<provider android:authorities="com.facebook.pages.app.ig4work.tokenhandoff"/>'
        named = '<provider android:name="com.instagram.contentprovider.Shared"></provider>'
        text = self.manifest(queries=f"        {nameless}{named}\n")

        cleaned, removed = sanitise_manifest_for_aapt1(text)

        self.assertEqual([item["element"] for item in removed], [nameless])
        self.assertIn(named, cleaned)

    def test_a_named_open_close_provider_before_a_nameless_one_survives(self) -> None:
        named = '<provider android:name="com.instagram.contentprovider.Shared"></provider>'
        nameless = '<provider android:authorities="com.facebook.pages.app.ig4work.tokenhandoff"/>'
        text = self.manifest(queries=f"        {named}{nameless}\n")

        cleaned, removed = sanitise_manifest_for_aapt1(text)

        self.assertEqual([item["element"] for item in removed], [nameless])
        self.assertIn(named, cleaned)

    def test_leaves_an_open_close_nameless_provider_outside_queries_alone(self) -> None:
        malformed = '<provider android:authorities="com.instagram.android.broken"></provider>'
        in_queries = (
            '<provider android:authorities="com.facebook.pages.app.ig4work.tokenhandoff">'
            "</provider>"
        )
        text = self.manifest(
            queries=f"        {in_queries}\n",
            application=f"        {malformed}\n",
        )

        cleaned, removed = sanitise_manifest_for_aapt1(text)

        # Positive control, as for the self-closing form: the same shape inside
        # <queries> is removed by this very call.
        self.assertEqual([item["element"] for item in removed], [in_queries])
        self.assertIn(malformed, cleaned)

    def test_prepare_removes_the_element_from_the_work_tree_and_reports_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stock = root / "stock"
            source = root / "source"
            output = root / "output"
            (stock / "smali/com/instagram").mkdir(parents=True)
            (stock / "smali/com/instagram/App.smali").write_text("stock", encoding="utf-8")
            (stock / "AndroidManifest.xml").write_text(
                self.manifest(queries=f"        {self.TOKEN_HANDOFF}\n"), encoding="utf-8"
            )
            (source / "newCode/com/dfinstagram").mkdir(parents=True)
            (source / "newCode/com/dfinstagram/hooks.smali").write_text("custom", encoding="utf-8")

            removed = prepare(stock, source, output)

            self.assertEqual([item["element"] for item in removed], [self.TOKEN_HANDOFF])
            self.assertNotIn("<provider", (output / "AndroidManifest.xml").read_text(encoding="utf-8"))
            # The decode is an input and is read, never edited: only the copy moves.
            self.assertIn(
                self.TOKEN_HANDOFF, (stock / "AndroidManifest.xml").read_text(encoding="utf-8")
            )
            self.assertEqual(
                (output / "smali_classes20/com/dfinstagram/hooks.smali").read_bytes(), b"custom"
            )

    def test_prepare_never_writes_a_manifest_it_had_nothing_to_remove_from(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stock = root / "stock"
            source = root / "source"
            output = root / "output"
            (stock / "smali").mkdir(parents=True)
            clean = self.manifest(queries='        <package android:name="com.facebook.katana"/>\n')
            stock_manifest = stock / "AndroidManifest.xml"
            stock_manifest.write_text(clean, encoding="utf-8")
            os.utime(stock_manifest, (1_000_000_000, 1_000_000_000))
            (source / "newCode").mkdir(parents=True)
            (source / "newCode/Only.smali").write_text("class", encoding="utf-8")

            removed = prepare(stock, source, output)

            work_manifest = output / "AndroidManifest.xml"
            self.assertEqual(removed, [])
            self.assertEqual(work_manifest.read_bytes(), clean.encode("utf-8"))
            # copytree preserves mtime, so the copy still carrying the decode's
            # timestamp is the only evidence that nothing rewrote it — identical
            # bytes cannot tell a no-op rewrite apart from no write at all.
            self.assertEqual(
                work_manifest.stat().st_mtime_ns, stock_manifest.stat().st_mtime_ns
            )

    def test_the_real_440_work_tree_manifest_loses_exactly_the_tokenhandoff_provider(self) -> None:
        """The format contract, taken from the manifest that actually broke the build."""
        manifest = REPOSITORY / "work" / "440-port" / "work-tree" / "AndroidManifest.xml"
        if not manifest.is_file():
            raise unittest.SkipTest(f"No 440 work tree manifest at {manifest}")
        text = manifest.read_text(encoding="utf-8")

        cleaned, removed = sanitise_manifest_for_aapt1(text)

        self.assertEqual([item["element"] for item in removed], [self.TOKEN_HANDOFF])
        self.assertEqual(text.count("<provider"), 22)
        self.assertEqual(cleaned.count("<provider"), 21)


class GraftTests(unittest.TestCase):
    @staticmethod
    def write_zip(path: Path, entries: dict[str, bytes]) -> None:
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in entries.items():
                archive.writestr(name, data)

    def test_replaces_only_graft_set_and_preserves_stock_entry_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stock = root / "stock.apk"
            intermediate = root / "intermediate.apk"
            output = root / "output.apk"
            stock_entries = {
                "AndroidManifest.xml": b"manifest",
                "resources.arsc": b"resources",
                "res/raw/a": b"a",
                "classes.dex": b"stock-1",
                "classes2.dex": b"stock-2",
                "classes3.dex": b"stock-3",
                "classes4.dex": b"stock-4",
                "classes6.dex": b"stock-6",
                "META-INF/MANIFEST.MF": b"signature",
                "META-INF/CERT.RSA": b"signature",
                "META-INF/keep.txt": b"keep",
            }
            replacements = {name: f"new-{name}".encode("utf-8") for name in GRAFT_NAMES}
            self.write_zip(stock, stock_entries)
            self.write_zip(intermediate, replacements)

            graft_apk(stock, intermediate, output)

            with zipfile.ZipFile(output) as archive:
                final_entries = {name: archive.read(name) for name in archive.namelist()}
            self.assertEqual(
                set(final_entries),
                set(stock_entries) - {"META-INF/MANIFEST.MF", "META-INF/CERT.RSA"}
                | {"classes20.dex"},
            )
            for name, data in stock_entries.items():
                if name not in GRAFT_NAMES and not name.startswith("META-INF/MANIFEST") and not name.endswith(".RSA"):
                    self.assertEqual(final_entries[name], data)
            for name, data in replacements.items():
                self.assertEqual(final_entries[name], data)

    def test_refuses_existing_output_and_duplicate_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stock = root / "stock.apk"
            intermediate = root / "intermediate.apk"
            output = root / "output.apk"
            self.write_zip(stock, {name: b"stock" for name in GRAFT_NAMES - {"classes20.dex"}})
            self.write_zip(intermediate, {name: b"new" for name in GRAFT_NAMES})
            output.write_bytes(b"exists")
            with self.assertRaises(FileExistsError):
                graft_apk(stock, intermediate, output)

            output.unlink()
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(stock, "a") as archive:
                    archive.writestr("classes.dex", b"duplicate")
            with self.assertRaises(ValueError):
                graft_apk(stock, intermediate, output)

    def test_tree_hash_includes_relative_paths_and_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a").mkdir()
            (root / "a/file").write_bytes(b"one")
            first = sha256_tree(root)
            (root / "a/file").write_bytes(b"two")
            self.assertNotEqual(first, sha256_tree(root))
            (root / "a/file").rename(root / "renamed")
            self.assertNotEqual(first, sha256_tree(root))


class FirstCommand(Exception):
    """Stands in for the first subprocess `main` would spawn, so only the guard runs."""


class BuildGuardTests(unittest.TestCase):
    """`main`'s overwrite guard must refuse what the build produces, and only that."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.stock_apk = root / "stock.apk"
        self.patch_source = root / "source"
        self.apktool_jar = root / "apktool.jar"
        self.framework_apk = root / "framework.apk"
        self.framework_path = root / "framework-cache"
        self.stock_decode = root / "decode"
        self.work_tree = root / "work"
        self.output_apk = root / "out" / "dfinsta.apk"
        self.stock_apk.write_bytes(b"stock")
        self.apktool_jar.write_bytes(b"jar")
        self.framework_apk.write_bytes(b"framework")
        self.patch_source.mkdir()
        (self.patch_source / "payload").write_bytes(b"payload")
        self.output_apk.parent.mkdir()
        # Spelled out rather than imported from build, so that changing how a name
        # is derived from the arguments is a test failure and not a silent rename.
        self.intermediate_apk = self.output_apk.parent / "dfinsta-intermediate.apk"
        self.anchored_report = root / "work-anchored-report.json"
        self.verification_report = self.output_apk.parent / "dfinsta.verification.json"
        self.build_report = self.output_apk.parent / "dfinsta.build.json"

    def build(self) -> None:
        """Run `main` far enough to clear the guard; the first command raises instead."""
        argv = [
            "build.py",
            str(self.stock_decode),
            str(self.stock_apk),
            str(self.patch_source),
            str(self.apktool_jar),
            str(self.framework_apk),
            "--framework-path",
            str(self.framework_path),
            "--work-tree",
            str(self.work_tree),
            "--output-apk",
            str(self.output_apk),
        ]
        with patch("build.subprocess.run", side_effect=FirstCommand), patch.object(sys, "argv", argv):
            main()

    def assert_refuses(self, path: Path) -> None:
        with self.assertRaises(FileExistsError) as caught:
            self.build()
        self.assertIn(str(path), str(caught.exception))

    def test_reaches_the_first_command_when_no_produced_path_exists(self) -> None:
        with self.assertRaises(FirstCommand):
            self.build()

    def test_existing_framework_cache_is_an_input_and_is_not_refused(self) -> None:
        self.framework_path.mkdir()
        (self.framework_path / "1.apk").write_bytes(b"installed by an earlier extract")

        with self.assertRaises(FirstCommand):
            self.build()

        # Control: the same run refuses a produced artifact, so reaching the first
        # command above is the guard passing the framework cache, not the guard
        # being skipped or every path being misspelled.
        self.output_apk.write_bytes(b"previous build")
        self.assert_refuses(self.output_apk)

    def test_refuses_an_existing_stock_decode_tree(self) -> None:
        self.stock_decode.mkdir()
        self.assert_refuses(self.stock_decode)

    def test_refuses_an_existing_work_tree(self) -> None:
        self.work_tree.mkdir()
        self.assert_refuses(self.work_tree)

    def test_refuses_an_existing_output_apk(self) -> None:
        self.output_apk.write_bytes(b"previous build")
        self.assert_refuses(self.output_apk)

    def test_refuses_an_existing_intermediate_apk_derived_from_the_output(self) -> None:
        self.intermediate_apk.write_bytes(b"previous build")
        self.assert_refuses(self.intermediate_apk)

    def test_refuses_an_existing_anchored_report_derived_from_the_work_tree(self) -> None:
        self.anchored_report.write_text("{}", encoding="utf-8")
        self.assert_refuses(self.anchored_report)

    def test_refuses_an_existing_verification_report_derived_from_the_output(self) -> None:
        self.verification_report.write_text("{}", encoding="utf-8")
        self.assert_refuses(self.verification_report)

    def test_refuses_an_existing_build_report_derived_from_the_output(self) -> None:
        self.build_report.write_text("{}", encoding="utf-8")
        self.assert_refuses(self.build_report)


class VerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dex_names = expected_dex_names()
        self.dex_content = {name: b"stock" for name in self.dex_names}
        self.dex_content["classes20.dex"] = " ".join(REQUIRED_CUSTOM_SYMBOLS).encode("utf-8")
        self.structural_hooks = {"hook": True}
        self.stock_entries = {
            "AndroidManifest.xml": b"manifest",
            "resources.arsc": b"arsc",
            "res/a": b"resource",
            "assets/a": b"asset",
        }
        self.final_entries = dict(self.stock_entries)

    def verify(self, **kwargs) -> dict:
        return verify(
            self.dex_names,
            self.dex_content,
            self.final_entries,
            self.stock_entries,
            self.structural_hooks,
            *payload_comparison(self.final_entries, self.stock_entries),
            **kwargs,
        )

    def test_accepts_exact_dex_symbols_hooks_and_resources(self) -> None:
        self.assertTrue(self.verify()["passed"])

    def test_rejects_wrong_dex_set_or_extra_custom_class(self) -> None:
        self.dex_names.pop()
        self.assertFalse(self.verify()["passed"])
        self.dex_names = expected_dex_names()
        self.dex_content["classes20.dex"] += b" Lcom/dfinstagram/Extra;"
        self.assertFalse(self.verify()["passed"])

    def test_an_extra_custom_class_fails_unless_the_caller_allows_it(self) -> None:
        """The probe class made the old equality check unpassable.

        `exact_custom_symbols` was `custom_symbols == set(REQUIRED_CUSTOM_SYMBOLS)`,
        and a probe-instrumented build's custom DEX also carries
        `Lcom/dfinstagram/probe;` — so the one kind of build that can attribute
        hook execution could never pass. `verify_build.py` used a superset check
        all along and passed, which is why nothing noticed.

        The allowance is the caller's, not a second module-level list, so an
        unexpected class still fails by default.
        """
        probe = "Lcom/dfinstagram/probe;"
        self.dex_content["classes20.dex"] += f" {probe}".encode("utf-8")
        result = self.verify()
        self.assertEqual(result["unexpected_custom_symbols"], [probe])
        self.assertFalse(result["passed"])

        allowed = self.verify(allowed_custom_symbols=[probe])
        self.assertEqual(allowed["unexpected_custom_symbols"], [])
        self.assertEqual(allowed["allowed_custom_symbols"], [probe])
        self.assertTrue(allowed["passed"])

        # And the allowance is exactly what it names: a different extra class
        # still fails while the probe is permitted.
        self.dex_content["classes20.dex"] += b" Lcom/dfinstagram/Extra;"
        still = self.verify(allowed_custom_symbols=[probe])
        self.assertEqual(still["unexpected_custom_symbols"], ["Lcom/dfinstagram/Extra;"])
        self.assertFalse(still["passed"])

    def test_rejects_missing_hook_activity_or_resource_change(self) -> None:
        self.structural_hooks["hook"] = False
        self.assertFalse(self.verify()["passed"])
        self.structural_hooks["hook"] = True
        self.dex_content["classes20.dex"] += b" Landroid/app/Activity;"
        self.assertFalse(self.verify()["passed"])
        self.dex_content["classes20.dex"] = " ".join(REQUIRED_CUSTOM_SYMBOLS).encode("utf-8")
        self.final_entries["res/a"] = b"changed"
        self.assertFalse(self.verify()["passed"])

    def test_rejects_changed_retained_payload(self) -> None:
        self.final_entries["assets/a"] = b"changed"
        result = self.verify()
        self.assertFalse(result["retained_payload_entry_bytes_equal"])
        self.assertFalse(result["passed"])

    def test_structural_hooks_require_exact_methods_and_sequences(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {
                "smali/com/instagram/api/tigon/TigonServiceLayer.smali": """
.method public startRequest(LX/05ez;LX/05fq;LX/05gu;)LX/0Fcm;
    iget-object v1, p1, LX/05ez;->A08:Ljava/net/URI;
    invoke-static {v1}, Lcom/dfinstagram/hooks;->throwIfBlocked(Ljava/net/URI;)V
.end method
""",
                "smali_classes3/com/instagram/app/InstagramAppShell.smali": """
.method public onCreate()V
    invoke-super {v0}, Landroid/app/Application;->onCreate()V
    invoke-static {v0}, Lcom/dfinstagram/startapp;->setContext(Landroid/app/Application;)V
.end method
""",
                "smali_classes4/X/05t2.smali": """
.method public A07(Landroid/content/Context;LX/0HSu;LX/0Jin;LX/0Jej;Lcom/instagram/common/session/UserSession;LX/0Jae;Ljava/lang/Integer;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Lkotlin/jvm/functions/Function0;ZZZZ)LX/017H;
    const-string v8, "clips/discover/"
    invoke-static {v8}, Lcom/dfinstagram/hooks;->replaceReelsEndpoint(Ljava/lang/String;)Ljava/lang/String;
    move-result-object v8
.end method
.method public A09(Landroid/content/Context;LX/0HSu;LX/0Jin;LX/0Jej;Lcom/instagram/common/session/UserSession;LX/0Jae;Ljava/lang/Integer;Ljava/lang/Long;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/util/List;Lkotlin/jvm/functions/Function0;ZZZZZZZZZZZ)LX/03xp;
    const-string v9, "clips/homecoming/"
    invoke-static {v9}, Lcom/dfinstagram/hooks;->replaceReelsEndpoint(Ljava/lang/String;)Ljava/lang/String;
    move-result-object v9
    const-string v9, "clips/discover/stream/"
    invoke-static {v9}, Lcom/dfinstagram/hooks;->replaceReelsEndpoint(Ljava/lang/String;)Ljava/lang/String;
    move-result-object v9
.end method
""",
                "smali_classes6/X/077K.smali": """
.method public A00(Landroid/content/Context;Lcom/instagram/common/session/UserSession;LX/077F;LX/0JxZ;)Landroid/widget/ImageView;
    invoke-static {v0, v6}, LX/00ZY;->A00(Landroid/view/View$OnClickListener;Landroid/view/View;)V
    instance-of v0, p3, LX/077N;
    if-eqz v0, :cond_0
    new-instance v0, Lcom/dfinstagram/SettingsWrapper;
    invoke-direct {v0}, Lcom/dfinstagram/SettingsWrapper;-><init>()V
    invoke-virtual {v6, v0}, Landroid/view/View;->setOnLongClickListener(Landroid/view/View$OnLongClickListener;)V
    :cond_0
.end method
""",
            }
            for relative, content in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            checks = verify_structural_hooks(root)
            self.assertTrue(all(checks.values()))
            settings = root / "smali_classes6/X/077K.smali"
            settings.write_text(
                files["smali_classes6/X/077K.smali"].replace("LX/077N;", "LX/077e;"),
                encoding="utf-8",
            )
            self.assertFalse(verify_structural_hooks(root)["settings_guarded_after_stock_click"])
            settings.write_text(files["smali_classes6/X/077K.smali"], encoding="utf-8")
            tigon = root / "smali/com/instagram/api/tigon/TigonServiceLayer.smali"
            tigon.write_text(
                files["smali/com/instagram/api/tigon/TigonServiceLayer.smali"].replace(
                    "invoke-static {v1}", "invoke-static {v2}"
                ),
                encoding="utf-8",
            )
            self.assertFalse(verify_structural_hooks(root)["tigon_start_request_sequence"])
            tigon.write_text(
                files["smali/com/instagram/api/tigon/TigonServiceLayer.smali"].replace(
                    "startRequest(LX/05ez;LX/05fq;LX/05gu;)LX/0Fcm;", "startRequest()V"
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                verify_structural_hooks(root)

    @patch("verify_apk_430.subprocess.run")
    def test_records_signature_verification(self, run_mock) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            apk = root / "app.apk"
            apksigner = root / "apksigner"
            apk.write_bytes(b"apk")
            apksigner.write_bytes(b"tool")
            run_mock.return_value = subprocess.CompletedProcess(
                [], 0, "Signer #1 certificate SHA-256 digest: abc\n", ""
            )
            result = signature_context(apk, apksigner)
            self.assertTrue(result["verified"])
            self.assertEqual(result["output"], ["Signer #1 certificate SHA-256 digest: abc"])

    @patch("verify_apk_430.subprocess.run")
    def test_rejects_failed_or_unapproved_signature(self, run_mock) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            apk = root / "app.apk"
            apksigner = root / "apksigner"
            apk.write_bytes(b"apk")
            apksigner.write_bytes(b"tool")
            digest = "a" * 64
            run_mock.return_value = subprocess.CompletedProcess(
                [], 0, f"Signer #1 certificate SHA-256 digest: {digest}\n", ""
            )
            self.assertFalse(signature_context(apk, apksigner, "b" * 64)["approved_signer"])
            run_mock.return_value = subprocess.CompletedProcess([], 1, "", "bad signature")
            self.assertFalse(signature_context(apk, apksigner)["verified"])


if __name__ == "__main__":
    unittest.main()

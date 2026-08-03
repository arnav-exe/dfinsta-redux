"""Tests for `tools/verify/verify_build.py`: the target-neutral static verifier.

This is the check standing between a built APK and a release, and until now it
had no tests. Every fact it claims in its own docstring is pinned here, one
named test each.

No apktool, no java, no adb, and nothing under `apks/` or `work/` is touched.
The fixtures are ordinary zip files built with `zipfile`, in the shape
`tools/port_430/build.py:graft_apk` produces: the stock archive minus its
signature entries, with a few `classesN.dex` entries replaced and one new
`classesN.dex` added. The "DEX" payloads are not real DEX — the verifier only
greps raw bytes for type descriptors and bare method names, so bytes containing
(or not containing) those strings are a faithful stand-in, and `zipfile` is the
only thing standing between the fixture and the code under test.

One fixture detail is load-bearing. The custom DEX body contains both the
`Lcom/dfinstagram/...;` descriptors *and* the bare method names `setContext`,
`throwIfBlocked`, `replaceReelsEndpoint`, `onLongClick`, because a real custom
DEX defines those methods and so carries both strings in its string table. That
is exactly why `host_hooks` has to be checked in the *named* DEX: searching the
whole archive would find every hook string in the custom DEX and prove nothing.
`test_a_host_hook_present_only_in_another_grafted_dex_still_fails` pins it.

`ArchiveShapeTests` covers the four defects this file's first run found and the
verifier has since fixed: an entry the build invented, a duplicate archive name
hiding the unpatched original behind the patched one, a miscounted
`preserved_entry_count`, and the three absent-entry shapes that used to raise a
bare `KeyError` out of a verifier that had not written its report yet.
"""

import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
import warnings
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
VERIFIER_PATH = ROOT / "tools" / "verify" / "verify_build.py"
FINALIZE_PATH = ROOT / "tools" / "release" / "finalize.py"


def _load(name: str, path: Path):
    """Import a `tools/` script by path; neither directory is a package."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


verify_build = _load("verify_build", VERIFIER_PATH)
# The real consumer of the report. Imported and called, rather than restated,
# so the envelope tests pin the contract instead of a copy of it.
finalize = _load("release_finalize", FINALIZE_PATH)


CUSTOM_DEX = "classes21.dex"
REPLACED = ("classes.dex", "classes3.dex", "classes6.dex")

#: Same shape and same content as `verify_build.DEFAULT_HOST_HOOKS`, spelled out
#: here because every other test mutates it.
HOST_HOOKS = {
    "classes.dex": [("Lcom/dfinstagram/hooks;", "throwIfBlocked")],
    "classes3.dex": [
        ("Lcom/dfinstagram/startapp;", "setContext"),
        ("Lcom/dfinstagram/hooks;", "replaceReelsEndpoint"),
    ],
    "classes6.dex": [("Lcom/dfinstagram/SettingsWrapper;", "onLongClick")],
}

CUSTOM_BODY = (
    "Lcom/dfinstagram/startapp; setContext "
    "Lcom/dfinstagram/dfinstagram; "
    "Lcom/dfinstagram/hooks; throwIfBlocked replaceReelsEndpoint "
    "Lcom/dfinstagram/SettingsWrapper; onLongClick"
)


def dex(body: str) -> bytes:
    return b"dex\n035\x00" + body.encode("utf-8")


STOCK_ENTRIES = {
    "AndroidManifest.xml": b"<manifest/>",
    "resources.arsc": b"arsc-bytes",
    "res/drawable/ic_home.png": b"png-bytes",
    "assets/config.json": b"{}",
    "lib/arm64-v8a/libinstagram.so": b"elf-bytes",
    # META-INF, but not a signature: must still be preservation-checked.
    "META-INF/services/x.Provider": b"provider",
    "classes.dex": dex("stock classes"),
    "classes2.dex": dex("stock classes2"),
    "classes3.dex": dex("stock classes3"),
    "classes6.dex": dex("stock classes6"),
    "META-INF/MANIFEST.MF": b"stock-manifest",
    "META-INF/CERT.SF": b"stock-sf",
    "META-INF/CERT.RSA": b"stock-rsa",
}

BUILT_ENTRIES = {
    name: data
    for name, data in STOCK_ENTRIES.items()
    if not verify_build.is_signature_entry(name)
}
BUILT_ENTRIES["classes.dex"] = dex("stock classes Lcom/dfinstagram/hooks; throwIfBlocked")
BUILT_ENTRIES["classes3.dex"] = dex(
    "stock classes3 Lcom/dfinstagram/startapp; setContext "
    "Lcom/dfinstagram/hooks; replaceReelsEndpoint"
)
BUILT_ENTRIES["classes6.dex"] = dex(
    "stock classes6 Lcom/dfinstagram/SettingsWrapper; onLongClick"
)
BUILT_ENTRIES[CUSTOM_DEX] = dex(CUSTOM_BODY)

_UNSET = object()

#: Every top-level key `verify` reports. Asserted whole wherever a test claims
#: "the report was produced": a run that raised part-way would be short of keys,
#: and the release gate reads this shape.
REPORT_KEYS = frozenset(
    {
        "duplicate_entries",
        "stock_dex_count",
        "built_dex_count",
        "custom_dex_is_new",
        "dex_topology_exact",
        "expected_entries_absent",
        "replaced_entries_absent_from_stock",
        "custom_required_symbols",
        "custom_forbidden_symbols",
        "host_hooks",
        "grafted_dex_changed",
        "added_entries",
        "preserved_entry_count",
        "preserved_entries_missing",
        "preserved_entries_mismatched",
        "signatures_stripped",
        "passed",
    }
)


def sha256_bytes_of_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class GraftFixture(unittest.TestCase):
    """A well-formed graft, as mutable dicts, plus the call under test."""

    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)
        self.stock_entries = dict(STOCK_ENTRIES)
        self.built_entries = dict(BUILT_ENTRIES)
        self.custom_dex = CUSTOM_DEX
        self.replaced = set(REPLACED)
        self.host_hooks = {dex_name: list(pairs) for dex_name, pairs in HOST_HOOKS.items()}
        self.appended: tuple[tuple[str, bytes], ...] = ()

    @staticmethod
    def write_zip(
        path: Path,
        entries: dict[str, bytes],
        appended: tuple[tuple[str, bytes], ...] = (),
    ) -> Path:
        """`appended` writes further entries after `entries`, names and all.

        A ZIP is a list, not a mapping: writing a name twice is legal and is the
        only way to build the archive `test_a_duplicate_entry...` needs.
        `zipfile` warns about it, and this suite runs under `-W error`.
        """
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
            for name, data in appended:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    archive.writestr(name, data)
        return path

    def apks(self) -> tuple[Path, Path]:
        return (
            self.write_zip(self.root / "built.apk", self.built_entries, self.appended),
            self.write_zip(self.root / "stock.apk", self.stock_entries),
        )

    def verify(self, host_hooks=_UNSET) -> dict:
        built, stock = self.apks()
        hooks = self.host_hooks if host_hooks is _UNSET else host_hooks
        return verify_build.verify(built, stock, self.custom_dex, self.replaced, hooks)

    def set_custom_body(self, body: str) -> None:
        self.built_entries[self.custom_dex] = dex(body)

    def assert_failing_verdicts(self, results: dict, *expected: str) -> None:
        """Exactly these terms of the `passed` conjunction are false.

        Naming every failure, not just the one the test is about, is what makes
        a test provably isolate its fact — and where a single defect is caught
        by more than one verdict, it says so instead of hiding the overlap.
        """
        verdicts = {
            "dex_topology_exact": results["dex_topology_exact"],
            "custom_dex_is_new": results["custom_dex_is_new"],
            "custom_required_symbols": all(results["custom_required_symbols"].values()),
            "custom_forbidden_symbols": not any(results["custom_forbidden_symbols"].values()),
            "host_hooks": all(all(v.values()) for v in results["host_hooks"].values()),
            "grafted_dex_changed": all(results["grafted_dex_changed"].values()),
            "preserved_entries_missing": not results["preserved_entries_missing"],
            "preserved_entries_mismatched": not results["preserved_entries_mismatched"],
            "added_entries": not results["added_entries"],
            "duplicate_entries": not results["duplicate_entries"],
            "expected_entries_absent": not results["expected_entries_absent"],
            "replaced_entries_absent_from_stock": not results["replaced_entries_absent_from_stock"],
            "signatures_stripped": results["signatures_stripped"],
        }
        self.assertEqual(sorted(set(expected) - set(verdicts)), [], "unknown verdict name")
        self.assertEqual(
            sorted(name for name, held in verdicts.items() if not held),
            sorted(expected),
        )
        self.assertFalse(results["passed"])
        self.assertEqual(set(results), REPORT_KEYS)


class WellFormedGraftTests(GraftFixture):
    def test_a_well_formed_graft_passes_every_check(self) -> None:
        """The positive control. Every other test here asserts an absence."""
        results = self.verify()

        self.assertTrue(results["dex_topology_exact"])
        self.assertTrue(results["custom_dex_is_new"])
        self.assertEqual(
            results["custom_required_symbols"],
            {symbol: True for symbol in verify_build.REQUIRED_CUSTOM_SYMBOLS},
        )
        self.assertEqual(
            results["custom_forbidden_symbols"],
            {symbol: False for symbol in verify_build.FORBIDDEN_CUSTOM_SYMBOLS},
        )
        self.assertTrue(all(all(v.values()) for v in results["host_hooks"].values()))
        self.assertEqual(
            results["grafted_dex_changed"],
            {name: True for name in REPLACED},
        )
        self.assertEqual(results["preserved_entries_missing"], [])
        self.assertEqual(results["preserved_entries_mismatched"], [])
        self.assertEqual(results["added_entries"], [])
        self.assertEqual(results["duplicate_entries"], [])
        self.assertEqual(results["expected_entries_absent"], [])
        self.assertEqual(results["replaced_entries_absent_from_stock"], [])
        self.assertTrue(results["signatures_stripped"])
        self.assertEqual(results["stock_dex_count"], 4)
        self.assertEqual(results["built_dex_count"], 5)
        self.assertEqual(set(results), REPORT_KEYS)
        self.assertIs(results["passed"], True)

    def test_the_default_host_hook_map_is_used_when_none_is_supplied(self) -> None:
        """`None` means the recorded 439 map; only `{}` is the vacuous refusal."""
        self.assertEqual(
            {name: [tuple(pair) for pair in pairs] for name, pairs in HOST_HOOKS.items()},
            {
                name: [tuple(pair) for pair in pairs]
                for name, pairs in verify_build.DEFAULT_HOST_HOOKS.items()
            },
        )

        results = self.verify(host_hooks=None)

        self.assertIs(results["passed"], True)
        self.assertEqual(sorted(results["host_hooks"]), sorted(verify_build.DEFAULT_HOST_HOOKS))


class DexTopologyTests(GraftFixture):
    def test_dex_topology_is_inexact_when_the_build_adds_a_dex_stock_does_not_have(self) -> None:
        """A second new DEX beyond the custom one is not a graft, it is a stowaway.

        Caught twice since `added_entries` landed — a stowaway DEX is also an
        entry the build invented — and both are asserted rather than one hidden.
        """
        self.assertIs(self.verify()["passed"], True)

        self.built_entries["classes22.dex"] = dex("an extra DEX nobody asked for")
        results = self.verify()

        self.assertEqual(results["built_dex_count"], 6)
        self.assertEqual(results["added_entries"], ["classes22.dex"])
        self.assert_failing_verdicts(results, "dex_topology_exact", "added_entries")

    def test_the_custom_dex_is_not_new_when_stock_already_ships_that_name(self) -> None:
        """439 already ships `classes20`, so a custom DEX name can collide.

        Colliding means the custom code overwrote a stock DEX instead of being
        added beside it — the topology set is unchanged, so only this flag can
        catch it.
        """
        self.assertIs(self.verify()["passed"], True)

        self.stock_entries[self.custom_dex] = dex("stock classes21")
        self.replaced.add(self.custom_dex)
        results = self.verify()

        self.assertTrue(results["dex_topology_exact"])
        self.assert_failing_verdicts(results, "custom_dex_is_new")


class CustomDexSymbolTests(GraftFixture):
    def test_each_required_custom_symbol_missing_from_the_custom_dex_fails(self) -> None:
        """All four approved classes must be in the custom DEX, individually."""
        for symbol in verify_build.REQUIRED_CUSTOM_SYMBOLS:
            with self.subTest(symbol=symbol):
                self.set_custom_body(CUSTOM_BODY)
                self.assertIs(self.verify()["passed"], True)

                self.set_custom_body(CUSTOM_BODY.replace(symbol, ""))
                results = self.verify()

                self.assertFalse(results["custom_required_symbols"][symbol])
                self.assertEqual(
                    [s for s, found in results["custom_required_symbols"].items() if not found],
                    [symbol],
                )
                self.assert_failing_verdicts(results, "custom_required_symbols")

    def test_each_forbidden_custom_symbol_present_in_the_custom_dex_fails(self) -> None:
        """Custom code stays self-contained: each forbidden symbol is fatal alone.

        The control run inside the loop is the point. Without it a check that
        rejected *everything* — not just a build carrying the symbol — would
        satisfy the same assertions.
        """
        for symbol in verify_build.FORBIDDEN_CUSTOM_SYMBOLS:
            with self.subTest(symbol=symbol):
                self.set_custom_body(CUSTOM_BODY)
                self.assertIs(self.verify()["passed"], True)

                self.set_custom_body(f"{CUSTOM_BODY} {symbol}")
                results = self.verify()

                self.assertEqual(
                    [s for s, found in results["custom_forbidden_symbols"].items() if found],
                    [symbol],
                )
                self.assert_failing_verdicts(results, "custom_forbidden_symbols")


class HostHookTests(GraftFixture):
    def test_a_declared_host_hook_absent_from_its_dex_fails(self) -> None:
        """Both halves of the pair are searched; losing either one is a failure."""
        for descriptor, method in HOST_HOOKS["classes3.dex"]:
            for dropped in (descriptor, method):
                with self.subTest(dropped=dropped):
                    self.built_entries["classes3.dex"] = BUILT_ENTRIES["classes3.dex"]
                    self.assertIs(self.verify()["passed"], True)

                    self.built_entries["classes3.dex"] = BUILT_ENTRIES["classes3.dex"].replace(
                        dropped.encode("utf-8"), b""
                    )
                    results = self.verify()

                    self.assertFalse(results["host_hooks"]["classes3.dex"][f"{descriptor} {method}"])
                    self.assert_failing_verdicts(results, "host_hooks")

    def test_a_host_hook_present_only_in_another_grafted_dex_still_fails(self) -> None:
        """Presence is checked in the DEX that owns the hook, not anywhere in the APK.

        A whole-archive search cannot fail: the custom DEX defines these methods
        and therefore already contains every descriptor and every bare method
        name. Moving `setContext` from `classes3.dex` to `classes.dex` — the hook
        landing in the wrong host — must still be caught.
        """
        descriptor, method = ("Lcom/dfinstagram/startapp;", "setContext")
        moved = f" {descriptor} {method}".encode("utf-8")
        self.assertIn(moved.strip(), BUILT_ENTRIES["classes3.dex"])

        self.built_entries["classes3.dex"] = BUILT_ENTRIES["classes3.dex"].replace(moved, b"")
        self.built_entries["classes.dex"] = BUILT_ENTRIES["classes.dex"] + moved
        results = self.verify()

        self.assertIn(descriptor.encode("utf-8"), self.built_entries["classes.dex"])
        self.assertIn(method.encode("utf-8"), self.built_entries[self.custom_dex])
        self.assertFalse(results["host_hooks"]["classes3.dex"][f"{descriptor} {method}"])
        self.assertTrue(all(results["host_hooks"]["classes.dex"].values()))
        self.assert_failing_verdicts(results, "host_hooks")

    def test_an_empty_host_hook_map_is_refused(self) -> None:
        """`all(...)` over an empty map is vacuously true, so it never reaches the zips."""
        with self.assertRaises(ValueError) as caught:
            self.verify(host_hooks={})

        self.assertIn("host_hooks is empty", str(caught.exception))

    def test_a_host_hook_naming_an_ungrafted_dex_is_refused(self) -> None:
        """A hook cannot be in a DEX that was never replaced — that map is wrong.

        Including the custom DEX: it is added, not replaced, so naming it is the
        same mistake.
        """
        self.host_hooks["classes4.dex"] = [("Lcom/dfinstagram/hooks;", "throwIfBlocked")]
        with self.assertRaises(ValueError) as caught:
            self.verify()
        self.assertIn("classes4.dex", str(caught.exception))
        self.assertIn("never replaced", str(caught.exception))

        del self.host_hooks["classes4.dex"]
        self.host_hooks[self.custom_dex] = [("Lcom/dfinstagram/hooks;", "throwIfBlocked")]
        with self.assertRaises(ValueError) as caught:
            self.verify()
        self.assertIn(self.custom_dex, str(caught.exception))


class GraftedDexTests(GraftFixture):
    def test_a_grafted_dex_byte_identical_to_stock_fails(self) -> None:
        """The graft silently shipping the unpatched original is the whole point.

        `classes2.dex` is declared grafted while carrying stock bytes, which is
        the only arrangement where this flag is the sole failure: a hook-bearing
        DEX left unpatched also loses its hook, and would be caught twice.
        """
        self.assertIs(self.verify()["passed"], True)

        self.replaced.add("classes2.dex")
        results = self.verify()

        self.assertEqual(
            results["grafted_dex_changed"],
            {"classes.dex": True, "classes2.dex": False, "classes3.dex": True, "classes6.dex": True},
        )
        self.assert_failing_verdicts(results, "grafted_dex_changed")

    def test_an_unpatched_host_dex_loses_its_hook_as_well(self) -> None:
        """The realistic form of the same failure, caught by both flags."""
        self.built_entries["classes6.dex"] = STOCK_ENTRIES["classes6.dex"]
        results = self.verify()

        self.assertFalse(results["grafted_dex_changed"]["classes6.dex"])
        self.assertFalse(all(results["host_hooks"]["classes6.dex"].values()))
        self.assertFalse(results["passed"])


class PreservationTests(GraftFixture):
    def test_a_preserved_entry_with_changed_bytes_is_named_and_fails(self) -> None:
        self.assertIs(self.verify()["passed"], True)

        self.built_entries["res/drawable/ic_home.png"] = b"repainted"
        results = self.verify()

        self.assertEqual(results["preserved_entries_mismatched"], ["res/drawable/ic_home.png"])
        self.assertEqual(results["preserved_entries_missing"], [])
        self.assert_failing_verdicts(results, "preserved_entries_mismatched")

    def test_a_preserved_entry_missing_from_the_build_is_named_and_fails(self) -> None:
        self.assertIs(self.verify()["passed"], True)

        del self.built_entries["assets/config.json"]
        results = self.verify()

        self.assertEqual(results["preserved_entries_missing"], ["assets/config.json"])
        self.assertEqual(results["preserved_entries_mismatched"], [])
        self.assert_failing_verdicts(results, "preserved_entries_missing")

    def test_a_meta_inf_entry_that_is_not_a_signature_is_preservation_checked(self) -> None:
        """The signature skip must not swallow the rest of META-INF."""
        self.assertFalse(verify_build.is_signature_entry("META-INF/services/x.Provider"))

        self.built_entries["META-INF/services/x.Provider"] = b"rewritten"
        results = self.verify()

        self.assertEqual(results["preserved_entries_mismatched"], ["META-INF/services/x.Provider"])
        self.assert_failing_verdicts(results, "preserved_entries_mismatched")

    def test_a_surviving_signature_entry_leaves_signatures_stripped_false_and_fails(self) -> None:
        """A stock signature that survived the graft is caught by this flag alone.

        The preservation loop skips signature entries, so identical stock bytes
        raise no mismatch — nothing else would notice.
        """
        self.assertIs(self.verify()["passed"], True)

        self.built_entries["META-INF/CERT.RSA"] = STOCK_ENTRIES["META-INF/CERT.RSA"]
        results = self.verify()

        self.assertEqual(results["preserved_entries_mismatched"], [])
        self.assertEqual(results["preserved_entries_missing"], [])
        self.assert_failing_verdicts(results, "signatures_stripped")


class ReportEnvelopeTests(GraftFixture):
    """`main()`: the identity envelope, the exit code, and the release gate."""

    def run_main(self, *extra: str) -> tuple[int, str, Path, Path]:
        built, stock = self.apks()
        hooks_path = self.root / "host-hooks.json"
        hooks_path.write_text(
            json.dumps({name: [list(pair) for pair in pairs] for name, pairs in self.host_hooks.items()}),
            encoding="utf-8",
        )
        argv = [
            str(VERIFIER_PATH),
            str(built),
            str(stock),
            "--custom-dex",
            self.custom_dex,
            "--replaced-dex",
            ",".join(sorted(self.replaced)),
            "--host-hooks",
            str(hooks_path),
            *extra,
        ]
        printed = io.StringIO()
        with mock.patch.object(sys, "argv", argv), redirect_stdout(printed):
            with self.assertRaises(SystemExit) as caught:
                verify_build.main()
        return caught.exception.code, printed.getvalue(), built, stock

    def test_the_identity_envelope_is_present_and_the_release_gate_accepts_it(self) -> None:
        """Without these fields a verified build is one the release path refuses.

        `tools/release/finalize.py:load_json_report` is imported and called here
        rather than restated, so this pins the contract against its real consumer.
        """
        output = self.root / "verification.json"
        code, printed, built, stock = self.run_main("--output", str(output))
        report = json.loads(printed)

        self.assertEqual(code, 0)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["apk"], str(built))
        self.assertEqual(report["stock_apk"], str(stock))
        self.assertEqual(report["apk_sha256"], sha256_bytes_of_file(built))
        self.assertEqual(report["stock_apk_sha256"], sha256_bytes_of_file(stock))
        self.assertEqual(report["verifier_sha256"], sha256_bytes_of_file(VERIFIER_PATH))
        self.assertNotEqual(report["apk_sha256"], report["stock_apk_sha256"])
        self.assertIs(report["passed"], True)

        self.assertEqual(finalize.load_json_report(output, "unsigned verification report"), report)

    def test_the_release_gate_refuses_a_report_that_did_not_pass(self) -> None:
        """A failing verification must never be signable."""
        self.built_entries["classes6.dex"] = STOCK_ENTRIES["classes6.dex"]
        output = self.root / "verification.json"
        code, printed, _, _ = self.run_main("--output", str(output))

        self.assertEqual(code, 1)
        self.assertIs(json.loads(printed)["passed"], False)
        with self.assertRaises(ValueError) as caught:
            finalize.load_json_report(output, "unsigned verification report")
        self.assertIn("did not pass", str(caught.exception))

    def test_the_release_gate_cross_checks_the_apk_the_report_was_written_for(self) -> None:
        """The envelope exists so signing cannot be pointed at a different APK.

        `validate_prerequisites` re-hashes the files it was handed and compares
        them with what the report claims, so a report whose `apk_sha256` names
        something else is refused by name.
        """
        output = self.root / "verification.json"
        _, _, built, stock = self.run_main("--output", str(output))

        def build_report_for(verification: Path) -> Path:
            path = self.root / "build.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "passed": True,
                        "source_commit": "0" * 40,
                        "unsigned_apk_sha256": sha256_bytes_of_file(built),
                        "stock_apk_sha256": sha256_bytes_of_file(stock),
                        "verification_report_sha256": sha256_bytes_of_file(verification),
                    }
                ),
                encoding="utf-8",
            )
            return path

        prerequisites = finalize.validate_prerequisites(
            built, stock, build_report_for(output), output
        )
        self.assertTrue(all(prerequisites["checks"].values()))

        tampered = json.loads(output.read_text(encoding="utf-8"))
        tampered["apk_sha256"] = "f" * 64
        output.write_text(json.dumps(tampered), encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            finalize.validate_prerequisites(built, stock, build_report_for(output), output)
        self.assertIn("verification_unsigned_apk", str(caught.exception))

    def test_main_exits_zero_when_passed_and_one_when_not(self) -> None:
        """The exit code is the only thing `build.py` reads: `run()` checks it."""
        self.assertEqual(self.run_main()[0], 0)

        self.built_entries["classes6.dex"] = STOCK_ENTRIES["classes6.dex"]
        code, printed, _, _ = self.run_main()

        self.assertEqual(code, 1)
        self.assertIs(json.loads(printed)["passed"], False)

    def test_the_output_file_holds_exactly_what_was_printed(self) -> None:
        output = self.root / "verification.json"
        code, printed, _, _ = self.run_main("--output", str(output))

        self.assertEqual(code, 0)
        self.assertEqual(output.read_text(encoding="utf-8"), printed)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), json.loads(printed))


class ArchiveShapeTests(GraftFixture):
    """The four defects this file's first run found, now fixed and pinned.

    Each was a way for the archive itself — not its DEX contents — to be wrong:
    an entry nothing looked at, a name read through to the wrong bytes, a count
    of a set that was never counted, and three shapes that crashed the verifier
    before it could write a report.
    """

    def test_an_added_entry_the_stock_apk_never_had_is_rejected_and_named(self) -> None:
        """"every archive entry outside the grafted set is byte-identical to stock".

        The preservation loop walks *stock's* names, so an entry present only in
        the output is compared with nothing; the topology check covers
        `classesN.dex` alone, so a smuggled native library slipped past both.
        `added_entries` is the term that looks at the output's own names.
        """
        self.assertIs(self.verify()["passed"], True)

        self.built_entries["lib/arm64-v8a/libsmuggled.so"] = b"elf-bytes"
        results = self.verify()

        self.assertEqual(results["added_entries"], ["lib/arm64-v8a/libsmuggled.so"])
        self.assert_failing_verdicts(results, "added_entries")

    def test_preserved_entry_count_is_the_number_of_entries_actually_compared(self) -> None:
        """Not `len(ref_names) - len(grafted)`, which counts uncompared entries.

        That form counted the stock signature entries the loop skips and
        subtracted a custom DEX that was never a stock entry, over-reporting by
        (signature entries - 1) — +2 on any real APK, and 9 against 7 here.
        """
        results = self.verify()
        compared = [
            name
            for name in self.stock_entries
            if not verify_build.is_signature_entry(name) and name not in self.replaced
        ]

        self.assertEqual(len(compared), 7)
        self.assertEqual(results["preserved_entry_count"], 7)
        self.assertIs(results["passed"], True)

        # An entry the build dropped is reported missing, not counted compared.
        del self.built_entries["assets/config.json"]
        self.assertEqual(self.verify()["preserved_entry_count"], 6)

    def test_a_duplicate_entry_hiding_the_stock_original_is_rejected_and_named(self) -> None:
        """A ZIP may carry a name twice, and `ZipFile.read` returns the LAST one.

        So an archive shipping the unpatched original *and* the patch under the
        same name read through to the patched bytes on every other check, and
        verified clean — while an installer taking the first entry would run
        stock code. `tools/port_430/build.py` refuses duplicates when it grafts;
        the verifier now refuses them too.
        """
        self.assertIs(self.verify()["passed"], True)

        self.built_entries["classes3.dex"] = STOCK_ENTRIES["classes3.dex"]
        self.appended = (("classes3.dex", BUILT_ENTRIES["classes3.dex"]),)
        built, _ = self.apks()
        with zipfile.ZipFile(built) as archive:
            names = [info.filename for info in archive.infolist()]
            self.assertEqual(names.count("classes3.dex"), 2)
            # Every other check reads through this name and sees the patch.
            self.assertEqual(archive.read("classes3.dex"), BUILT_ENTRIES["classes3.dex"])

        results = self.verify()

        self.assertEqual(results["duplicate_entries"], ["classes3.dex"])
        self.assert_failing_verdicts(results, "duplicate_entries")

    def test_a_custom_dex_absent_from_the_build_is_named_not_raised(self) -> None:
        """The likeliest real failure: the custom tree never compiled to a DEX.

        `out.read(custom_dex)` raised a bare `KeyError` out of a verifier that
        had not written its report, so the run died with no JSON and no stated
        cause. It now reports — and every symbol assertion reads False against
        the empty blob rather than exploding.
        """
        del self.built_entries[self.custom_dex]
        results = self.verify()

        self.assertEqual(results["expected_entries_absent"], [self.custom_dex])
        self.assertEqual(
            results["custom_required_symbols"],
            {symbol: False for symbol in verify_build.REQUIRED_CUSTOM_SYMBOLS},
        )
        self.assert_failing_verdicts(
            results,
            "expected_entries_absent",
            "custom_dex_is_new",
            "dex_topology_exact",
            "custom_required_symbols",
        )

    def test_a_host_hook_dex_absent_from_the_build_is_named_not_raised(self) -> None:
        """Same crash, reached through the hook loop instead of the custom read."""
        del self.built_entries["classes6.dex"]
        results = self.verify()

        self.assertEqual(results["expected_entries_absent"], ["classes6.dex"])
        self.assertFalse(any(results["host_hooks"]["classes6.dex"].values()))
        self.assert_failing_verdicts(
            results,
            "expected_entries_absent",
            "host_hooks",
            "grafted_dex_changed",
            "dex_topology_exact",
        )

    def test_a_replaced_dex_stock_never_had_is_named_not_raised(self) -> None:
        """`--replaced-dex` naming a DEX stock does not have, in both shapes.

        Whether or not the build produced the entry, the comparison against
        stock has no left-hand side. `ref.read` used to raise; the name is now
        reported, and `replaced_entries_absent_from_stock` says which side is
        missing where `expected_entries_absent` cannot.
        """
        self.replaced.add("classes9.dex")

        with self.subTest(shape="the build produced it, stock never had it"):
            self.built_entries["classes9.dex"] = dex("stock classes9 patched")
            results = self.verify()
            self.assertEqual(results["replaced_entries_absent_from_stock"], ["classes9.dex"])
            self.assertEqual(results["expected_entries_absent"], [])
            self.assert_failing_verdicts(
                results,
                "replaced_entries_absent_from_stock",
                "added_entries",
                "grafted_dex_changed",
                "dex_topology_exact",
            )

        with self.subTest(shape="neither archive has it"):
            del self.built_entries["classes9.dex"]
            results = self.verify()
            self.assertEqual(results["replaced_entries_absent_from_stock"], ["classes9.dex"])
            self.assertEqual(results["expected_entries_absent"], ["classes9.dex"])
            self.assert_failing_verdicts(
                results,
                "replaced_entries_absent_from_stock",
                "expected_entries_absent",
                "grafted_dex_changed",
            )


if __name__ == "__main__":
    unittest.main()

"""Structural verification for a DFInsta graft, on any Instagram version.

Deliberately separate from ``tools/port_430/verify_apk.py``: that verifier pins
430's exact obfuscated descriptors and method signatures, all of which moved in
439. Loosening those pins into something that passes on either target would prove
less about both. This takes the opposite route -- every version-specific fact is
supplied by the caller, derived from that run's own resolution, so the assertions
stay exact without being hardcoded.

Supplied per target: which DEX holds the custom code, which host DEX files were
grafted, and which hook call must appear in each of them (``--host-hooks``, the
map the Resolve stage already knows and used to be hand-written here).

What it proves:
  * exact DEX topology, including that the custom code landed in its own new DEX
  * the custom DEX contains the approved descriptors and nothing forbidden
  * every host hook call is present in the DEX that owns it
  * every grafted host DEX actually differs from stock
  * every archive entry outside the grafted set is byte-identical to stock

What it does not prove: runtime behaviour. A structurally perfect graft can still
be inert -- that has happened three times in this project -- so a device contrast
remains mandatory before calling a port good.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


REQUIRED_CUSTOM_SYMBOLS = (
    "Lcom/dfinstagram/startapp;",
    "Lcom/dfinstagram/dfinstagram;",
    "Lcom/dfinstagram/hooks;",
    "Lcom/dfinstagram/SettingsWrapper;",
)

# Custom code must stay self-contained: no Instagram types, no Activity or
# resource dependencies, no inherited telemetry, no revived response rewriting.
FORBIDDEN_CUSTOM_SYMBOLS = (
    "Lcom/instagram/",
    "Landroid/app/Activity;",
    "Landroid/preference/PreferenceActivity;",
    "Amplitude",
    "Lcom/acra/",
    "DistractionFree",
    "FeedCache",
    "modifyFeedResponse",
    "nativeReadBuffer",
    "Lcom/dfinstagram/preference/Preference;",
)

# The default host-hook map, used when --host-hooks is not supplied. It is the
# 439 resolution, kept as a worked example of the shape.
#
# NOTE: a DEX does NOT store a method reference as the concatenated smali form
# "Lcom/x;->m(Ljava/lang/String;)V". It stores three separate indices (class
# type, name, prototype), and only the type descriptor and the bare method name
# exist as literal strings in the string table. Searching raw DEX bytes for the
# smali signature therefore always fails -- it did here, and looked like a
# missing hook until the type descriptor was checked directly.
DEFAULT_HOST_HOOKS = {
    "classes3.dex": (
        ("Lcom/dfinstagram/startapp;", "setContext"),
        ("Lcom/dfinstagram/hooks;", "replaceReelsEndpoint"),
    ),
    "classes.dex": (("Lcom/dfinstagram/hooks;", "throwIfBlocked"),),
    "classes6.dex": (("Lcom/dfinstagram/SettingsWrapper;", "onLongClick"),),
}

SIGNATURE_PREFIXES = ("META-INF/MANIFEST.MF",)


def is_signature_entry(name: str) -> bool:
    parts = name.upper().split("/")
    if len(parts) != 2 or parts[0] != "META-INF":
        return False
    return parts[1] == "MANIFEST.MF" or parts[1].endswith((".SF", ".RSA", ".DSA", ".EC"))


def verify(
    built: Path,
    stock: Path,
    custom_dex: str,
    replaced: set[str],
    host_hooks: dict | None = None,
    expect_signed: bool = False,
) -> dict:
    """Check a built APK against the stock archive it was grafted from.

    ``expect_signed`` flips two assertions that are only correct before signing.
    The graft strips every stock signature artifact, so an *unsigned* build must
    carry none — but the release gate runs this same verifier again **after**
    apksigner has written `META-INF/*.SF` and `*.RSA`, and there those entries are
    the point. Without this the post-signing check fails on the two files it was
    asked to confirm, which is how the release path came to be run by hand.
    """
    host_hooks = DEFAULT_HOST_HOOKS if host_hooks is None else host_hooks
    if not host_hooks:
        # An empty map would make `all(...)` over it vacuously true, so a build
        # with no host hook proven at all would pass.
        raise ValueError(
            "host_hooks is empty: with nothing to prove, every hook assertion "
            "passes vacuously and the verifier certifies an unpatched graft"
        )
    unknown = sorted(set(host_hooks) - replaced)
    if unknown:
        raise ValueError(
            f"host_hooks names {unknown}, which are not among the grafted DEX files "
            f"{sorted(replaced)}; a hook cannot be in a DEX that was never replaced"
        )
    results: dict[str, object] = {}
    with zipfile.ZipFile(built) as out, zipfile.ZipFile(stock) as ref:
        out_infos = out.infolist()
        out_names = {i.filename for i in out_infos}
        ref_names = {i.filename for i in ref.infolist()}

        # A ZIP may carry the same name twice, and `ZipFile.read` returns the
        # LAST one. An archive still carrying the unpatched original alongside
        # the patched entry would verify clean on every check below, because
        # every check reads through that name. `tools/port_430/build.py` refuses
        # duplicates when it grafts; a verifier that does not is weaker than the
        # builder whose output it is supposed to check.
        seen: set[str] = set()
        duplicates = sorted({i.filename for i in out_infos if i.filename in seen or seen.add(i.filename)})
        results["duplicate_entries"] = duplicates

        stock_dex = sorted(n for n in ref_names if n.startswith("classes") and n.endswith(".dex"))
        built_dex = sorted(n for n in out_names if n.startswith("classes") and n.endswith(".dex"))
        results["stock_dex_count"] = len(stock_dex)
        results["built_dex_count"] = len(built_dex)
        results["custom_dex_is_new"] = custom_dex in out_names and custom_dex not in ref_names
        results["dex_topology_exact"] = set(built_dex) == set(stock_dex) | {custom_dex}

        # Every read below is against a name the build was supposed to produce,
        # and the likeliest real failure — the custom tree never compiled into
        # its own DEX — makes that name absent. Reading it anyway raises a bare
        # KeyError out of a verifier that has not written its report yet, so the
        # run fails with no JSON and no stated cause. Report it instead.
        absent = sorted(
            name
            for name in {custom_dex} | set(replaced) | set(host_hooks)
            if name not in out_names
        )
        results["expected_entries_absent"] = absent
        missing_from_stock = sorted(name for name in replaced if name not in ref_names)
        results["replaced_entries_absent_from_stock"] = missing_from_stock

        custom = out.read(custom_dex) if custom_dex in out_names else b""
        results["custom_required_symbols"] = {
            s: (s.encode() in custom) for s in REQUIRED_CUSTOM_SYMBOLS
        }
        results["custom_forbidden_symbols"] = {
            s: (s.encode() in custom) for s in FORBIDDEN_CUSTOM_SYMBOLS
        }

        hooks: dict[str, dict[str, bool]] = {}
        for dex, pairs in host_hooks.items():
            blob = out.read(dex) if dex in out_names else b""
            hooks[dex] = {
                f"{descriptor} {name}": (descriptor.encode() in blob and name.encode() in blob)
                for descriptor, name in pairs
            }
        results["host_hooks"] = hooks

        # A grafted host DEX must actually differ from stock, otherwise the
        # graft silently shipped the unpatched original.
        results["grafted_dex_changed"] = {
            name: (
                name in out_names
                and name in ref_names
                and hashlib.sha256(out.read(name)).digest()
                != hashlib.sha256(ref.read(name)).digest()
            )
            for name in sorted(replaced)
        }

        grafted = replaced | {custom_dex}
        mismatched, missing, compared = [], [], 0
        for name in sorted(ref_names):
            if is_signature_entry(name) or name in grafted:
                continue
            if name not in out_names:
                missing.append(name)
                continue
            compared += 1
            if hashlib.sha256(ref.read(name)).digest() != hashlib.sha256(out.read(name)).digest():
                mismatched.append(name)
        # Entries the BUILD INVENTED. The loop above walks stock's names, so an
        # entry present only in the output is examined by nothing at all — and
        # this module's contract is that every archive entry outside the grafted
        # set is byte-identical to stock, which an added file silently breaks.
        signature_entries = sorted(n for n in out_names if is_signature_entry(n))
        results["signature_entries"] = signature_entries
        added = sorted(out_names - ref_names - {custom_dex})
        if expect_signed:
            # After signing, the signer's own files are the expected addition —
            # and the ONLY one. Everything else added is still rejected, so this
            # relaxes exactly the entries apksigner is known to write and nothing
            # more.
            added = [name for name in added if not is_signature_entry(name)]
        results["added_entries"] = added
        # Count what was actually compared. `len(ref_names) - len(grafted)` also
        # counts the stock signature entries the loop skips, and subtracts a
        # custom DEX that was never a stock entry, so it over-reported by
        # (signature entries - 1) on every real APK.
        results["preserved_entry_count"] = compared
        results["preserved_entries_missing"] = missing
        results["preserved_entries_mismatched"] = mismatched
        results["signatures_stripped"] = not any(is_signature_entry(n) for n in out_names)

    passed = (
        results["dex_topology_exact"]
        and results["custom_dex_is_new"]
        and all(results["custom_required_symbols"].values())
        and not any(results["custom_forbidden_symbols"].values())
        and all(all(v.values()) for v in results["host_hooks"].values())
        and all(results["grafted_dex_changed"].values())
        and not missing
        and not mismatched
        and not added
        and not duplicates
        and not absent
        and not missing_from_stock
        # Before signing: no signature artifact may survive the graft. After
        # signing: at least one must exist, or "signed" is an assertion about an
        # archive that carries no signature.
        and (bool(signature_entries) if expect_signed else results["signatures_stripped"])
    )
    results["passed"] = bool(passed)
    return results


CERTIFICATE_LINE = re.compile(r"Signer #\d+ certificate SHA-256 digest: ([0-9a-fA-F]{64})")


def signature_context(
    apk: Path, apksigner: Path, expected_certificate_sha256: str | None = None
) -> dict:
    """What apksigner says about this APK, and whether that is who we expected.

    Deliberately a second implementation of the check in
    `tools/port_430/verify_apk.py` rather than a shared import. The two verifiers
    are meant to be independent — one is 430-shaped and pins things this one does
    not — and they are invoked as bare scripts from `finalize.py`, so a shared
    module would either couple them or depend on PYTHONPATH being set by whoever
    runs the release. The *contract* is what must not drift, and both files pin it
    in their own tests: `passed` requires BOTH that apksigner verified the archive
    and that the certificate is the expected one. Verified-but-unexpected is the
    dangerous case — a correctly signed APK signed by the wrong key.
    """
    result = subprocess.run(
        [str(apksigner), "verify", "--verbose", "--print-certs", str(apk)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = (result.stdout + result.stderr).strip().splitlines()
    certificates = [
        match.group(1).lower()
        for line in output
        if (match := CERTIFICATE_LINE.fullmatch(line.strip()))
    ]
    expected = expected_certificate_sha256.lower() if expected_certificate_sha256 else None
    return {
        "tool": str(apksigner),
        "tool_sha256": sha256_file(apksigner),
        "verified": result.returncode == 0,
        "certificate_sha256": certificates,
        "expected_certificate_sha256": expected,
        "approved_signer": expected is None or certificates == [expected],
        "output": output,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("built_apk", type=Path)
    parser.add_argument("stock_apk", type=Path)
    parser.add_argument("--custom-dex", default="classes21.dex")
    parser.add_argument("--replaced-dex", default="classes.dex,classes3.dex,classes6.dex")
    parser.add_argument(
        "--host-hooks",
        type=Path,
        help='JSON {"classes3.dex": [["Lcom/dfinstagram/startapp;", "setContext"]]} '
        "naming the call each grafted DEX must contain; derived from this run's "
        "resolution. Defaults to the recorded 439 map.",
    )
    parser.add_argument("--output", type=Path)
    # The post-signing half. Before these existed this verifier could not be the
    # release gate's `--final-verifier` at all: `finalize.py` invokes it with
    # exactly these flags, only the 430-shaped verifier accepted them, and so a
    # target-neutral build had no target-neutral post-signing check. The 440
    # release was signed by hand for that reason.
    parser.add_argument("--apksigner", type=Path)
    parser.add_argument("--require-signature", action="store_true")
    parser.add_argument("--expected-certificate-sha256")
    parser.add_argument(
        "--apktool-jar",
        type=Path,
        help="not used to verify; its hash is recorded so the report names the "
        "toolchain the build came from",
    )
    args = parser.parse_args()

    if args.require_signature and args.apksigner is None:
        parser.error("--require-signature requires --apksigner")
    # `is not None`, not truthiness: an EMPTY string is a supplied-but-unusable
    # pin, and a falsy check skips the hex guard, hands `signature_context` an
    # `expected` of None, and reports `approved_signer: true` for any key at all.
    # An unset shell variable expanding to "" would silently turn the certificate
    # pin off while the command line still says it is on.
    if args.expected_certificate_sha256 is not None:
        if args.apksigner is None:
            parser.error("--expected-certificate-sha256 requires --apksigner")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", args.expected_certificate_sha256):
            parser.error("--expected-certificate-sha256 must be 64 hexadecimal characters")

    host_hooks = None
    if args.host_hooks:
        host_hooks = {
            dex: [tuple(pair) for pair in pairs]
            for dex, pairs in json.loads(
                args.host_hooks.read_text(encoding="utf-8")
            ).items()
        }

    # The identity envelope is what lets the release gate consume this report:
    # `tools/release/finalize.py` refuses a report whose `schema_version` is not 1
    # and cross-checks `apk_sha256`/`stock_apk_sha256` against the files it was
    # handed, so that signing cannot be pointed at a different APK than the one
    # that was verified. Without these three fields the driver could produce a
    # verified build that the release path would not accept — which is exactly
    # what happened the first time anyone tried it. Same names and same meanings
    # as `tools/port_430/verify_apk.py`, deliberately: two verifiers that pin
    # different things must still be interchangeable to the gate.
    report = {
        "schema_version": 1,
        "apk": str(args.built_apk),
        "apk_sha256": sha256_file(args.built_apk),
        "stock_apk": str(args.stock_apk),
        "stock_apk_sha256": sha256_file(args.stock_apk),
        "verifier_sha256": sha256_file(Path(__file__).resolve()),
        **verify(
            args.built_apk,
            args.stock_apk,
            args.custom_dex,
            {n for n in args.replaced_dex.split(",") if n},
            host_hooks,
            # Being given an apksigner IS the statement that this is the
            # post-signing check; there is no other reason to pass one. Inferred
            # rather than a separate flag so `finalize.py`, which passes
            # --apksigner and nothing like --expect-signed, gets the right
            # behaviour without the release gate having to know about this file's
            # internals.
            expect_signed=args.apksigner is not None,
        ),
    }
    if args.apktool_jar is not None:
        report["apktool_jar"] = str(args.apktool_jar)
        report["apktool_jar_sha256"] = sha256_file(args.apktool_jar)

    signature = (
        signature_context(args.built_apk, args.apksigner, args.expected_certificate_sha256)
        if args.apksigner
        else None
    )
    report["signature"] = signature
    if signature is not None:
        report["passed"] = bool(
            report["passed"] and signature["verified"] and signature["approved_signer"]
        )
    elif args.require_signature:
        # Unreachable through argparse, which refuses --require-signature without
        # --apksigner first. Kept as a backstop rather than deleted: it is the
        # invariant itself — an unchecked signature is not a satisfied
        # requirement — and it survives a refactor that loosens the flag parsing.
        report["passed"] = False

    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()

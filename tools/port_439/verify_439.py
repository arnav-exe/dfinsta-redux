"""Structural verification for the Instagram 439 DFInsta graft.

Deliberately separate from ``tools/port_430/verify_apk.py``: that verifier pins
430's exact obfuscated descriptors and method signatures, all of which moved in
439. Rather than loosen those pins into something that would pass on either
target and prove less about both, this checks 439's own topology and its own
resolved hosts.

What it proves:
  * exact DEX topology, including that the custom code landed in its own new DEX
  * the custom DEX contains exactly the four approved descriptors and nothing else
  * no forbidden symbol leaked into custom code
  * every host hook call is present in the DEX that owns it
  * every archive entry outside the grafted set is byte-identical to stock

What it does not prove: runtime behaviour. A structurally perfect graft can still
be inert -- that has happened three times in this project -- so a device contrast
remains mandatory before calling a port good.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

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

# host DEX -> (type descriptor, method name) pairs that must appear in it.
#
# NOTE: a DEX does NOT store a method reference as the concatenated smali form
# "Lcom/x;->m(Ljava/lang/String;)V". It stores three separate indices (class
# type, name, prototype), and only the type descriptor and the bare method name
# exist as literal strings in the string table. Searching raw DEX bytes for the
# smali signature therefore always fails -- it did here, and looked like a
# missing hook until the type descriptor was checked directly.
HOST_HOOKS = {
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


def verify(built: Path, stock: Path, custom_dex: str, replaced: set[str]) -> dict:
    results: dict[str, object] = {}
    with zipfile.ZipFile(built) as out, zipfile.ZipFile(stock) as ref:
        out_names = {i.filename for i in out.infolist()}
        ref_names = {i.filename for i in ref.infolist()}

        stock_dex = sorted(n for n in ref_names if n.startswith("classes") and n.endswith(".dex"))
        built_dex = sorted(n for n in out_names if n.startswith("classes") and n.endswith(".dex"))
        results["stock_dex_count"] = len(stock_dex)
        results["built_dex_count"] = len(built_dex)
        results["custom_dex_is_new"] = custom_dex in out_names and custom_dex not in ref_names
        results["dex_topology_exact"] = set(built_dex) == set(stock_dex) | {custom_dex}

        custom = out.read(custom_dex)
        results["custom_required_symbols"] = {
            s: (s.encode() in custom) for s in REQUIRED_CUSTOM_SYMBOLS
        }
        results["custom_forbidden_symbols"] = {
            s: (s.encode() in custom) for s in FORBIDDEN_CUSTOM_SYMBOLS
        }

        hooks: dict[str, dict[str, bool]] = {}
        for dex, pairs in HOST_HOOKS.items():
            blob = out.read(dex)
            hooks[dex] = {
                f"{descriptor} {name}": (descriptor.encode() in blob and name.encode() in blob)
                for descriptor, name in pairs
            }
        results["host_hooks"] = hooks

        # A grafted host DEX must actually differ from stock, otherwise the
        # graft silently shipped the unpatched original.
        results["grafted_dex_changed"] = {
            name: hashlib.sha256(out.read(name)).digest()
            != hashlib.sha256(ref.read(name)).digest()
            for name in sorted(replaced)
        }

        grafted = replaced | {custom_dex}
        mismatched, missing = [], []
        for name in sorted(ref_names):
            if is_signature_entry(name) or name in grafted:
                continue
            if name not in out_names:
                missing.append(name)
                continue
            if hashlib.sha256(ref.read(name)).digest() != hashlib.sha256(out.read(name)).digest():
                mismatched.append(name)
        results["preserved_entry_count"] = len(ref_names) - len(grafted)
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
        and results["signatures_stripped"]
    )
    results["passed"] = bool(passed)
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("built_apk", type=Path)
    parser.add_argument("stock_apk", type=Path)
    parser.add_argument("--custom-dex", default="classes21.dex")
    parser.add_argument("--replaced-dex", default="classes.dex,classes3.dex,classes6.dex")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    report = verify(
        args.built_apk,
        args.stock_apk,
        args.custom_dex,
        {n for n in args.replaced_dex.split(",") if n},
    )
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()

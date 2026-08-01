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
) -> dict:
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
        for dex, pairs in host_hooks.items():
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
    parser.add_argument(
        "--host-hooks",
        type=Path,
        help='JSON {"classes3.dex": [["Lcom/dfinstagram/startapp;", "setContext"]]} '
        "naming the call each grafted DEX must contain; derived from this run's "
        "resolution. Defaults to the recorded 439 map.",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    host_hooks = None
    if args.host_hooks:
        host_hooks = {
            dex: [tuple(pair) for pair in pairs]
            for dex, pairs in json.loads(
                args.host_hooks.read_text(encoding="utf-8")
            ).items()
        }

    report = verify(
        args.built_apk,
        args.stock_apk,
        args.custom_dex,
        {n for n in args.replaced_dex.split(",") if n},
        host_hooks,
    )
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()

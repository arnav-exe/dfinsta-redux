import argparse
import json
import re
import zipfile
from pathlib import Path
from typing import Callable


REQUIRED_CUSTOM_SYMBOLS = [
    "Lcom/dfinstagram/startapp;",
    "Lcom/dfinstagram/dfinstagram;",
    "Lcom/dfinstagram/hooks;",
    "Lcom/dfinstagram/SettingsWrapper;",
]

HOST_HOOK_MARKERS = {
    "classes.dex": "Lcom/dfinstagram/hooks;->throwIfBlocked(Ljava/net/URI;)V",
    "classes3.dex": "Lcom/dfinstagram/startapp;->setContext(Landroid/app/Application;)V",
    "classes6.dex": "Lcom/dfinstagram/SettingsWrapper;",
}

FORBIDDEN_CUSTOM_SYMBOLS = [
    "Lcom/instagram/",
    "DistractionFree",
    "Amplitude",
    "Lcom/acra/",
    "UniFile",
    "FeedCache",
    "Hardcore",
    "donate_",
    "welcome",
    "istring",
    "improveRemove",
    "modifyFeedResponse",
    "nativeReadBuffer",
    "Lcom/dfinstagram/preference/Preference;",
    "Lcom/dfinstagram/preference/PreferenceFragment;",
    "Landroid/app/Activity;",
    "Landroid/preference/PreferenceActivity;",
]


def expected_dex_names() -> list[str]:
    return ["classes.dex"] + [f"classes{index}.dex" for index in range(2, 21)]


def has_dex_marker(content: bytes, marker: str) -> bool:
    if "->" not in marker:
        return marker.encode("utf-8") in content
    descriptor, member = marker.split("->", 1)
    method_name = member.split("(", 1)[0]
    return all(token.encode("utf-8") in content for token in (descriptor, method_name))


def verify(
    dex_names: list[str],
    dex_content: dict[str, bytes],
    final_entries: dict[str, bytes],
    stock_entries: dict[str, bytes],
) -> dict:
    custom_dex = dex_content.get("classes20.dex", b"")
    custom_symbols = {
        match.decode("utf-8")
        for match in re.findall(rb"Lcom/dfinstagram/[A-Za-z0-9_$/]+;", custom_dex)
    }
    required = {symbol: symbol in custom_symbols for symbol in REQUIRED_CUSTOM_SYMBOLS}
    forbidden = {
        symbol: symbol.encode("utf-8") in custom_dex for symbol in FORBIDDEN_CUSTOM_SYMBOLS
    }
    host_hooks = {
        marker: has_dex_marker(dex_content.get(dex_name, b""), marker)
        for dex_name, marker in HOST_HOOK_MARKERS.items()
    }
    exact_dex_set = len(dex_names) == 20 and set(dex_names) == set(expected_dex_names())
    final_res = {name for name in final_entries if name.startswith("res/")}
    stock_res = {name for name in stock_entries if name.startswith("res/")}
    resource_names_equal = final_res == stock_res
    resource_bytes_equal = resource_names_equal and all(
        final_entries[name] == stock_entries[name] for name in stock_res
    )
    manifest_equal = (
        "AndroidManifest.xml" in final_entries
        and "AndroidManifest.xml" in stock_entries
        and final_entries["AndroidManifest.xml"] == stock_entries["AndroidManifest.xml"]
    )
    resources_arsc_equal = (
        "resources.arsc" in final_entries
        and "resources.arsc" in stock_entries
        and final_entries["resources.arsc"] == stock_entries["resources.arsc"]
    )
    exact_custom_symbols = custom_symbols == set(REQUIRED_CUSTOM_SYMBOLS)
    return {
        "dex_files": sorted(dex_names),
        "dex_count": len(dex_names),
        "exact_dex_set": exact_dex_set,
        "exact_custom_symbols": exact_custom_symbols,
        "required_custom_symbols": required,
        "host_hook_markers": host_hooks,
        "forbidden_custom_symbols_present": forbidden,
        "android_manifest_equal": manifest_equal,
        "resources_arsc_equal": resources_arsc_equal,
        "res_entry_names_equal": resource_names_equal,
        "res_entry_bytes_equal": resource_bytes_equal,
        "passed": all(
            [
                exact_dex_set,
                exact_custom_symbols,
                all(required.values()),
                all(host_hooks.values()),
                not any(forbidden.values()),
                manifest_equal,
                resources_arsc_equal,
                resource_bytes_equal,
            ]
        ),
    }


def read_apk(
    path: Path, include: Callable[[str], bool]
) -> tuple[list[str], dict[str, bytes]]:
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise ValueError(f"Corrupt APK: {path}")
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError(f"Duplicate ZIP entry in {path}")
        return names, {name: archive.read(name) for name in names if include(name)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("apk", type=Path)
    parser.add_argument("stock_apk", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    relevant = lambda name: (
        name in {"AndroidManifest.xml", "resources.arsc"}
        or name.startswith("res/")
        or (name.startswith("classes") and name.endswith(".dex") and "/" not in name)
    )
    resources = lambda name: name in {"AndroidManifest.xml", "resources.arsc"} or name.startswith(
        "res/"
    )
    final_names, final_entries = read_apk(args.apk, relevant)
    _, stock_entries = read_apk(args.stock_apk, resources)
    dex_names = [
        name
        for name in final_names
        if name.startswith("classes") and name.endswith(".dex") and "/" not in name
    ]
    dex_content = {name: final_entries[name] for name in dex_names}
    result = {
        "apk": str(args.apk),
        "stock_apk": str(args.stock_apk),
        **verify(dex_names, dex_content, final_entries, stock_entries),
    }
    rendered = json.dumps(result, indent=2) + "\n"
    if args.output:
        if args.output.exists():
            raise FileExistsError(f"Refusing to overwrite {args.output}")
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

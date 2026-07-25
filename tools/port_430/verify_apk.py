import argparse
import json
import zipfile
from pathlib import Path


REQUIRED_CUSTOM_SYMBOLS = [
    "Lcom/dfinstagram/startapp;",
    "Lcom/dfinstagram/dfinstagram;",
    "Lcom/dfinstagram/hooks;",
    "Lcom/dfinstagram/SettingsWrapper;",
    "Lcom/dfinstagram/preference/Preference;",
    "Lcom/dfinstagram/preference/PreferenceFragment;",
    "setContext",
    "throwIfBlocked",
    "startDfInstagramSettings",
]

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
]


def expected_dex_names() -> list[str]:
    return ["classes.dex"] + [f"classes{index}.dex" for index in range(2, 21)]


def verify(dex_names: list[str], custom_dex: bytes) -> dict:
    required = {
        symbol: symbol.encode("utf-8") in custom_dex for symbol in REQUIRED_CUSTOM_SYMBOLS
    }
    forbidden = {
        symbol: symbol.encode("utf-8") in custom_dex for symbol in FORBIDDEN_CUSTOM_SYMBOLS
    }
    exact_dex_set = set(dex_names) == set(expected_dex_names())
    return {
        "dex_files": sorted(dex_names),
        "dex_count": len(dex_names),
        "exact_dex_set": exact_dex_set,
        "required_custom_symbols": required,
        "forbidden_custom_symbols_present": forbidden,
        "passed": exact_dex_set and all(required.values()) and not any(forbidden.values()),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("apk", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    with zipfile.ZipFile(args.apk) as archive:
        archive.testzip()
        dex_names = [
            name
            for name in archive.namelist()
            if name.startswith("classes") and name.endswith(".dex") and "/" not in name
        ]
        if "classes20.dex" not in dex_names:
            custom_dex = b""
        else:
            custom_dex = archive.read("classes20.dex")

    result = {"apk": str(args.apk), **verify(dex_names, custom_dex)}
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

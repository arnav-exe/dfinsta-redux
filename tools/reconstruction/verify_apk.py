import argparse
import json
import zipfile
from pathlib import Path


REQUIRED_SYMBOLS = [
    "Lcom/dfinstagram/DistractionFree;",
    "Lcom/dfinstagram/SettingsWrapper;",
    "Lcom/dfinstagram/hooks;",
    "Lcom/dfinstagram/startapp;",
    "improveRemovePosts",
    "improveRemoveReels",
    "improveRemoveStories",
    "improveRemoveShopping",
    "improveRemoveAdsProfile",
    "throwIfBlocked",
    "setFeedCache",
    "clearFeedCache",
]

FORBIDDEN_1_3_SYMBOLS = [
    "modifyTigonBuffer",
    "modifyFeedResponse",
    "nativeReadBufferRead",
    "nativeReadBufferSize",
    "jniHandlerSendHeaders",
    "jniHandlerSendRequest",
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("apk", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    with zipfile.ZipFile(args.apk) as archive:
        names = set(archive.namelist())
        dex_names = sorted(name for name in names if name.startswith("classes") and name.endswith(".dex"))
        dex_content = b"".join(archive.read(name) for name in dex_names)
        archive.testzip()

    required = {symbol: symbol.encode("utf-8") in dex_content for symbol in REQUIRED_SYMBOLS}
    forbidden = {
        symbol: symbol.encode("utf-8") in dex_content for symbol in FORBIDDEN_1_3_SYMBOLS
    }
    result = {
        "apk": str(args.apk),
        "dex_files": dex_names,
        "dex_count": len(dex_names),
        "required_symbols": required,
        "forbidden_1_3_symbols_present": forbidden,
        "passed": len(dex_names) == 11 and all(required.values()) and not any(forbidden.values()),
    }
    rendered = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

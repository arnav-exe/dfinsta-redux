import argparse
import json
import re
import shutil
from pathlib import Path

#: Elements inside `<queries>` that aapt1 refuses to compile, with the reason.
#: `<queries>` postdates aapt1, which validates its children against the rules
#: for the identically named elements under `<application>`. A
#: `<queries><provider android:authorities="..."/>` is legal Android and has no
#: `android:name` — aapt1 sees a provider with no name and stops the build.
#: Instagram 440 added exactly one and 439 had none, so this is the first time
#: it mattered.
#:
#: Both the self-closing and the open/close forms are matched. apktool emits
#: empty elements self-closed and the real 440 manifest uses that form, so only
#: the first is live today — but matching the opening tag alone would delete it
#: and leave a bare `</provider>` behind, turning a tooling limitation into
#: malformed XML and a different, more confusing build failure.
AAPT1_REJECTS = (
    (
        # The open/close body is a TEMPERED dot: it may not contain another
        # `<provider` or `</provider`. A plain `.*?` reaches past a self-closing
        # sibling to the NEXT element's closing tag and deletes both, which is
        # how one over-broad match quietly removes a declaration that was fine.
        re.compile(
            r"<provider\b(?![^>]*\bandroid:name=)[^>]*"
            r"(?:/>|>(?:(?!</?provider\b).)*</provider\s*>)",
            re.DOTALL,
        ),
        "aapt1 requires android:name on <provider>; inside <queries> it is optional",
    ),
)

QUERIES_BLOCK = re.compile(r"<queries>.*?</queries>", re.DOTALL)


def copy_overlay(source: Path, destination: Path) -> None:
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        target = destination / path.relative_to(source)
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite payload target {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def sanitise_manifest_for_aapt1(text: str) -> tuple[str, list[dict[str, str]]]:
    """Drop `<queries>` children aapt1 cannot compile, and nothing else.

    **Why this is safe, and why it would not be anywhere else.** The manifest in
    the work tree is compiled to produce the *intermediate* APK, and
    `build.graft_apk` takes only DEX entries from that APK — `AndroidManifest.xml`,
    `resources.arsc` and every `res/` entry are copied byte-for-byte from the
    stock archive. So nothing edited here can reach the shipped app. Editing the
    stock decode instead, or building without the graft, would make the same edit
    a real change to what the app declares.

    The removal is confined to `<queries>` for the same reason it is needed
    there: a `<provider>` with no `android:name` under `<application>` is a
    genuinely malformed declaration, and quietly dropping it would hide a broken
    manifest instead of a tooling limitation. One is left in place so aapt1 still
    fails loudly.
    """
    removed: list[dict[str, str]] = []

    def scrub(match: re.Match[str]) -> str:
        block = match.group(0)
        for pattern, reason in AAPT1_REJECTS:
            for element in pattern.findall(block):
                removed.append({"element": element.strip(), "reason": reason})
            block = pattern.sub("", block)
        return block

    return QUERIES_BLOCK.sub(scrub, text), removed


def prepare(
    stock_decode: Path,
    patch_source: Path,
    output: Path,
    custom_tree: str = "smali_classes20",
) -> list[dict[str, str]]:
    """Overlay the custom classes into a fresh copy of a stock decode.

    `custom_tree` is a parameter because the free index is target-specific:
    Instagram 430 shipped 19 DEX files so smali_classes20 was free, while 439
    ships 20 and needs smali_classes21. Defaulting to the 430 value keeps every
    existing caller and test unchanged.

    Returns whatever had to be removed from the work tree's manifest to let
    aapt1 compile it — empty for every version before 440.
    """
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite work tree {output}")
    if not (stock_decode / "AndroidManifest.xml").is_file():
        raise FileNotFoundError(f"Not an apktool decode: {stock_decode}")
    if (stock_decode / custom_tree).exists():
        raise ValueError(f"Stock tree already has {custom_tree}")

    shutil.copytree(stock_decode, output)
    copy_overlay(patch_source / "newCode", output / custom_tree)

    manifest = output / "AndroidManifest.xml"
    text = manifest.read_text(encoding="utf-8")
    cleaned, removed = sanitise_manifest_for_aapt1(text)
    if removed:
        manifest.write_text(cleaned, encoding="utf-8")
    return removed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stock_decode", type=Path)
    parser.add_argument("patch_source", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--custom-tree", default="smali_classes20")
    args = parser.parse_args()

    removed = prepare(args.stock_decode, args.patch_source, args.output, args.custom_tree)
    print(f"Prepared build tree at {args.output}")
    for item in removed:
        # Printed, never silent: this is an edit to a manifest, and the reader
        # needs to see it even though the graft keeps it out of the built APK.
        print(f"  removed from <queries> for aapt1: {json.dumps(item)}")


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


ANDROID_NS = "http://schemas.android.com/apk/res/android"
CONST_STRING = re.compile(
    r'^const-string(?:/jumbo)?\s+(?P<register>[vp]\d+),\s+"(?P<literal>.*)"$'
)
CLASS = re.compile(r"^\.class\s+.*\s+(L[^\s]+;)$")
METHOD = re.compile(r"^\.method\s+.*?([^\s]+\([^\s]*\)[^\s]+)$")

INTENT_IDS = (
    "app-context",
    "block-explore",
    "block-feed",
    "block-profile-ads",
    "block-reels",
    "block-shopping",
    "block-stories",
    "cache-lifecycle",
    "privacy-hardening",
    "settings-entry",
    "welcome-flow",
)

FEATURE_INTENTS = {
    "explore": "block-explore",
    "feed": "block-feed",
    "profile_ads": "block-profile-ads",
    "reels": "block-reels",
    "shopping": "block-shopping",
    "stories": "block-stories",
}

ANCHORED_340_INTENTS = {
    "capture_feed_cache": ("cache-lifecycle",),
    "clear_feed_cache_reference": ("cache-lifecycle",),
    "install_dfinsta_settings_long_click": ("settings-entry",),
    "remove_stock_settings_long_click": ("settings-entry",),
    "set_app_context": ("app-context",),
    "show_welcome_dialog": ("welcome-flow",),
    "tigon_url_block": (
        "block-explore",
        "block-feed",
        "block-profile-ads",
        "block-reels",
        "block-shopping",
        "block-stories",
    ),
}

ANCHORED_430_METHODS = {
    "install_settings_long_click": (
        "A00(Landroid/content/Context;Lcom/instagram/common/session/UserSession;"
        "LX/077F;LX/0JxZ;)Landroid/widget/ImageView;"
    ),
    "replace_reels_discover_endpoint": (
        "A07(Landroid/content/Context;LX/0HSu;LX/0Jin;LX/0Jej;"
        "Lcom/instagram/common/session/UserSession;LX/0Jae;Ljava/lang/Integer;"
        "Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;"
        "Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;"
        "Ljava/lang/String;Ljava/lang/String;Lkotlin/jvm/functions/Function0;ZZZZ)LX/017H;"
    ),
    "replace_reels_homecoming_endpoint": (
        "A09(Landroid/content/Context;LX/0HSu;LX/0Jin;LX/0Jej;"
        "Lcom/instagram/common/session/UserSession;LX/0Jae;Ljava/lang/Integer;"
        "Ljava/lang/Long;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;"
        "Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;"
        "Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;"
        "Ljava/lang/String;Ljava/util/List;Lkotlin/jvm/functions/Function0;"
        "ZZZZZZZZZZZ)LX/03xp;"
    ),
    "replace_reels_stream_endpoint": (
        "A09(Landroid/content/Context;LX/0HSu;LX/0Jin;LX/0Jej;"
        "Lcom/instagram/common/session/UserSession;LX/0Jae;Ljava/lang/Integer;"
        "Ljava/lang/Long;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;"
        "Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;"
        "Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;"
        "Ljava/lang/String;Ljava/util/List;Lkotlin/jvm/functions/Function0;"
        "ZZZZZZZZZZZ)LX/03xp;"
    ),
    "set_app_context": "onCreate()V",
    "tigon_url_block": "startRequest(LX/05ez;LX/05fq;LX/05gu;)LX/0Fcm;",
}

ANCHORED_430_INTENTS = {
    "install_settings_long_click": ("settings-entry",),
    "replace_reels_discover_endpoint": ("block-reels",),
    "replace_reels_homecoming_endpoint": ("block-reels",),
    "replace_reels_stream_endpoint": ("block-reels",),
    "set_app_context": ("app-context",),
    "tigon_url_block": (
        "block-explore",
        "block-feed",
        "block-profile-ads",
        "block-reels",
        "block-stories",
    ),
}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    rendered = json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def source_records(root: Path, paths: list[Path]) -> list[dict[str, str]]:
    records = [
        {"relative_path": path.relative_to(root).as_posix(), "sha256": sha256_file(path)}
        for path in paths
    ]
    records.sort(key=lambda item: item["relative_path"])
    if len(records) != len({item["relative_path"] for item in records}):
        raise ValueError("Duplicate consumed source path")
    return records


def regular_files(root: Path) -> list[Path]:
    if not root.is_dir():
        raise FileNotFoundError(root)
    return sorted(path for path in root.rglob("*") if path.is_file())


def significant(lines: list[str]) -> list[tuple[int, str]]:
    return [
        (index, stripped)
        for index, line in enumerate(lines)
        if (stripped := line.strip())
        and not stripped.startswith(".line")
        and not stripped.startswith("#")
    ]


def method_ranges(lines: list[str]) -> list[tuple[int, int, str]]:
    ranges: list[tuple[int, int, str]] = []
    start: int | None = None
    signature: str | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(".method "):
            if start is not None:
                raise ValueError("Nested smali methods")
            match = METHOD.fullmatch(stripped)
            if not match:
                raise ValueError(f"Cannot parse method declaration: {stripped}")
            start, signature = index, match.group(1)
        elif stripped == ".end method":
            if start is None or signature is None:
                raise ValueError("Unmatched .end method")
            ranges.append((start, index, signature))
            start = None
            signature = None
    if start is not None:
        raise ValueError("Unterminated smali method")
    return ranges


def descriptor_from_file(path: Path) -> str:
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            match = CLASS.fullmatch(line.strip())
            if match:
                return match.group(1)
    raise ValueError(f"Class descriptor not found: {path}")


def resolve_classes(decode: Path, descriptors: set[str]) -> dict[str, Path]:
    resolved: dict[str, Path] = {}
    for descriptor in sorted(descriptors):
        relative = descriptor[1:-1]
        parent, basename = relative.rsplit("/", 1)
        candidates = sorted(decode.glob(f"smali*/{parent}/{basename}*.smali"))
        matches = [path for path in candidates if descriptor_from_file(path) == descriptor]
        if len(matches) != 1:
            raise ValueError(f"Descriptor {descriptor} resolved to {len(matches)} files")
        resolved[descriptor] = matches[0]
    return resolved


def sequence_matches(lines: list[str], sequence: list[str]) -> list[tuple[int, int]]:
    normalized = significant(lines)
    wanted = [line.strip() for line in sequence if line.strip() and not line.strip().startswith("#")]
    matches = []
    for index in range(len(normalized) - len(wanted) + 1):
        if [item[1] for item in normalized[index : index + len(wanted)]] == wanted:
            matches.append((normalized[index][0], normalized[index + len(wanted) - 1][0]))
    return matches


def owning_method(ranges: list[tuple[int, int, str]], start: int, end: int) -> tuple[int, int, str]:
    owners = [item for item in ranges if item[0] <= start and end <= item[1]]
    if len(owners) != 1:
        raise ValueError(f"Sequence has {len(owners)} owning methods")
    return owners[0]


def smali_operation(
    operation_id: str,
    intent_ids: tuple[str, ...],
    descriptor: str,
    method_signature: str,
    mode: str,
    precondition: list[str],
    precondition_count: int,
    payload: list[str],
    final: list[str],
    final_count: int,
    match_policy: str = "all",
    occurrence: int | None = None,
) -> dict[str, Any]:
    return {
        "descriptor": descriptor,
        "expected_final_count": final_count,
        "expected_precondition_count": precondition_count,
        "final_sequence": final,
        "intent_ids": sorted(intent_ids),
        "kind": "smali_edit",
        "match_policy": match_policy,
        "method_signature": method_signature,
        "mode": mode,
        "occurrence": occurrence,
        "operation_id": operation_id,
        "payload": payload,
        "precondition_sequence": precondition,
    }


def endpoint_operations(
    manifest: dict[str, Any], classes: dict[str, Path]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for record_index, record in enumerate(manifest["operations"]):
        descriptor = record["descriptor"]
        lines = classes[descriptor].read_text(encoding="utf-8").splitlines()
        ranges = method_ranges(lines)
        matches: list[tuple[int, str, str, str]] = []
        for line_index, line in significant(lines):
            match = CONST_STRING.fullmatch(line)
            if match and match.group("literal") == record["literal"]:
                method = owning_method(ranges, line_index, line_index)[2]
                matches.append((line_index, method, match.group("register"), line))
        if len(matches) != record["expected_count"]:
            raise ValueError(
                f"Endpoint cardinality drift for {descriptor} {record['literal']}: "
                f"{len(matches)} != {record['expected_count']}"
            )

        groups: dict[tuple[str, str, str], list[int]] = {}
        for line_index, method, register, instruction in matches:
            groups.setdefault((method, register, instruction), []).append(line_index)
        for group_index, ((method, register, instruction), selected) in enumerate(sorted(groups.items())):
            method_range = next(item for item in ranges if item[2] == method)
            method_instruction_matches = [
                index
                for index, line in significant(lines[method_range[0] : method_range[1] + 1])
                if line == instruction
            ]
            all_selected = len(selected) == len(method_instruction_matches)
            policy = "all" if all_selected else "occurrence"
            occurrence = None
            if not all_selected:
                occurrence = method_instruction_matches.index(selected[0] - method_range[0])
                if len(selected) != 1:
                    raise ValueError("An occurrence edit cannot select multiple instructions")

            helper = record["helper"]
            if record["mode"] == "replace":
                payload = [
                    f"invoke-static {{}}, Lcom/dfinstagram/DistractionFree;->{helper}()Ljava/lang/String;",
                    f"move-result-object {register}",
                ]
            elif record["mode"] == "wrap":
                payload = [
                    instruction,
                    f"invoke-static {{{register}}}, Lcom/dfinstagram/DistractionFree;"
                    f"->{helper}(Ljava/lang/String;)Ljava/lang/String;",
                    f"move-result-object {register}",
                ]
            else:
                raise ValueError(f"Unknown endpoint mode: {record['mode']}")
            slug = re.sub(r"[^a-z0-9]+", "-", record["literal"].lower()).strip("-")
            result.append(
                smali_operation(
                    f"340.endpoint.{record_index:02d}.{slug}.{group_index}",
                    (FEATURE_INTENTS[record["intent"]],),
                    descriptor,
                    method,
                    "replace",
                    [instruction],
                    len(selected),
                    payload,
                    payload,
                    len(selected),
                    policy,
                    occurrence,
                )
            )
    if len(result) != 38:
        raise ValueError(f"Expected 38 endpoint edits, generated {len(result)}")
    return result


def anchored_operations_340(
    manifest: dict[str, Any], classes: dict[str, Path]
) -> list[dict[str, Any]]:
    result = []
    for record in manifest["operations"]:
        descriptor = record["descriptor"]
        lines = classes[descriptor].read_text(encoding="utf-8").splitlines()
        ranges = method_ranges(lines)
        matches = sequence_matches(lines, record["anchor"])
        if len(matches) != record["expected_anchor_count"]:
            raise ValueError(f"Anchor cardinality drift for {record['id']}")
        owners = {owning_method(ranges, start, end)[2] for start, end in matches}
        if len(owners) != 1:
            raise ValueError(f"Anchor spans multiple methods for {record['id']}")
        method = owners.pop()
        selected = record.get("occurrence")
        policy = "all" if selected is None else "occurrence"
        payload = [line for _, line in significant(record["payload"])]
        anchor = [line for _, line in significant(record["anchor"])]
        if record["mode"] == "replace" and not payload:
            final, final_count = anchor, 0
        elif record["mode"] == "insert_after":
            final, final_count = [*anchor, *payload], 1
        elif record["mode"] == "insert_before":
            final, final_count = [*payload, *anchor], 1
        else:
            final, final_count = payload, 1
        result.append(
            smali_operation(
                f"340.anchor.{record['id']}",
                ANCHORED_340_INTENTS[record["id"]],
                descriptor,
                method,
                record["mode"],
                anchor,
                len(matches),
                payload,
                final,
                final_count,
                policy,
                selected,
            )
        )
    if len(result) != 7:
        raise ValueError(f"Expected 7 anchored edits, generated {len(result)}")
    return result


def anchored_operations_430(
    manifest: dict[str, Any], classes: dict[str, Path]
) -> list[dict[str, Any]]:
    result = []
    for record in manifest["operations"]:
        descriptor = record["descriptor"]
        lines = classes[descriptor].read_text(encoding="utf-8").splitlines()
        ranges = method_ranges(lines)
        anchor = [line for _, line in significant(record["anchor"])]
        payload = [line for _, line in significant(record["payload"])]
        matches = sequence_matches(lines, anchor)
        if len(matches) != record["expected_anchor_count"]:
            raise ValueError(f"430 anchor cardinality drift for {record['id']}")
        owners = {owning_method(ranges, start, end)[2] for start, end in matches}
        if owners != {ANCHORED_430_METHODS[record["id"]]}:
            raise ValueError(f"430 method ownership drift for {record['id']}: {sorted(owners)}")
        if record["mode"] == "insert_after":
            final = [*anchor, *payload]
        elif record["mode"] == "insert_before":
            final = [*payload, *anchor]
        else:
            final = payload
        result.append(
            smali_operation(
                f"430.anchor.{record['id']}",
                ANCHORED_430_INTENTS[record["id"]],
                descriptor,
                ANCHORED_430_METHODS[record["id"]],
                record["mode"],
                anchor,
                record["expected_anchor_count"],
                payload,
                final,
                1,
            )
        )
    if len(result) != 6:
        raise ValueError(f"Expected 6 430 edits, generated {len(result)}")
    return result


def overlay_operation(
    operation_id: str,
    source_prefix: str,
    target_prefix: str,
    source_root: Path,
    intent_ids: tuple[str, ...],
    expected_count: int,
) -> dict[str, Any]:
    files = regular_files(source_root)
    records = source_records(source_root, files)
    if len(records) != expected_count:
        raise ValueError(f"Overlay {source_prefix} has {len(records)} files, expected {expected_count}")
    return {
        "collision_policy": "forbid",
        "intent_ids": sorted(intent_ids),
        "kind": "overlay_tree",
        "operation_id": operation_id,
        "source_files": records,
        "source_manifest_sha256": canonical_sha256(records),
        "source_prefix": source_prefix,
        "target_prefix": target_prefix,
    }


def canonical_xml(element: ET.Element) -> str:
    for node in element.iter():
        if node.text is not None and not node.text.strip():
            node.text = None
        if node.tail is not None and not node.tail.strip():
            node.tail = None
    return ET.canonicalize(ET.tostring(element, encoding="unicode"), strip_text=True)


def parse_resource_xml(xml: str) -> ET.Element:
    root = ET.fromstring(f'<resources xmlns:android="{ANDROID_NS}">{xml}</resources>')
    if len(root) != 1:
        raise ValueError("Expected exactly one resource entry")
    return root[0]


def resource_identity(element: ET.Element) -> tuple[str, str]:
    resource_type = element.attrib.get("type", element.tag)
    name = element.attrib.get("name")
    if not resource_type or not name:
        raise ValueError("Resource entry lacks a type or name")
    return resource_type, name


def append_resource_operations(source: Path) -> list[dict[str, Any]]:
    result = []
    files = regular_files(source / "appendRes")
    if len(files) != 8:
        raise ValueError(f"Expected 8 append-resource fragments, found {len(files)}")
    for path in files:
        fragment = path.read_text(encoding="utf-8")
        root = ET.fromstring(f"<resources>{fragment}</resources>")
        entries = []
        for element in root:
            resource_type, name = resource_identity(element)
            entries.append(
                {
                    "canonical_xml": canonical_xml(element),
                    "name": name,
                    "resource_type": resource_type,
                }
            )
        entries.sort(key=lambda item: (item["resource_type"], item["name"]))
        relative = path.relative_to(source / "appendRes").as_posix()
        result.append(
            {
                "archive_path": f"res/{relative}",
                "entries": entries,
                "intent_ids": ["settings-entry", "welcome-flow"],
                "kind": "append_resource_entries",
                "operation_id": f"340.resource.append.{relative.replace('/', '.').replace('.xml', '')}",
            }
        )
    return result


def replace_resource_operations(source: Path) -> list[dict[str, Any]]:
    changed = read_json(source / "resourcePatches" / "changed_values.json")
    result = []
    for identity, values in sorted(changed.items()):
        archive_path, resource_identity = identity.split("::", 1)
        resource_type, name = resource_identity.split("/", 1)
        entry = lambda xml: {
            "canonical_xml": canonical_xml(parse_resource_xml(xml)),
            "name": name,
            "resource_type": resource_type,
        }
        result.append(
            {
                "after": entry(values["modified"]),
                "archive_path": archive_path,
                "before": entry(values["stock"]),
                "intent_ids": ["privacy-hardening"],
                "kind": "replace_resource_entry",
                "operation_id": f"340.resource.replace.{name}",
            }
        )
    if len(result) != 2:
        raise ValueError(f"Expected 2 resource replacements, generated {len(result)}")
    return result


def manifest_operation(source: Path) -> dict[str, Any]:
    component = ET.parse(source / "manifest" / "added_components.xml").getroot()
    android_name = component.attrib.get(f"{{{ANDROID_NS}}}name")
    if not android_name:
        raise ValueError("Manifest component lacks android:name")
    return {
        "archive_path": "AndroidManifest.xml",
        "components": [
            {
                "android_name": android_name,
                "canonical_xml": canonical_xml(component),
                "tag": component.tag,
            }
        ],
        "intent_ids": ["settings-entry", "welcome-flow"],
        "kind": "append_manifest_components",
        "operation_id": "340.manifest.append-settings-activity",
    }


def custom_descriptors(source_root: Path, expected: int) -> list[str]:
    descriptors = sorted(descriptor_from_file(path) for path in regular_files(source_root))
    if len(descriptors) != expected or len(descriptors) != len(set(descriptors)):
        raise ValueError("Custom descriptor set drift")
    return descriptors


def dex_string_assertions(
    dex_entry: str, required: tuple[str, ...], forbidden: tuple[str, ...], prefix: str
) -> list[dict[str, Any]]:
    return [
        {
            "assertion_id": f"{prefix}.absent",
            "dex_entry": dex_entry,
            "kind": "dex_string_substrings_absent",
            "substrings": sorted(forbidden),
        },
        {
            "assertion_id": f"{prefix}.present",
            "dex_entry": dex_entry,
            "kind": "dex_strings_present",
            "strings": sorted(required),
        },
    ]


def validate_overlay_absent(
    decode: Path, source_root: Path, target_prefix: str, label: str
) -> None:
    collisions = [
        (decode / target_prefix / path.relative_to(source_root)).as_posix()
        for path in regular_files(source_root)
        if (decode / target_prefix / path.relative_to(source_root)).exists()
    ]
    if collisions:
        raise ValueError(f"{label} overlay target already exists: {collisions[0]}")


def validate_340_decode_preconditions(source: Path, decode: Path) -> None:
    validate_overlay_absent(decode, source / "newCode", "smali_classes11", "340 custom-code")
    validate_overlay_absent(decode, source / "newRes", "res", "340 resource")

    for fragment_path in regular_files(source / "appendRes"):
        relative = fragment_path.relative_to(source / "appendRes")
        target_path = decode / "res" / relative
        if not target_path.is_file():
            raise FileNotFoundError(f"Append-resource target missing: {target_path}")
        fragment = ET.fromstring(
            f"<resources>{fragment_path.read_text(encoding='utf-8')}</resources>"
        )
        target = ET.parse(target_path).getroot()
        target_identities = {resource_identity(element) for element in target}
        for element in fragment:
            identity = resource_identity(element)
            if identity in target_identities:
                raise ValueError(
                    f"Append-resource identity already exists in {target_path}: {identity}"
                )

    changed = read_json(source / "resourcePatches" / "changed_values.json")
    for key, values in sorted(changed.items()):
        archive_path, encoded_identity = key.split("::", 1)
        expected_identity = tuple(encoded_identity.split("/", 1))
        target_path = decode / archive_path
        if not target_path.is_file():
            raise FileNotFoundError(f"Replacement-resource target missing: {target_path}")
        matches = [
            element
            for element in ET.parse(target_path).getroot()
            if resource_identity(element) == expected_identity
        ]
        if len(matches) != 1:
            raise ValueError(
                f"Replacement-resource identity {expected_identity} occurs {len(matches)} times "
                f"in {target_path}"
            )
        expected_before = canonical_xml(parse_resource_xml(values["stock"]))
        actual_before = canonical_xml(matches[0])
        if actual_before != expected_before:
            raise ValueError(f"Replacement-resource before XML drift in {target_path}: {key}")

    component = ET.parse(source / "manifest" / "added_components.xml").getroot()
    component_name = component.attrib.get(f"{{{ANDROID_NS}}}name")
    manifest = ET.parse(decode / "AndroidManifest.xml").getroot()
    application = manifest.find("application")
    if application is None:
        raise ValueError("Stock 340 manifest has no application element")
    if any(
        child.tag == component.tag
        and child.attrib.get(f"{{{ANDROID_NS}}}name") == component_name
        for child in application
    ):
        raise ValueError(f"Manifest component already exists: {component.tag} {component_name}")
    if not (decode / "assets" / "drawables.bin").is_file():
        raise FileNotFoundError("Stock 340 assets/drawables.bin is absent")


def intent_spec() -> dict[str, Any]:
    descriptions = {
        "app-context": "Capture the application context during process startup",
        "block-explore": "Block Explore network requests when selected",
        "block-feed": "Block home-feed network requests when selected",
        "block-profile-ads": "Block exact profile-ad network requests when selected",
        "block-reels": "Block Reels request paths and selected central endpoints",
        "block-shopping": "Block supported Shopping request paths when selected",
        "block-stories": "Block Stories tray network requests when selected",
        "cache-lifecycle": "Capture and release the feed cache coordinator lifecycle",
        "privacy-hardening": "Exclude inherited telemetry and crash-reporting behavior",
        "settings-entry": "Expose distraction-free controls from the profile options surface",
        "welcome-flow": "Show the retained first-run DFInsta welcome flow",
    }
    dependencies = {
        "app-context": ["application-onCreate"],
        "block-explore": ["request-uri"],
        "block-feed": ["request-uri"],
        "block-profile-ads": ["request-uri"],
        "block-reels": ["request-uri"],
        "block-shopping": ["request-uri"],
        "block-stories": ["request-uri"],
        "cache-lifecycle": ["feed-cache-coordinator"],
        "privacy-hardening": ["custom-code-bundle"],
        "settings-entry": ["current-profile-options"],
        "welcome-flow": ["root-activity"],
    }
    fallbacks = {
        "block-explore": ["response-rewrite"],
        "block-feed": ["response-rewrite"],
        "block-profile-ads": ["substring-profile-ad-match"],
        "block-reels": ["global-string-pool-edit"],
        "block-shopping": ["unverified-bloks-substitution"],
        "block-stories": ["hide-own-story"],
        "privacy-hardening": ["startup-telemetry", "third-party-crash-reporting"],
    }
    strategies = {
        "app-context": ["smali_edit"],
        "block-explore": ["smali_edit"],
        "block-feed": ["smali_edit"],
        "block-profile-ads": ["smali_edit"],
        "block-reels": ["smali_edit"],
        "block-shopping": ["smali_edit"],
        "block-stories": ["smali_edit"],
        "cache-lifecycle": ["smali_edit"],
        "privacy-hardening": ["delete_path", "overlay_tree", "replace_resource_entry"],
        "settings-entry": [
            "append_manifest_components",
            "append_resource_entries",
            "overlay_tree",
            "smali_edit",
        ],
        "welcome-flow": [
            "append_manifest_components",
            "append_resource_entries",
            "overlay_tree",
            "smali_edit",
        ],
    }
    return {
        "hooks": [
            {
                "allowed_strategies": strategies[intent_id],
                "description": descriptions[intent_id],
                "disposition": "retain",
                "feature_id": intent_id,
                "forbidden_fallbacks": sorted(fallbacks.get(intent_id, [])),
                "hook_id": intent_id,
                "semantic_dependencies": sorted(dependencies[intent_id]),
            }
            for intent_id in INTENT_IDS
        ],
        "policy_revision": "dfinsta-phase-b-1",
        "schema_version": 2,
    }


def resolution_340(
    root: Path, stock_decode: Path, stock_apk: Path, intent_hash: str
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    expected_apk_sha256 = "68f4546f8cb597a668d6033916200ef99191a9006350fcd986fd33392aea5113"
    actual_apk_sha256 = sha256_file(stock_apk)
    if actual_apk_sha256 != expected_apk_sha256:
        raise ValueError(
            f"Stock 340 APK SHA-256 drift: {actual_apk_sha256} != {expected_apk_sha256}"
        )
    source = root / "dfinsta_source_1.4.1"
    decode = stock_decode
    endpoint_path = source / "patches" / "endpoint_replacements.json"
    anchored_path = source / "patches" / "anchored_patches.json"
    endpoints = read_json(endpoint_path)
    anchored = read_json(anchored_path)
    descriptors = {
        record["descriptor"] for record in endpoints["operations"] + anchored["operations"]
    }
    classes = resolve_classes(decode, descriptors)
    validate_340_decode_preconditions(source, decode)
    smali = endpoint_operations(endpoints, classes) + anchored_operations_340(anchored, classes)
    if len(smali) != 45:
        raise ValueError(f"Expected 45 340 smali edits, generated {len(smali)}")

    consumed = [endpoint_path, anchored_path]
    for directory in ("newCode", "newRes", "appendRes"):
        consumed.extend(regular_files(source / directory))
    consumed.extend(
        [
            source / "resourcePatches" / "changed_values.json",
            source / "manifest" / "added_components.xml",
        ]
    )
    records = source_records(root, consumed)
    descriptors_340 = custom_descriptors(source / "newCode", 9)
    dex_entries = ["classes.dex", *(f"classes{index}.dex" for index in range(2, 12))]
    operations = [
        *smali,
        overlay_operation(
            "340.overlay.custom-code",
            "dfinsta_source_1.4.1/newCode",
            "smali_classes11",
            source / "newCode",
            ("privacy-hardening",),
            9,
        ),
        overlay_operation(
            "340.overlay.new-resources",
            "dfinsta_source_1.4.1/newRes",
            "res",
            source / "newRes",
            ("settings-entry", "welcome-flow"),
            91,
        ),
        *append_resource_operations(source),
        *replace_resource_operations(source),
        manifest_operation(source),
        {
            "archive_path": "assets/drawables.bin",
            "expected_present": True,
            "intent_ids": ["privacy-hardening"],
            "kind": "delete_path",
            "operation_id": "340.delete.assets-drawables-bin",
        },
    ]
    assertions = [
        {
            "assertion_id": "340.custom-descriptors",
            "descriptors": descriptors_340,
            "dex_entry": "classes11.dex",
            "kind": "descriptors_present",
        },
        {
            "assertion_id": "340.dex-topology",
            "entries": dex_entries,
            "kind": "dex_entry_set_equality",
        },
        *dex_string_assertions(
            "classes11.dex",
            (
                "Lcom/dfinstagram/DistractionFree;",
                "Lcom/dfinstagram/SettingsWrapper;",
                "Lcom/dfinstagram/hooks;",
                "Lcom/dfinstagram/startapp;",
                "clearFeedCache",
                "improveRemoveAdsProfile",
                "improveRemovePosts",
                "improveRemoveReels",
                "improveRemoveShopping",
                "improveRemoveStories",
                "setFeedCache",
                "throwIfBlocked",
            ),
            (
                "AmplitudeEventsSender",
                "Lcom/acra/",
                "ReportsCrashes",
                "jniHandlerSendHeaders",
                "jniHandlerSendRequest",
                "modifyFeedResponse",
                "modifyTigonBuffer",
                "nativeReadBufferRead",
                "nativeReadBufferSize",
            ),
            "340.bytes",
        ),
    ]
    resolution = {
        "additional_assertions": sorted(assertions, key=lambda item: item["assertion_id"]),
        "backend": {
            "dex_entries": dex_entries,
            "kind": "apktool_full_rebuild",
            "profile_id": "apktool-2.9.3-aapt1",
        },
        "intent_sha256": intent_hash,
        "intent_statuses": [
            {"intent_id": intent_id, "rationale": None, "status": "implemented"}
            for intent_id in INTENT_IDS
        ],
        "operations": operations,
        "schema_version": 3,
        "source_bundle_sha256": canonical_sha256(records),
        "target": {
            "apk_sha256": expected_apk_sha256,
            "composition": "monolithic",
            "package_name": "com.instagram.android",
            "version_code": 374010893,
            "version_name": "340.0.0.22.109",
        },
    }
    return resolution, records


def is_signature_artifact(name: str) -> bool:
    parts = name.upper().split("/")
    return len(parts) == 2 and parts[0] == "META-INF" and (
        parts[1] == "MANIFEST.MF"
        or parts[1].startswith("SIG-")
        or parts[1].endswith((".SF", ".RSA", ".DSA", ".EC"))
    )


def resolution_430(
    root: Path, stock_decode: Path, stock_apk: Path, intent_hash: str
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    expected_apk_sha256 = "38ae9861b9ca89f60f41767324e1c3d54a4e3a00ed5555b92660a08e6db14754"
    actual_apk_sha256 = sha256_file(stock_apk)
    if actual_apk_sha256 != expected_apk_sha256:
        raise ValueError(
            f"Stock 430 APK SHA-256 mismatch: {actual_apk_sha256} != {expected_apk_sha256}"
        )
    source = root / "dfinsta_source_430"
    anchored_path = source / "patches" / "anchored_patches.json"
    anchored = read_json(anchored_path)
    decode = stock_decode
    classes = resolve_classes(
        decode, {record["descriptor"] for record in anchored["operations"]}
    )
    consumed = [anchored_path, *regular_files(source / "newCode")]
    records = source_records(root, consumed)
    validate_overlay_absent(decode, source / "newCode", "smali_classes20", "430 custom-code")
    operations = [
        *anchored_operations_430(anchored, classes),
        overlay_operation(
            "430.overlay.custom-code",
            "dfinsta_source_430/newCode",
            "smali_classes20",
            source / "newCode",
            ("privacy-hardening",),
            4,
        ),
    ]
    stock_dex = ["classes.dex", *(f"classes{index}.dex" for index in range(2, 20))]
    final_dex = [*stock_dex, "classes20.dex"]
    graft_dex = ["classes.dex", "classes20.dex", "classes3.dex", "classes4.dex", "classes6.dex"]
    with zipfile.ZipFile(stock_apk) as archive:
        signatures = sorted(name for name in archive.namelist() if is_signature_artifact(name))
    if not signatures:
        raise ValueError("Stock 430 APK contains no top-level signature artifacts")
    exclusions = sorted({*graft_dex, *signatures})
    descriptors = custom_descriptors(source / "newCode", 4)
    assertions = [
        {
            "assertion_id": "430.archive-preservation",
            "exclusions": exclusions,
            "kind": "archive_preservation_except",
        },
        {
            "assertion_id": "430.archive-signatures-absent",
            "entries": signatures,
            "kind": "archive_entries_absent",
        },
        {
            "assertion_id": "430.custom-descriptors",
            "descriptors": descriptors,
            "dex_entry": "classes20.dex",
            "kind": "descriptor_set_equality",
        },
        {
            "assertion_id": "430.dex-topology",
            "entries": final_dex,
            "kind": "dex_entry_set_equality",
        },
        *dex_string_assertions(
            "classes20.dex",
            (
                "Lcom/dfinstagram/SettingsWrapper;",
                "Lcom/dfinstagram/dfinstagram;",
                "Lcom/dfinstagram/hooks;",
                "Lcom/dfinstagram/startapp;",
            ),
            (
                "Amplitude",
                "DistractionFree",
                "FeedCache",
                "Hardcore",
                "Landroid/app/Activity;",
                "Landroid/preference/PreferenceActivity;",
                "Lcom/acra/",
                "Lcom/dfinstagram/preference/Preference;",
                "Lcom/dfinstagram/preference/PreferenceFragment;",
                "Lcom/instagram/",
                "UniFile",
                "donate_",
                "improveRemove",
                "istring",
                "modifyFeedResponse",
                "nativeReadBuffer",
                "welcome",
            ),
            "430.bytes",
        ),
    ]
    omitted = {
        "block-shopping": (
            "Instagram 430 has no standalone Shopping tab; the documented minimal contract retires "
            "unverified distributed commerce blocking."
        ),
        "cache-lifecycle": (
            "The resource-free 430 graft deliberately excludes the 340-specific feed-cache coordinator "
            "and its obfuscated field dependencies."
        ),
        "welcome-flow": (
            "The resource-free 430 graft deliberately excludes the custom Activity, resources, and "
            "first-run welcome UI."
        ),
    }
    resolution = {
        "additional_assertions": sorted(assertions, key=lambda item: item["assertion_id"]),
        "backend": {
            "add_dex_entries": ["classes20.dex"],
            "kind": "stock_dex_graft",
            "profile_id": "apktool-2.9.3-aapt1-api36",
            "replace_dex_entries": ["classes.dex", "classes3.dex", "classes4.dex", "classes6.dex"],
            "stock_dex_entries": stock_dex,
        },
        "intent_sha256": intent_hash,
        "intent_statuses": [
            {
                "intent_id": intent_id,
                "rationale": omitted.get(intent_id),
                "status": "omitted" if intent_id in omitted else "implemented",
            }
            for intent_id in INTENT_IDS
        ],
        "operations": operations,
        "schema_version": 3,
        "source_bundle_sha256": canonical_sha256(records),
        "target": {
            "apk_sha256": expected_apk_sha256,
            "composition": "monolithic",
            "package_name": "com.instagram.android",
            "version_code": 383611248,
            "version_name": "430.0.0.53.80",
        },
    }
    return resolution, records


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--stock-340-decode", required=True, type=Path)
    parser.add_argument("--stock-340-apk", required=True, type=Path)
    parser.add_argument("--stock-430-decode", required=True, type=Path)
    parser.add_argument("--stock-430-apk", required=True, type=Path)
    args = parser.parse_args()

    root = args.repo_root.resolve()
    output = args.output if args.output.is_absolute() else root / args.output
    stock_340_decode = args.stock_340_decode.resolve()
    stock_340_apk = args.stock_340_apk.resolve()
    stock_430_decode = args.stock_430_decode.resolve()
    stock_430_apk = args.stock_430_apk.resolve()
    for path, label in (
        (stock_340_decode, "--stock-340-decode"),
        (stock_430_decode, "--stock-430-decode"),
    ):
        if not path.is_dir():
            parser.error(f"{label} must be an existing directory: {path}")
    for path, label in (
        (stock_340_apk, "--stock-340-apk"),
        (stock_430_apk, "--stock-430-apk"),
    ):
        if not path.is_file():
            parser.error(f"{label} must be an existing file: {path}")
    intent = intent_spec()
    intent_hash = canonical_sha256(intent)
    resolution340, sources340 = resolution_340(
        root, stock_340_decode, stock_340_apk, intent_hash
    )
    resolution430, sources430 = resolution_430(
        root, stock_430_decode, stock_430_apk, intent_hash
    )
    generated = {
        output / "intent_v2.json": intent,
        output / "resolutions" / "instagram_340.json": resolution340,
        output / "resolutions" / "instagram_430.json": resolution430,
        output / "source_manifests" / "instagram_340.json": sources340,
        output / "source_manifests" / "instagram_430.json": sources430,
    }
    for path, value in generated.items():
        write_json(path, value)


if __name__ == "__main__":
    main()

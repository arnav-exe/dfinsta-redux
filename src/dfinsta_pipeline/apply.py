from __future__ import annotations

import hashlib
import io
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .compiler import TargetPortSpecV2
from .contracts import canonical_sha256
from .port_contracts import (
    AppendManifestComponents,
    AppendResourceEntries,
    DeletePath,
    ManifestComponent,
    Operation,
    OverlayTree,
    ReplaceResourceEntry,
    ResourceEntry,
    SmaliEdit,
)


ANDROID_NS = "http://schemas.android.com/apk/res/android"
CLASS_RE = re.compile(r"^\.class\s+.*?(L[^\s]+;)$")
METHOD_RE = re.compile(r"^\.method(?:\s+\S+)*\s+(\S+\([^)]*\)\S+)$")


class ApplyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class OperationResult:
    operation_id: str
    status: Literal["applied", "already_applied"]


@dataclass(frozen=True, slots=True)
class ApplyReport:
    results: tuple[OperationResult, ...]

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


def apply_port(spec: TargetPortSpecV2, work_tree: Path, source_root: Path) -> ApplyReport:
    work_tree = _root(work_tree, "work tree")
    source_root = _root(source_root, "source root")
    descriptor_operation = next(
        (
            operation
            for operation in spec.operations
            if isinstance(operation, SmaliEdit)
            or isinstance(operation, OverlayTree)
            and any(source.relative_path.endswith(".smali") for source in operation.source_files)
        ),
        None,
    )
    try:
        descriptors = _descriptor_index(work_tree) if descriptor_operation else {}
    except ApplyError as error:
        assert descriptor_operation is not None
        raise ApplyError(f"{descriptor_operation.operation_id}: {error}") from error
    results = []
    for operation in spec.operations:
        try:
            status = _apply_operation(operation, work_tree, source_root, descriptors)
        except ApplyError as error:
            raise ApplyError(f"{operation.operation_id}: {error}") from error
        results.append(OperationResult(operation.operation_id, status))
    return ApplyReport(tuple(results))


def _root(path: Path, label: str) -> Path:
    if not isinstance(path, Path):
        raise TypeError(f"{label} must be a Path")
    if path.is_symlink() or not path.is_dir():
        raise ApplyError(f"{label} must be an existing non-symlink directory")
    return path.resolve()


def _confined(root: Path, relative: str) -> Path:
    candidate = root.joinpath(*relative.split("/"))
    current = root
    for part in candidate.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise ApplyError(f"Symlink path is not allowed: {relative}")
        if not current.exists():
            break
    resolved = candidate.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise ApplyError(f"Path escapes root: {relative}")
    return candidate


def _apply_operation(
    operation: Operation, work_tree: Path, source_root: Path, descriptors: dict[str, Path]
) -> Literal["applied", "already_applied"]:
    if isinstance(operation, SmaliEdit):
        return _apply_smali(operation, descriptors)
    if isinstance(operation, OverlayTree):
        return _apply_overlay(operation, work_tree, source_root, descriptors)
    if isinstance(operation, AppendResourceEntries):
        return _append_resources(operation, work_tree)
    if isinstance(operation, ReplaceResourceEntry):
        return _replace_resource(operation, work_tree)
    if isinstance(operation, AppendManifestComponents):
        return _append_manifest(operation, work_tree)
    if isinstance(operation, DeletePath):
        return _delete_path(operation, work_tree)
    raise TypeError(f"Unsupported operation: {type(operation).__name__}")


def _descriptor_index(work_tree: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for smali_root in sorted(work_tree.glob("smali*")):
        if smali_root.is_symlink():
            raise ApplyError(f"Symlink smali root is not allowed: {smali_root}")
        if not smali_root.is_dir():
            continue
        for path in sorted(smali_root.rglob("*.smali")):
            relative = path.relative_to(work_tree).as_posix()
            _confined(work_tree, relative)
            if path.is_symlink() or not path.is_file():
                raise ApplyError(f"Smali source is not a regular file: {relative}")
            descriptor = _class_descriptor(path)
            if descriptor in result:
                raise ApplyError(f"Duplicate smali descriptor: {descriptor}")
            result[descriptor] = path
    return result


def _class_descriptor(path: Path) -> str:
    return _class_descriptor_text(path.read_text(encoding="utf-8"), str(path))


def _class_descriptor_text(text: str, label: str) -> str:
    matches = []
    for line in text.splitlines():
        match = CLASS_RE.fullmatch(line.strip())
        if match:
            matches.append(match.group(1))
    if len(matches) != 1:
        raise ApplyError(f"Smali file must declare exactly one class: {label}")
    return matches[0]


def _method_range(lines: list[str], signature: str) -> tuple[int, int]:
    ranges = []
    start = None
    current = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(".method"):
            if start is not None:
                raise ApplyError("Nested smali method declaration")
            match = METHOD_RE.fullmatch(stripped)
            if not match:
                raise ApplyError(f"Cannot parse smali method declaration: {stripped}")
            start, current = index, match.group(1)
        elif stripped == ".end method":
            if start is None:
                raise ApplyError("Unmatched .end method")
            if current == signature:
                ranges.append((start, index))
            start = current = None
    if start is not None:
        raise ApplyError("Unterminated smali method")
    if len(ranges) != 1:
        raise ApplyError(f"Method {signature} resolved to {len(ranges)} ranges")
    return ranges[0]


def _significant(lines: list[str], start: int, end: int) -> list[tuple[int, str]]:
    result = []
    for index in range(start, end + 1):
        stripped = lines[index].strip()
        if stripped and not stripped.startswith("#") and stripped != ".line" and not stripped.startswith(".line "):
            result.append((index, stripped))
    return result


def _matches(
    lines: list[str], start: int, end: int, sequence: tuple[str, ...]
) -> list[tuple[int, int]]:
    significant = _significant(lines, start, end)
    wanted = _normalized_sequence(sequence)
    if not wanted:
        raise ApplyError("Smali sequence has no significant lines")
    matches = []
    for index in range(len(significant) - len(wanted) + 1):
        window = significant[index : index + len(wanted)]
        if [line for _, line in window] == wanted:
            matches.append((window[0][0], window[-1][0]))
    return matches


def _normalized_sequence(sequence: tuple[str, ...]) -> list[str]:
    result = []
    for line in sequence:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and stripped != ".line" and not stripped.startswith(".line "):
            result.append(stripped)
    return result


def _sequence_count(haystack: tuple[str, ...], needle: tuple[str, ...]) -> int:
    normalized_haystack = _normalized_sequence(haystack)
    normalized_needle = _normalized_sequence(needle)
    return sum(
        normalized_haystack[index : index + len(normalized_needle)] == normalized_needle
        for index in range(len(normalized_haystack) - len(normalized_needle) + 1)
    )


def _expected_precondition_count(operation: SmaliEdit, selected_count: int) -> int:
    if operation.mode != "replace":
        return operation.expected_precondition_count
    retained_per_final = _sequence_count(operation.final_sequence, operation.precondition_sequence)
    return (
        operation.expected_precondition_count
        - selected_count
        + retained_per_final * operation.expected_final_count
    )


def _render_payload(payload: tuple[str, ...], anchor: str) -> list[str]:
    indent = anchor[: len(anchor) - len(anchor.lstrip())] or "    "
    return [line if line[:1].isspace() else f"{indent}{line}" for line in payload]


def _read_text_lines(path: Path) -> tuple[list[str], str, bool]:
    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ApplyError(f"File is not UTF-8: {path}") from error
    crlf = data.count(b"\r\n")
    bare_lf = data.count(b"\n") - crlf
    if crlf and bare_lf:
        raise ApplyError(f"File has mixed line endings: {path}")
    newline = "\r\n" if crlf else "\n"
    return text.splitlines(), newline, text.endswith(("\n", "\r"))


def _atomic_replace(path: Path, data: bytes) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _apply_smali(
    operation: SmaliEdit, descriptors: dict[str, Path]
) -> Literal["applied", "already_applied"]:
    path = descriptors.get(operation.descriptor)
    if path is None:
        raise ApplyError(f"Smali descriptor not found: {operation.descriptor}")
    lines, newline, trailing_newline = _read_text_lines(path)
    method_start, method_end = _method_range(lines, operation.method_signature)
    final_matches = _matches(lines, method_start, method_end, operation.final_sequence)
    pre_matches = _matches(lines, method_start, method_end, operation.precondition_sequence)

    if operation.expected_final_count:
        if len(final_matches) == operation.expected_final_count:
            selected_count = (
                operation.expected_precondition_count
                if operation.match_policy == "all"
                else 1
            )
            if len(pre_matches) != _expected_precondition_count(operation, selected_count):
                raise ApplyError(f"Final state has unexpected preconditions for {operation.operation_id}")
            return "already_applied"
        if final_matches:
            raise ApplyError(f"Partial final state for {operation.operation_id}")
        if len(pre_matches) != operation.expected_precondition_count:
            raise ApplyError(f"Precondition cardinality mismatch for {operation.operation_id}")
    else:
        if not pre_matches:
            return "already_applied"
        if len(pre_matches) != operation.expected_precondition_count:
            raise ApplyError(f"Removal cardinality mismatch for {operation.operation_id}")

    selected = pre_matches
    if operation.match_policy == "occurrence":
        assert operation.occurrence is not None
        if operation.occurrence >= len(pre_matches):
            raise ApplyError(f"Smali occurrence is out of range for {operation.operation_id}")
        selected = [pre_matches[operation.occurrence]]
    for (_, previous_end), (next_start, _) in zip(selected, selected[1:]):
        if next_start <= previous_end:
            raise ApplyError(f"Overlapping smali matches for {operation.operation_id}")

    for raw_start, raw_end in reversed(selected):
        payload = _render_payload(operation.payload, lines[raw_start])
        if operation.mode == "insert_before":
            lines[raw_start:raw_start] = payload
        elif operation.mode == "insert_after":
            lines[raw_end + 1 : raw_end + 1] = payload
        else:
            lines[raw_start : raw_end + 1] = payload
    rendered = newline.join(lines) + (newline if trailing_newline else "")
    verified = rendered.splitlines()
    verify_start, verify_end = _method_range(verified, operation.method_signature)
    final_count = len(_matches(verified, verify_start, verify_end, operation.final_sequence))
    if final_count != operation.expected_final_count:
        raise ApplyError(f"Final cardinality mismatch for {operation.operation_id}")
    pre_count = len(_matches(verified, verify_start, verify_end, operation.precondition_sequence))
    selected_count = len(selected)
    expected_pre = _expected_precondition_count(operation, selected_count)
    if pre_count != expected_pre:
        raise ApplyError(f"Precondition still has an unexpected cardinality for {operation.operation_id}")
    _atomic_replace(path, rendered.encode("utf-8"))
    return "applied"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _apply_overlay(
    operation: OverlayTree,
    work_tree: Path,
    source_root: Path,
    descriptors: dict[str, Path],
) -> Literal["applied", "already_applied"]:
    records = []
    overlay_descriptors: dict[str, Path] = {}
    for source_file in operation.source_files:
        source_relative = f"{operation.source_prefix}/{source_file.relative_path}"
        target_relative = f"{operation.target_prefix}/{source_file.relative_path}"
        source = _confined(source_root, source_relative)
        target = _confined(work_tree, target_relative)
        if source.is_symlink() or not source.is_file():
            raise ApplyError(f"Overlay source is not a regular file: {source_relative}")
        data = source.read_bytes()
        if _sha256(data) != source_file.sha256:
            raise ApplyError(f"Overlay source hash mismatch: {source_relative}")
        descriptor = None
        if source_file.relative_path.endswith(".smali"):
            try:
                source_text = data.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ApplyError(f"Overlay smali is not UTF-8: {source_relative}") from error
            descriptor = _class_descriptor_text(source_text, source_relative)
            existing = descriptors.get(descriptor) or overlay_descriptors.get(descriptor)
            if existing is not None and existing != target:
                raise ApplyError(f"Overlay descriptor already exists: {descriptor}")
            overlay_descriptors[descriptor] = target
        records.append((target, data, descriptor))

    states = ["absent" if not target.exists() else "exact" if target.is_file() and target.read_bytes() == data else "collision" for target, data, _ in records]
    if all(state == "exact" for state in states):
        return "already_applied"
    if operation.collision_policy == "require_exact":
        raise ApplyError(f"Overlay require_exact state mismatch for {operation.operation_id}")
    if not all(state == "absent" for state in states):
        raise ApplyError(f"Overlay partial or collision state for {operation.operation_id}")
    for target, data, _ in records:
        target.parent.mkdir(parents=True, exist_ok=True)
        _confined(work_tree, target.relative_to(work_tree).as_posix())
        _atomic_replace(target, data)
    descriptors.update(overlay_descriptors)
    return "applied"


def _xml_path(work_tree: Path, relative: str) -> Path:
    path = _confined(work_tree, relative)
    if path.is_symlink() or not path.is_file():
        raise ApplyError(f"XML target is not a regular file: {relative}")
    return path


def _parse_xml(path: Path) -> ET.ElementTree:
    return _parse_xml_data(path.read_bytes(), str(path))


def _parse_xml_data(data: bytes, label: str) -> ET.ElementTree:
    try:
        namespaces = list(ET.iterparse(io.BytesIO(data), events=("start-ns",)))
        for _, (prefix, uri) in namespaces:
            if not re.fullmatch(r"ns\d+", prefix):
                ET.register_namespace(prefix, uri)
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
        return ET.ElementTree(ET.fromstring(data, parser=parser))
    except ET.ParseError as error:
        raise ApplyError(f"Invalid XML: {label}") from error


def _canonical(element: ET.Element) -> str:
    clone = ET.fromstring(ET.tostring(element, encoding="utf-8"))
    for node in clone.iter():
        if node.text is not None and not node.text.strip():
            node.text = None
        if node.tail is not None and not node.tail.strip():
            node.tail = None
    return ET.canonicalize(ET.tostring(clone, encoding="unicode"), strip_text=True)


def _declared_element(xml: str) -> ET.Element:
    try:
        wrapper = ET.fromstring(f'<root xmlns:android="{ANDROID_NS}">{xml}</root>')
    except ET.ParseError as error:
        raise ApplyError("Invalid declared canonical XML") from error
    if len(wrapper) != 1:
        raise ApplyError("Declared canonical XML must contain exactly one element")
    return wrapper[0]


def _render_xml(tree: ET.ElementTree) -> bytes:
    ET.register_namespace("android", ANDROID_NS)
    ET.indent(tree, space="    ")
    stream = io.BytesIO()
    tree.write(stream, encoding="utf-8", xml_declaration=True, short_empty_elements=True)
    return stream.getvalue()


def _resource_identity(element: ET.Element) -> tuple[str, str] | None:
    if not isinstance(element.tag, str):
        return None
    name = element.attrib.get("name")
    resource_type = element.attrib.get("type", _local_name(element.tag))
    return (resource_type, name) if name else None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _declared_resource(entry: ResourceEntry) -> ET.Element:
    element = _declared_element(entry.canonical_xml)
    if _resource_identity(element) != entry.identity:
        raise ApplyError(f"Declared resource identity mismatch: {entry.identity}")
    return element


def _resource_map(root: ET.Element, wanted: set[tuple[str, str]]) -> dict[tuple[str, str], ET.Element]:
    found: dict[tuple[str, str], ET.Element] = {}
    for element in root:
        identity = _resource_identity(element)
        if identity not in wanted:
            continue
        if identity in found:
            raise ApplyError(f"Duplicate resource identity: {identity}")
        found[identity] = element
    return found


def _append_resources(
    operation: AppendResourceEntries, work_tree: Path
) -> Literal["applied", "already_applied"]:
    path = _xml_path(work_tree, operation.archive_path)
    tree = _parse_xml(path)
    root = tree.getroot()
    if _local_name(root.tag) != "resources":
        raise ApplyError("Resource target root must be <resources>")
    declared = {entry.identity: _declared_resource(entry) for entry in operation.entries}
    wanted = {entry.identity for entry in operation.entries}
    found = _resource_map(root, wanted)
    states = []
    for entry in operation.entries:
        element = found.get(entry.identity)
        expected = _canonical(declared[entry.identity])
        states.append("absent" if element is None else "exact" if _canonical(element) == expected else "mismatch")
    if all(state == "exact" for state in states):
        return "already_applied"
    if not all(state == "absent" for state in states):
        raise ApplyError(f"Resource partial or mismatch state for {operation.operation_id}")
    for entry in operation.entries:
        root.append(declared[entry.identity])
    rendered = _render_xml(tree)
    verified = _resource_map(_parse_xml_data(rendered, operation.archive_path).getroot(), wanted)
    if any(_canonical(verified[entry.identity]) != _canonical(declared[entry.identity]) for entry in operation.entries):
        raise ApplyError(f"Resource verification failed for {operation.operation_id}")
    _atomic_replace(path, rendered)
    return "applied"


def _replace_resource(
    operation: ReplaceResourceEntry, work_tree: Path
) -> Literal["applied", "already_applied"]:
    path = _xml_path(work_tree, operation.archive_path)
    before_element = _declared_resource(operation.before)
    after_element = _declared_resource(operation.after)
    before = _canonical(before_element)
    after = _canonical(after_element)
    if before == after:
        raise ApplyError(f"Resource replacement is a semantic no-op for {operation.operation_id}")
    tree = _parse_xml(path)
    root = tree.getroot()
    found = _resource_map(root, {operation.before.identity})
    element = found.get(operation.before.identity)
    if element is None:
        raise ApplyError(f"Resource replacement identity is absent for {operation.operation_id}")
    actual = _canonical(element)
    if actual == after and actual != before:
        return "already_applied"
    if actual != before or actual == after:
        raise ApplyError(f"Resource replacement state mismatch for {operation.operation_id}")
    index = list(root).index(element)
    root.remove(element)
    root.insert(index, after_element)
    rendered = _render_xml(tree)
    verified = _resource_map(
        _parse_xml_data(rendered, operation.archive_path).getroot(),
        {operation.after.identity},
    )
    if _canonical(verified[operation.after.identity]) != after:
        raise ApplyError(f"Resource replacement verification failed for {operation.operation_id}")
    _atomic_replace(path, rendered)
    return "applied"


def _manifest_map(
    application: ET.Element, wanted: set[tuple[str, str]]
) -> dict[tuple[str, str], ET.Element]:
    found = {}
    for element in application:
        if not isinstance(element.tag, str):
            continue
        name = element.attrib.get(f"{{{ANDROID_NS}}}name")
        identity = (_local_name(element.tag), name) if name else None
        if identity not in wanted:
            continue
        if identity in found:
            raise ApplyError(f"Duplicate manifest component: {identity}")
        found[identity] = element
    return found


def _application(root: ET.Element) -> ET.Element:
    applications = [
        element
        for element in root
        if isinstance(element.tag, str) and _local_name(element.tag) == "application"
    ]
    if len(applications) != 1:
        raise ApplyError(f"Manifest must contain exactly one application, found {len(applications)}")
    return applications[0]


def _append_manifest(
    operation: AppendManifestComponents, work_tree: Path
) -> Literal["applied", "already_applied"]:
    path = _xml_path(work_tree, operation.archive_path)
    tree = _parse_xml(path)
    application = _application(tree.getroot())
    wanted = {component.identity for component in operation.components}
    found = _manifest_map(application, wanted)
    states = []
    for component in operation.components:
        element = found.get(component.identity)
        expected = _canonical(_declared_element(component.canonical_xml))
        states.append("absent" if element is None else "exact" if _canonical(element) == expected else "mismatch")
    if all(state == "exact" for state in states):
        return "already_applied"
    if not all(state == "absent" for state in states):
        raise ApplyError(f"Manifest partial or mismatch state for {operation.operation_id}")
    for component in operation.components:
        element = _declared_element(component.canonical_xml)
        if (_local_name(element.tag), element.attrib.get(f"{{{ANDROID_NS}}}name")) != component.identity:
            raise ApplyError(f"Manifest declared identity mismatch for {operation.operation_id}")
        application.append(element)
    rendered = _render_xml(tree)
    verified = _manifest_map(
        _application(_parse_xml_data(rendered, operation.archive_path).getroot()), wanted
    )
    if any(_canonical(verified[component.identity]) != _canonical(_declared_element(component.canonical_xml)) for component in operation.components):
        raise ApplyError(f"Manifest verification failed for {operation.operation_id}")
    _atomic_replace(path, rendered)
    return "applied"


def _delete_path(
    operation: DeletePath, work_tree: Path
) -> Literal["applied", "already_applied"]:
    path = _confined(work_tree, operation.archive_path)
    if not path.exists():
        return "already_applied"
    if path.is_symlink():
        raise ApplyError(f"Delete target is a symlink: {operation.archive_path}")
    if not operation.expected_present:
        raise ApplyError(f"Unexpected delete target exists: {operation.archive_path}")
    if path.is_dir():
        raise ApplyError(f"Delete target directory has no content manifest: {operation.archive_path}")
    elif path.is_file():
        path.unlink()
    else:
        raise ApplyError(f"Delete target is not a file or directory: {operation.archive_path}")
    if path.exists() or path.is_symlink():
        raise ApplyError(f"Delete verification failed for {operation.operation_id}")
    return "applied"

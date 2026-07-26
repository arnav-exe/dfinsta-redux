from __future__ import annotations

import hashlib
import io
import os
import re
import stat
import struct
import xml.etree.ElementTree as ET
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

from .compiler import TargetPortSpecV2
from .contracts import canonical_sha256
from .port_contracts import (
    AppendManifestComponents,
    AppendResourceEntries,
    ArchiveEntriesAbsent,
    ArchiveEntryNamesAndBytesPreservedExcept,
    BytesAbsent,
    BytesPresent,
    DeletePath,
    DescriptorSetEquality,
    DescriptorsPresent,
    DexEntrySetEquality,
    DexStringSubstringsAbsent,
    DexStringsAbsent,
    DexStringsPresent,
    ExactSmaliSequenceCount,
    Operation,
    OperationPostcondition,
    OverlayTree,
    ReplaceResourceEntry,
    ResourceEntry,
    SmaliEdit,
    StaticAssertion,
    StockDexGraftBackend,
)


ANDROID_NS = "http://schemas.android.com/apk/res/android"
CLASS_RE = re.compile(r"^\.class\s+.*?(L[^\s]+;)$")
METHOD_RE = re.compile(r"^\.method(?:\s+\S+)*\s+(\S+\([^)]*\)\S+)$")
DEX_ENTRY_RE = re.compile(r"classes(?:[2-9]|[1-9][0-9]+)?\.dex")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
LOWER_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")


class VerificationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AssertionResult:
    assertion_id: str
    kind: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class DecodedArtifactReceipt:
    output_apk_sha256: str
    decoded_tree_sha256: str
    decoder_profile_id: str
    decoder_capability_sha256: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.output_apk_sha256, "receipt output APK SHA-256"),
            (self.decoded_tree_sha256, "receipt decoded tree SHA-256"),
            (self.decoder_capability_sha256, "receipt decoder capability SHA-256"),
        ):
            if type(value) is not str or SHA256_RE.fullmatch(value) is None:
                raise ValueError(f"Invalid {label}")
        if (
            type(self.decoder_profile_id) is not str
            or LOWER_ID_RE.fullmatch(self.decoder_profile_id) is None
        ):
            raise ValueError("Invalid lowercase decoder profile ID")


@dataclass(frozen=True, slots=True)
class VerificationReport:
    output_sha256: str
    stock_sha256: str
    decoded_artifact_receipt: DecodedArtifactReceipt
    assertion_results: tuple[AssertionResult, ...]
    operation_proof_count: int
    passed: bool

    @property
    def decoded_tree_sha256(self) -> str:
        return self.decoded_artifact_receipt.decoded_tree_sha256

    @property
    def decoder_profile_id(self) -> str:
        return self.decoded_artifact_receipt.decoder_profile_id

    @property
    def decoder_capability_sha256(self) -> str:
        return self.decoded_artifact_receipt.decoder_capability_sha256

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class _Archive:
    entries: tuple[tuple[str, bytes], ...]

    @property
    def by_name(self) -> dict[str, bytes]:
        return dict(self.entries)


def verify_apk(
    spec: TargetPortSpecV2,
    stock_apk: Path,
    output_apk: Path,
    decoded_output: Path,
    source_root: Path,
    decoded_receipt: DecodedArtifactReceipt,
) -> VerificationReport:
    if not isinstance(spec, TargetPortSpecV2):
        raise TypeError("spec must be a TargetPortSpecV2")
    if not isinstance(decoded_receipt, DecodedArtifactReceipt):
        raise TypeError("decoded_receipt must be a DecodedArtifactReceipt")
    stock_apk = _regular_path(stock_apk, "stock APK")
    output_apk = _regular_path(output_apk, "output APK")
    decoded_output = _directory_path(decoded_output, "decoded output")
    source_root = _directory_path(source_root, "source root")

    stock, stock_sha256 = _load_archive(stock_apk, "stock")
    output, output_sha256 = _load_archive(output_apk, "output")
    if stock_sha256 != spec.target.apk_sha256:
        raise VerificationError("Stock APK SHA-256 does not match the target identity")
    tree_sha256 = decoded_tree_sha256(decoded_output)
    if decoded_receipt.output_apk_sha256 != output_sha256:
        raise VerificationError("Decoded receipt output APK SHA-256 mismatch")
    if decoded_receipt.decoded_tree_sha256 != tree_sha256:
        raise VerificationError("Decoded receipt tree SHA-256 mismatch")

    operations = _operation_proofs(spec)
    _require_backend_topology_assertion(spec)
    descriptor_index: dict[str, Path] | None = None
    dex_strings: dict[str, frozenset[str]] = {}
    results = []
    for assertion in spec.assertions:
        try:
            if isinstance(assertion, (OperationPostcondition, ExactSmaliSequenceCount)):
                if descriptor_index is None:
                    descriptor_index = _descriptor_index(decoded_output)
            passed, detail = _evaluate_assertion(
                assertion,
                operations,
                stock,
                output,
                decoded_output,
                source_root,
                descriptor_index,
                dex_strings,
            )
        except VerificationError as error:
            raise VerificationError(f"{assertion.assertion_id}: {error}") from error
        results.append(AssertionResult(assertion.assertion_id, assertion.kind, passed, detail))

    signature_names = sorted(name for name, _ in output.entries if _is_signature(name))
    if isinstance(spec.backend, StockDexGraftBackend):
        signature_passed = not signature_names
        signature_detail = (
            "matched"
            if signature_passed
            else f"recognized signature artifacts remain: {signature_names!r}"
        )
    else:
        signature_passed = True
        signature_detail = "not applicable to full rebuild"
    results.append(
        AssertionResult(
            "backend.signature-policy",
            "backend_signature_policy",
            signature_passed,
            signature_detail,
        )
    )
    return VerificationReport(
        output_sha256=output_sha256,
        stock_sha256=stock_sha256,
        decoded_artifact_receipt=decoded_receipt,
        assertion_results=tuple(results),
        operation_proof_count=len(operations),
        passed=all(result.passed for result in results),
    )


def decoded_tree_sha256(root: Path) -> str:
    root = _directory_path(root, "decoded output")
    records: list[tuple[str, Path]] = []
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        parent = Path(directory)
        for name in (*directory_names, *file_names):
            path = parent / name
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise VerificationError(f"Symlink or junction in decoded tree: {path}")
        for name in file_names:
            path = parent / name
            relative = path.relative_to(root).as_posix()
            records.append((relative, path))
    digest = hashlib.sha256()
    for relative, path in sorted(records):
        encoded = relative.encode("utf-8")
        data = _read_regular(path)
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _require_backend_topology_assertion(spec: TargetPortSpecV2) -> None:
    matches = [
        assertion
        for assertion in spec.assertions
        if assertion.assertion_id == "backend.final-dex-entries"
    ]
    if len(matches) != 1 or not isinstance(matches[0], DexEntrySetEquality):
        raise VerificationError("Missing or invalid backend.final-dex-entries assertion")
    if matches[0].entries != spec.backend.final_dex_entries:
        raise VerificationError("Backend final DEX assertion disagrees with backend topology")
    if any(assertion.assertion_id == "backend.signature-policy" for assertion in spec.assertions):
        raise VerificationError("backend.signature-policy is verifier-owned")


def _operation_proofs(spec: TargetPortSpecV2) -> dict[str, Operation]:
    operations: dict[str, Operation] = {}
    for operation in spec.operations:
        if operation.operation_id in operations:
            raise VerificationError(f"Duplicate operation: {operation.operation_id}")
        operations[operation.operation_id] = operation
    proofs: dict[str, OperationPostcondition] = {}
    for assertion in spec.assertions:
        if not isinstance(assertion, OperationPostcondition):
            continue
        if assertion.operation_id in proofs:
            raise VerificationError(f"Duplicate operation proof: {assertion.operation_id}")
        operation = operations.get(assertion.operation_id)
        if operation is None:
            raise VerificationError(f"Operation proof references unknown operation: {assertion.operation_id}")
        if assertion.operation_sha256 != canonical_sha256(operation):
            raise VerificationError(f"Operation proof hash mismatch: {assertion.operation_id}")
        proofs[assertion.operation_id] = assertion
    missing = sorted(set(operations) - set(proofs))
    if missing:
        raise VerificationError(f"Missing operation proof: {missing[0]}")
    return operations


def _evaluate_assertion(
    assertion: StaticAssertion,
    operations: dict[str, Operation],
    stock: _Archive,
    output: _Archive,
    decoded_output: Path,
    source_root: Path,
    descriptor_index: dict[str, Path] | None,
    dex_strings: dict[str, frozenset[str]],
) -> tuple[bool, str]:
    if isinstance(assertion, OperationPostcondition):
        assert descriptor_index is not None
        return _verify_operation(
            operations[assertion.operation_id], decoded_output, source_root, descriptor_index
        )
    if isinstance(assertion, ExactSmaliSequenceCount):
        assert descriptor_index is not None
        count = _smali_count(
            descriptor_index, assertion.descriptor, assertion.method_signature, assertion.sequence
        )
        return _count_result(count, assertion.expected_count)
    if isinstance(assertion, DexEntrySetEquality):
        actual = {name for name, _ in output.entries if DEX_ENTRY_RE.fullmatch(name)}
        return _set_result(actual, set(assertion.entries), "DEX entries")
    if isinstance(assertion, DescriptorSetEquality):
        actual = _descriptor_set_for_dex(decoded_output, assertion.dex_entry)
        return _set_result(actual, set(assertion.descriptors), "descriptors")
    if isinstance(assertion, DescriptorsPresent):
        actual = _descriptor_set_for_dex(decoded_output, assertion.dex_entry)
        missing = sorted(set(assertion.descriptors) - actual)
        return (not missing, "matched" if not missing else f"missing descriptors: {missing!r}")
    if isinstance(
        assertion, (DexStringsPresent, DexStringsAbsent, DexStringSubstringsAbsent)
    ):
        strings = dex_strings.get(assertion.dex_entry)
        if strings is None:
            payload = output.by_name.get(assertion.dex_entry)
            if payload is None:
                return False, f"archive entry is absent: {assertion.dex_entry}"
            strings = _dex_string_table(payload)
            dex_strings[assertion.dex_entry] = strings
        wanted = set(
            assertion.substrings
            if isinstance(assertion, DexStringSubstringsAbsent)
            else assertion.strings
        )
        if isinstance(assertion, DexStringsPresent):
            missing = sorted(wanted - strings)
            return (not missing, "matched" if not missing else f"missing strings: {missing!r}")
        if isinstance(assertion, DexStringSubstringsAbsent):
            occurrences = sorted(
                (substring, value)
                for substring in assertion.substrings
                for value in strings
                if substring in value
            )
            return (
                not occurrences,
                "matched"
                if not occurrences
                else f"forbidden substring occurrences: {occurrences!r}",
            )
        present = sorted(wanted & strings)
        return (not present, "matched" if not present else f"unexpected strings: {present!r}")
    if isinstance(assertion, ArchiveEntriesAbsent):
        present = sorted(set(assertion.entries) & set(output.by_name))
        return (not present, "matched" if not present else f"unexpected entries: {present!r}")
    if isinstance(assertion, ArchiveEntryNamesAndBytesPreservedExcept):
        excluded = set(assertion.exclusions)
        stock_entries = {name: data for name, data in stock.entries if name not in excluded}
        output_entries = {name: data for name, data in output.entries if name not in excluded}
        if set(stock_entries) != set(output_entries):
            return _set_result(set(output_entries), set(stock_entries), "preserved archive entries")
        changed = sorted(name for name in stock_entries if stock_entries[name] != output_entries[name])
        return (not changed, "matched" if not changed else f"changed entry bytes: {changed!r}")
    if isinstance(assertion, (BytesPresent, BytesAbsent)):
        payload = output.by_name.get(assertion.archive_path)
        if payload is None:
            return False, f"archive entry is absent: {assertion.archive_path}"
        wanted = bytes.fromhex(assertion.bytes_hex)
        found = wanted in payload
        passed = found if isinstance(assertion, BytesPresent) else not found
        return passed, "matched" if passed else "byte membership mismatch"
    raise TypeError(f"Unsupported assertion: {type(assertion).__name__}")


def _verify_operation(
    operation: Operation,
    decoded_output: Path,
    source_root: Path,
    descriptors: dict[str, Path],
) -> tuple[bool, str]:
    if isinstance(operation, SmaliEdit):
        path = descriptors.get(operation.descriptor)
        if path is None:
            return False, f"smali descriptor is absent: {operation.descriptor}"
        lines = _smali_lines(path)
        try:
            start, end = _method_range(lines, operation.method_signature)
        except VerificationError as error:
            if str(error).endswith("resolved to 0 ranges"):
                return False, f"smali method is absent: {operation.method_signature}"
            raise
        method_lines = lines[start : end + 1]
        final_count = _sequence_count(method_lines, operation.final_sequence)
        if final_count != operation.expected_final_count:
            return _count_result(final_count, operation.expected_final_count)
        precondition_count = _sequence_count(method_lines, operation.precondition_sequence)
        selected_count = (
            operation.expected_precondition_count if operation.match_policy == "all" else 1
        )
        expected_residual = operation.expected_precondition_count
        if operation.mode == "replace":
            retained_per_final = _sequence_count(
                list(operation.final_sequence), operation.precondition_sequence
            )
            expected_residual = (
                operation.expected_precondition_count
                - selected_count
                + retained_per_final * operation.expected_final_count
            )
        if precondition_count != expected_residual:
            return (
                False,
                f"expected residual precondition count {expected_residual}, "
                f"found {precondition_count}",
            )
        return True, "matched"
    if isinstance(operation, OverlayTree):
        for source_file in operation.source_files:
            source_relative = f"{operation.source_prefix}/{source_file.relative_path}"
            target_relative = f"{operation.target_prefix}/{source_file.relative_path}"
            source = _confined(source_root, source_relative)
            target = _confined(decoded_output, target_relative)
            if not source.exists() or not source.is_file():
                return False, f"overlay source is absent: {source_relative}"
            if not target.exists() or not target.is_file():
                return False, f"overlay target is absent: {target_relative}"
            source_data = source.read_bytes()
            if hashlib.sha256(source_data).hexdigest() != source_file.sha256:
                return False, f"overlay source hash mismatch: {source_relative}"
            target_data = target.read_bytes()
            suffix = source_file.relative_path.rsplit(".", 1)[-1].lower()
            if suffix == "smali":
                equal = _canonicalize_labels(
                    _significant_text(source_data, source_relative)
                ) == _canonicalize_labels(_significant_text(target_data, target_relative))
            elif suffix == "xml":
                equal = _xml_semantics(source_data, source_relative) == _xml_semantics(
                    target_data, target_relative
                )
            else:
                equal = source_data == target_data
            if not equal:
                return False, f"overlay target mismatch: {target_relative}"
        return True, "matched"
    if isinstance(operation, AppendResourceEntries):
        return _verify_resources(decoded_output, operation.archive_path, operation.entries)
    if isinstance(operation, ReplaceResourceEntry):
        return _verify_resources(decoded_output, operation.archive_path, (operation.after,))
    if isinstance(operation, AppendManifestComponents):
        return _verify_manifest(decoded_output, operation)
    if isinstance(operation, DeletePath):
        path = _confined(decoded_output, operation.archive_path)
        return (not path.exists() and not path.is_symlink(), "matched" if not path.exists() else "path exists")
    raise TypeError(f"Unsupported operation: {type(operation).__name__}")


def _verify_resources(
    root: Path, archive_path: str, entries: tuple[ResourceEntry, ...]
) -> tuple[bool, str]:
    path = _confined(root, archive_path)
    if not path.exists() or not path.is_file():
        return False, f"resource target is absent: {archive_path}"
    tree = _parse_xml(path.read_bytes(), archive_path)
    xml_root = tree.getroot()
    if _local_name(xml_root.tag) != "resources":
        raise VerificationError("Resource target root must be <resources>")
    wanted = {entry.identity: entry for entry in entries}
    found: dict[tuple[str, str], ET.Element] = {}
    for element in xml_root:
        identity = _resource_identity(element)
        if identity not in wanted:
            continue
        if identity in found:
            raise VerificationError(f"Duplicate resource identity: {identity}")
        found[identity] = element
    for identity, entry in wanted.items():
        element = found.get(identity)
        if element is None:
            return False, f"resource is absent: {identity!r}"
        declared = _declared_element(entry.canonical_xml)
        if _resource_identity(declared) != identity:
            raise VerificationError(f"Declared resource identity mismatch: {identity!r}")
        if _element_semantics(element) != _element_semantics(declared):
            return False, f"resource content mismatch: {identity!r}"
    return True, "matched"


def _verify_manifest(
    root: Path, operation: AppendManifestComponents
) -> tuple[bool, str]:
    path = _confined(root, operation.archive_path)
    if not path.exists() or not path.is_file():
        return False, f"manifest target is absent: {operation.archive_path}"
    tree = _parse_xml(
        path.read_bytes(),
        operation.archive_path,
    )
    applications = [
        child
        for child in tree.getroot()
        if isinstance(child.tag, str) and _local_name(child.tag) == "application"
    ]
    if len(applications) != 1:
        raise VerificationError(f"Manifest must contain exactly one application, found {len(applications)}")
    wanted = {component.identity: component for component in operation.components}
    found: dict[tuple[str, str], ET.Element] = {}
    for element in applications[0]:
        if not isinstance(element.tag, str):
            continue
        name = element.attrib.get(f"{{{ANDROID_NS}}}name")
        identity = (_local_name(element.tag), name) if name else None
        if identity not in wanted:
            continue
        if identity in found:
            raise VerificationError(f"Duplicate manifest component: {identity}")
        found[identity] = element
    for identity, component in wanted.items():
        element = found.get(identity)
        if element is None:
            return False, f"manifest component is absent: {identity!r}"
        declared = _declared_element(component.canonical_xml)
        declared_identity = (
            _local_name(declared.tag),
            declared.attrib.get(f"{{{ANDROID_NS}}}name"),
        )
        if declared_identity != identity:
            raise VerificationError(f"Declared manifest identity mismatch: {identity!r}")
        if _element_semantics(element) != _element_semantics(declared):
            return False, f"manifest content mismatch: {identity!r}"
    return True, "matched"


def _descriptor_index(root: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for smali_root in sorted(root.glob("smali*")):
        _ensure_confined_entry(root, smali_root)
        if not smali_root.is_dir():
            continue
        for path in _regular_files(smali_root, "*.smali"):
            descriptor = _class_descriptor(path)
            if descriptor in result:
                raise VerificationError(f"Duplicate smali descriptor: {descriptor}")
            result[descriptor] = path
    return result


def _descriptor_set_for_dex(root: Path, dex_entry: str) -> set[str]:
    smali_name = "smali" if dex_entry == "classes.dex" else f"smali_classes{dex_entry[7:-4]}"
    smali_root = _confined(root, smali_name)
    if not smali_root.exists():
        return set()
    if not smali_root.is_dir() or smali_root.is_symlink():
        raise VerificationError(f"Decoded DEX path is not a directory: {smali_name}")
    result: set[str] = set()
    for path in _regular_files(smali_root, "*.smali"):
        descriptor = _class_descriptor(path)
        if descriptor in result:
            raise VerificationError(f"Duplicate smali descriptor: {descriptor}")
        result.add(descriptor)
    return result


def _smali_count(
    descriptors: dict[str, Path],
    descriptor: str,
    method_signature: str | None,
    sequence: tuple[str, ...],
) -> int:
    path = descriptors.get(descriptor)
    if path is None:
        return 0
    lines = _smali_lines(path)
    if method_signature is None:
        start, end = 0, len(lines) - 1
    else:
        try:
            start, end = _method_range(lines, method_signature)
        except VerificationError as error:
            if str(error).endswith("resolved to 0 ranges"):
                return 0
            raise
    return _sequence_count(lines[start : end + 1], sequence)


def _smali_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise VerificationError(f"Smali file is not UTF-8: {path}") from error


def _sequence_count(haystack: list[str] | tuple[str, ...], sequence: tuple[str, ...]) -> int:
    significant = _significant(list(haystack))
    wanted = _significant(list(sequence))
    if not wanted:
        raise VerificationError("Smali sequence has no significant lines")
    canonical_wanted = _canonicalize_labels(wanted)
    return sum(
        _canonicalize_labels(significant[index : index + len(wanted)]) == canonical_wanted
        for index in range(len(significant) - len(wanted) + 1)
    )


def _method_range(lines: list[str], signature: str) -> tuple[int, int]:
    ranges = []
    start: int | None = None
    current: str | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(".method"):
            if start is not None:
                raise VerificationError("Nested smali method declaration")
            match = METHOD_RE.fullmatch(stripped)
            if match is None:
                raise VerificationError(f"Cannot parse smali method declaration: {stripped}")
            start, current = index, match.group(1)
        elif stripped == ".end method":
            if start is None:
                raise VerificationError("Unmatched .end method")
            if current == signature:
                ranges.append((start, index))
            start = None
            current = None
    if start is not None:
        raise VerificationError("Unterminated smali method")
    if len(ranges) != 1:
        raise VerificationError(f"Method {signature} resolved to {len(ranges)} ranges")
    return ranges[0]


def _significant(lines: list[str]) -> list[str]:
    return [
        stripped
        for line in lines
        if (stripped := line.strip())
        and not stripped.startswith("#")
        and stripped != ".line"
        and not stripped.startswith(".line ")
    ]


def _significant_text(data: bytes, label: str) -> list[str]:
    try:
        return _significant(data.decode("utf-8").splitlines())
    except UnicodeDecodeError as error:
        raise VerificationError(f"Smali file is not UTF-8: {label}") from error


def _canonicalize_labels(lines: list[str]) -> list[str]:
    labels: dict[str, str] = {}

    def replace_segment(segment: str) -> str:
        def replace(match: re.Match[str]) -> str:
            label = match.group(0)
            return labels.setdefault(label, f":label_{len(labels)}")

        return re.sub(r":[A-Za-z0-9_.$-]+", replace, segment)

    result = []
    for line in lines:
        rendered = []
        start = 0
        quoted = False
        escaped = False
        for index, character in enumerate(line):
            if character == '"' and not escaped:
                if not quoted:
                    rendered.append(replace_segment(line[start:index]))
                    start = index
                else:
                    rendered.append(line[start : index + 1])
                    start = index + 1
                quoted = not quoted
            escaped = character == "\\" and not escaped
            if character != "\\":
                escaped = False
        rendered.append(line[start:] if quoted else replace_segment(line[start:]))
        result.append("".join(rendered))
    return result


def _class_descriptor(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise VerificationError(f"Smali file is not UTF-8: {path}") from error
    matches = [match.group(1) for line in lines if (match := CLASS_RE.fullmatch(line.strip()))]
    if len(matches) != 1:
        raise VerificationError(f"Smali file must declare exactly one class: {path}")
    return matches[0]


def _parse_xml(data: bytes, label: str) -> ET.ElementTree:
    try:
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
        return ET.ElementTree(ET.fromstring(data, parser=parser))
    except ET.ParseError as error:
        raise VerificationError(f"Invalid XML: {label}") from error


def _declared_element(xml: str) -> ET.Element:
    tree = _parse_xml(f'<root xmlns:android="{ANDROID_NS}">{xml}</root>'.encode(), "declared XML")
    root = tree.getroot()
    if len(root) != 1:
        raise VerificationError("Declared canonical XML must contain exactly one element")
    return root[0]


def _xml_semantics(data: bytes, label: str) -> object:
    return _element_semantics(_parse_xml(data, label).getroot())


def _element_semantics(element: ET.Element) -> object:
    if not isinstance(element.tag, str):
        return None
    content: list[object] = []
    if element.text and element.text.strip():
        content.append(("text", element.text.strip()))
    for child in element:
        child_semantics = _element_semantics(child)
        if child_semantics is not None:
            content.append(("element", child_semantics))
        if child.tail and child.tail.strip():
            content.append(("text", child.tail.strip()))
    return element.tag, tuple(sorted(element.attrib.items())), tuple(content)


def _resource_identity(element: ET.Element) -> tuple[str, str] | None:
    if not isinstance(element.tag, str):
        return None
    name = element.attrib.get("name")
    resource_type = element.attrib.get("type", _local_name(element.tag))
    return (resource_type, name) if name else None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _dex_string_table(data: bytes) -> frozenset[str]:
    sections, data_start, data_end = _validate_dex(data)
    count = _u32(data, 56)
    offset = _u32(data, 60)
    if count == 0:
        if 0x2002 in sections:
            raise VerificationError("Empty DEX string table has a string_data map entry")
        return frozenset()
    strings = []
    string_offsets = [_u32(data, offset + index * 4) for index in range(count)]
    if len(string_offsets) != len(set(string_offsets)):
        raise VerificationError("DEX string_data offsets are not unique")
    ordered_offsets = sorted(string_offsets)
    section_offsets = sorted(section_offset for _, section_offset in sections.values())
    for string_offset in string_offsets:
        if string_offset < data_start or string_offset >= data_end:
            raise VerificationError("DEX string_data offset is out of bounds")
        boundaries = [
            candidate
            for candidate in (*ordered_offsets, *section_offsets, data_end)
            if candidate > string_offset
        ]
        boundary = min(boundaries)
        utf16_size, cursor = _uleb128(data, string_offset, boundary)
        value, _ = _mutf8(data, cursor, boundary)
        if len(value.encode("utf-16-le", errors="surrogatepass")) // 2 != utf16_size:
            raise VerificationError("DEX string UTF-16 length mismatch")
        strings.append(value)
    string_data = sections.get(0x2002)
    if string_data != (count, min(string_offsets)):
        raise VerificationError("DEX string_data map entry is inconsistent")
    if len(strings) != len(set(strings)):
        raise VerificationError("DEX string table contains duplicates")
    return frozenset(strings)


def _validate_dex(data: bytes) -> tuple[dict[int, tuple[int, int]], int, int]:
    if len(data) < 112 or data[:8] not in {
        f"dex\n{version:03d}\0".encode("ascii") for version in range(35, 42)
    }:
        raise VerificationError("Malformed or unsupported DEX header")
    if _u32(data, 32) != len(data) or _u32(data, 36) != 112:
        raise VerificationError("Malformed DEX file or header size")
    if _u32(data, 40) != 0x12345678:
        raise VerificationError("Invalid DEX endian tag")
    if data[12:32] != hashlib.sha1(data[32:]).digest():
        raise VerificationError("DEX SHA-1 signature mismatch")
    if _u32(data, 8) != zlib.adler32(data[12:]) & 0xFFFFFFFF:
        raise VerificationError("DEX Adler-32 checksum mismatch")
    link_size = _u32(data, 44)
    link_offset = _u32(data, 48)
    if (not link_size and link_offset) or (
        link_size and (not link_offset or link_offset > len(data) - link_size)
    ):
        raise VerificationError("DEX link section bounds are invalid")

    data_size = _u32(data, 104)
    data_offset = _u32(data, 108)
    if (
        not data_size
        or data_offset < 112
        or data_offset % 4
        or data_offset + data_size != len(data)
    ):
        raise VerificationError("DEX data section bounds are invalid")
    map_offset = _u32(data, 52)
    if map_offset < data_offset or map_offset % 4 or map_offset + 4 > len(data):
        raise VerificationError("DEX map offset is invalid")
    map_size = _u32(data, map_offset)
    if not map_size or map_size > (len(data) - map_offset - 4) // 12:
        raise VerificationError("DEX map list is out of bounds")

    sections: dict[int, tuple[int, int]] = {}
    previous_offset = -1
    for index in range(map_size):
        cursor = map_offset + 4 + index * 12
        section_type, unused, size, offset = struct.unpack_from("<HHII", data, cursor)
        if unused or not size or section_type in sections:
            raise VerificationError("DEX map list contains invalid or duplicate entries")
        if offset <= previous_offset or offset >= len(data):
            raise VerificationError("DEX map section offsets are not monotonically valid")
        if section_type >= 0x1000 and offset < data_offset:
            raise VerificationError("DEX data map entry precedes the data section")
        sections[section_type] = (size, offset)
        previous_offset = offset

    if sections.get(0x0000) != (1, 0):
        raise VerificationError("DEX header map entry is inconsistent")
    if sections.get(0x1000) != (1, map_offset):
        raise VerificationError("DEX map-list entry is inconsistent")
    data_offsets = [offset for section_type, (_, offset) in sections.items() if section_type >= 0x1000]
    if not data_offsets or min(data_offsets) != data_offset:
        raise VerificationError("DEX data offset does not match the first data section")

    header_sections = (
        (0x0001, 56, 60, 4),
        (0x0002, 64, 68, 4),
        (0x0003, 72, 76, 12),
        (0x0004, 80, 84, 8),
        (0x0005, 88, 92, 8),
        (0x0006, 96, 100, 32),
    )
    for section_type, size_field, offset_field, width in header_sections:
        size = _u32(data, size_field)
        offset = _u32(data, offset_field)
        if not size:
            if offset or section_type in sections:
                raise VerificationError("Empty DEX ID section is inconsistent")
            continue
        if (
            offset < 112
            or offset % 4
            or size > (len(data) - offset) // width
            or sections.get(section_type) != (size, offset)
        ):
            raise VerificationError("DEX ID section is inconsistent with its map entry")
    return sections, data_offset, len(data)


def _u32(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise VerificationError("DEX field is out of bounds")
    return struct.unpack_from("<I", data, offset)[0]


def _uleb128(data: bytes, offset: int, limit: int | None = None) -> tuple[int, int]:
    limit = len(data) if limit is None else limit
    value = 0
    for index in range(5):
        if offset + index >= limit:
            raise VerificationError("Truncated DEX ULEB128")
        byte = data[offset + index]
        if index == 4 and byte > 0x0F:
            raise VerificationError("Overflowing DEX ULEB128")
        value |= (byte & 0x7F) << (index * 7)
        if not byte & 0x80:
            if index and byte == 0:
                raise VerificationError("Noncanonical DEX ULEB128")
            return value, offset + index + 1
    raise VerificationError("Unterminated DEX ULEB128")


def _mutf8(data: bytes, offset: int, limit: int | None = None) -> tuple[str, int]:
    units: list[int] = []
    cursor = offset
    limit = len(data) if limit is None else limit
    while cursor < limit:
        first = data[cursor]
        cursor += 1
        if first == 0:
            raw = b"".join(unit.to_bytes(2, "little") for unit in units)
            try:
                return raw.decode("utf-16-le", errors="surrogatepass"), cursor
            except UnicodeDecodeError as error:
                raise VerificationError("Invalid DEX modified UTF-8 surrogate sequence") from error
        if first <= 0x7F:
            units.append(first)
            continue
        if first & 0xE0 == 0xC0:
            if cursor >= limit:
                raise VerificationError("Truncated DEX modified UTF-8")
            second = data[cursor]
            cursor += 1
            if second & 0xC0 != 0x80:
                raise VerificationError("Invalid DEX modified UTF-8 continuation")
            unit = ((first & 0x1F) << 6) | (second & 0x3F)
            if unit and unit < 0x80:
                raise VerificationError("Overlong DEX modified UTF-8")
            units.append(unit)
            continue
        if first & 0xF0 == 0xE0:
            if cursor + 1 >= limit:
                raise VerificationError("Truncated DEX modified UTF-8")
            second, third = data[cursor], data[cursor + 1]
            cursor += 2
            if second & 0xC0 != 0x80 or third & 0xC0 != 0x80:
                raise VerificationError("Invalid DEX modified UTF-8 continuation")
            unit = ((first & 0x0F) << 12) | ((second & 0x3F) << 6) | (third & 0x3F)
            if unit < 0x800:
                raise VerificationError("Overlong DEX modified UTF-8")
            units.append(unit)
            continue
        raise VerificationError("Invalid DEX modified UTF-8 lead byte")
    raise VerificationError("Unterminated DEX modified UTF-8 string")


def _load_archive(path: Path, label: str) -> tuple[_Archive, str]:
    try:
        data = _read_regular(path)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise VerificationError(f"{label} archive contains duplicate entry names")
            entries = tuple((info.filename, archive.read(info)) for info in infos)
        return _Archive(entries), hashlib.sha256(data).hexdigest()
    except VerificationError:
        raise
    except (
        OSError,
        EOFError,
        RuntimeError,
        NotImplementedError,
        zlib.error,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as error:
        raise VerificationError(f"Could not read {label} archive: {error}") from error


def _read_regular(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise VerificationError(f"Path is not a regular file: {path}")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                return stream.read()
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except OSError as error:
        raise VerificationError(f"Could not read regular file {path}: {error}") from error


def _regular_path(value: object, label: str) -> Path:
    if not isinstance(value, Path):
        raise TypeError(f"{label} must be a Path")
    _reject_symlink_components(value)
    if value.is_symlink() or not value.is_file():
        raise VerificationError(f"{label} must be an existing regular non-symlink file")
    return value.resolve()


def _directory_path(value: object, label: str) -> Path:
    if not isinstance(value, Path):
        raise TypeError(f"{label} must be a Path")
    _reject_symlink_components(value)
    if value.is_symlink() or not value.is_dir():
        raise VerificationError(f"{label} must be an existing non-symlink directory")
    return value.resolve()


def _reject_symlink_components(path: Path) -> None:
    absolute = path.absolute()
    for component in (absolute, *absolute.parents):
        if component.is_symlink() or (
            hasattr(component, "is_junction") and component.is_junction()
        ):
            raise VerificationError(f"Path contains a symlink or junction: {path}")


def _confined(root: Path, relative: str) -> Path:
    candidate = root.joinpath(*relative.split("/"))
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise VerificationError(f"Path escapes root: {relative}") from error
    current = root
    for part in candidate.relative_to(root).parts:
        current /= part
        if current.is_symlink() or (
            hasattr(current, "is_junction") and current.is_junction()
        ):
            raise VerificationError(f"Symlink path is not allowed: {relative}")
        if not current.exists():
            break
    resolved = candidate.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise VerificationError(f"Path escapes root: {relative}")
    return candidate


def _ensure_confined_entry(root: Path, path: Path) -> None:
    relative = path.relative_to(root).as_posix()
    _confined(root, relative)


def _regular_files(root: Path, pattern: str) -> tuple[Path, ...]:
    files = []
    for path in sorted(root.rglob(pattern)):
        if path.is_symlink() or not path.is_file():
            raise VerificationError(f"Decoded path is not a regular file: {path}")
        _ensure_confined_entry(root, path)
        files.append(path)
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise VerificationError(f"Symlink path is not allowed: {path}")
    return tuple(files)


def _is_signature(name: str) -> bool:
    parts = name.upper().split("/")
    return len(parts) == 2 and parts[0] == "META-INF" and (
        parts[1] == "MANIFEST.MF"
        or parts[1].startswith("SIG-")
        or parts[1].endswith((".SF", ".RSA", ".DSA", ".EC"))
    )


def _count_result(actual: int, expected: int) -> tuple[bool, str]:
    return (
        actual == expected,
        "matched" if actual == expected else f"expected count {expected}, found {actual}",
    )


def _set_result(actual: set[str], expected: set[str], label: str) -> tuple[bool, str]:
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    passed = not missing and not extra
    return passed, "matched" if passed else f"{label} missing={missing!r} extra={extra!r}"

#!/usr/bin/env python3
"""Safe host-side ADB validation for the DFInsta behavior contract."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


DEFAULT_CONTRACT = Path(__file__).resolve().parents[2] / "dfinsta_source_1.4.1" / "behavior_contract.json"
BOUNDS_RE = re.compile(r"^\[(\d+),(\d+)\]\[(\d+),(\d+)\]$")
FATAL_MARKERS = ("FATAL EXCEPTION", "ACRA caught a")


class AdbError(RuntimeError):
    def __init__(self, command: Sequence[str], returncode: int, output: str) -> None:
        super().__init__(f"ADB command failed ({returncode}): {' '.join(command)}\n{output.strip()}")
        self.command = list(command)
        self.returncode = returncode
        self.output = output


@dataclass(frozen=True)
class UiNode:
    text: str
    content_desc: str
    resource_id: str
    class_name: str
    bounds: str
    clickable: bool
    long_clickable: bool
    checked: bool
    selected: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "content_desc": self.content_desc,
            "resource_id": self.resource_id,
            "class": self.class_name,
            "bounds": self.bounds,
            "center": bounds_center(self.bounds),
            "clickable": self.clickable,
            "long_clickable": self.long_clickable,
            "checked": self.checked,
            "selected": self.selected,
        }


class Adb:
    def __init__(self, executable: str = "adb", serial: str | None = None) -> None:
        self.executable = executable
        self.serial = serial

    def command(self, *args: str) -> list[str]:
        command = [self.executable]
        if self.serial:
            command.extend(["-s", self.serial])
        return [*command, *args]

    def run(self, *args: str, timeout: float = 30, check: bool = True) -> subprocess.CompletedProcess[str]:
        command = self.command(*args)
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        if check and result.returncode:
            raise AdbError(command, result.returncode, result.stdout + result.stderr)
        return result


def load_contract(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def bounds_center(bounds: str) -> tuple[int, int]:
    match = BOUNDS_RE.fullmatch(bounds)
    if not match:
        raise ValueError(f"Invalid UI bounds: {bounds!r}")
    left, top, right, bottom = (int(value) for value in match.groups())
    if right < left or bottom < top:
        raise ValueError(f"Inverted UI bounds: {bounds!r}")
    return ((left + right) // 2, (top + bottom) // 2)


def parse_ui_xml(xml: str | bytes) -> list[UiNode]:
    root = ET.fromstring(xml)
    nodes: list[UiNode] = []
    for element in root.iter("node"):
        attributes = element.attrib
        nodes.append(
            UiNode(
                text=attributes.get("text", ""),
                content_desc=attributes.get("content-desc", ""),
                resource_id=attributes.get("resource-id", ""),
                class_name=attributes.get("class", ""),
                bounds=attributes.get("bounds", ""),
                clickable=attributes.get("clickable") == "true",
                long_clickable=attributes.get("long-clickable") == "true",
                checked=attributes.get("checked") == "true",
                selected=attributes.get("selected") == "true",
            )
        )
    return nodes


def accepted_startup_anchor_set(nodes: Iterable[UiNode], anchor_sets: Sequence[Sequence[str]]) -> list[str] | None:
    visible = {value for node in nodes for value in (node.text, node.content_desc) if value}
    return next((list(anchor_set) for anchor_set in anchor_sets if set(anchor_set) <= visible), None)


def fatal_log_lines(logcat: str) -> list[str]:
    return [line for line in logcat.splitlines() if any(marker in line for marker in FATAL_MARKERS)]


def find_nodes(nodes: Iterable[UiNode], **criteria: Any) -> list[UiNode]:
    aliases = {"content_desc": "content_desc", "resource_id": "resource_id", "class_name": "class_name"}
    matches = []
    for node in nodes:
        if all(getattr(node, aliases.get(key, key)) == value for key, value in criteria.items() if value is not None):
            matches.append(node)
    return matches


def selector_criteria(selector: dict[str, Any]) -> dict[str, str]:
    allowed = {"text", "resource_id", "content_desc"}
    unsupported = set(selector) - allowed
    if unsupported:
        raise ValueError(f"Unsupported selector keys: {', '.join(sorted(unsupported))}")
    criteria = {key: value for key, value in selector.items() if key in allowed and isinstance(value, str) and value}
    if not criteria:
        raise ValueError("Selector requires non-empty text, resource_id, or content_desc")
    return criteria


def find_selector_nodes(nodes: Iterable[UiNode], selector: dict[str, Any]) -> list[UiNode]:
    return find_nodes(nodes, **selector_criteria(selector))


def evaluate_text_assertions(nodes: Iterable[UiNode], assertions: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    visible_text = {node.text for node in nodes if node.text}
    results = []
    for assertion in assertions:
        kind = assertion.get("kind")
        anchors = assertion.get("anchors")
        match = assertion.get("match", "all")
        severity = assertion.get("severity", "required")
        if kind not in {"visible_text", "absent_text"}:
            raise ValueError(f"Unsupported text assertion kind: {kind!r}")
        if not isinstance(anchors, list) or not anchors or not all(isinstance(anchor, str) and anchor for anchor in anchors):
            raise ValueError("Text assertion anchors must be a non-empty string list")
        if match not in {"all", "any"}:
            raise ValueError(f"Unsupported text assertion match: {match!r}")
        if severity not in {"required", "evidence"}:
            raise ValueError(f"Unsupported text assertion severity: {severity!r}")
        anchor_matches = {anchor: (anchor in visible_text) for anchor in anchors}
        values = list(anchor_matches.values())
        passed = all(values) if match == "all" else any(values)
        if kind == "absent_text":
            passed = not any(values) if match == "all" else not all(values)
        results.append(
            {
                "name": assertion.get("name"),
                "kind": kind,
                "anchors": anchors,
                "match": match,
                "severity": severity,
                "passed": passed,
                "anchor_visible": anchor_matches,
            }
        )
    return results


def dump_ui(adb: Adb, destination: Path) -> list[UiNode]:
    remote = "/data/local/tmp/dfinsta-window.xml"
    adb.run("shell", "uiautomator", "dump", remote, timeout=20)
    destination.parent.mkdir(parents=True, exist_ok=True)
    adb.run("pull", remote, str(destination))
    return parse_ui_xml(destination.read_text(encoding="utf-8"))


def capture_screenshot(adb: Adb, destination: Path, name: str) -> None:
    remote = f"/data/local/tmp/dfinsta-{name}.png"
    adb.run("shell", "screencap", "-p", remote)
    destination.parent.mkdir(parents=True, exist_ok=True)
    adb.run("pull", remote, str(destination))


def tap(adb: Adb, node: UiNode) -> None:
    x, y = bounds_center(node.bounds)
    adb.run("shell", "input", "tap", str(x), str(y))


def long_press(adb: Adb, node: UiNode, duration_ms: int = 800) -> None:
    x, y = bounds_center(node.bounds)
    adb.run("shell", "input", "swipe", str(x), str(y), str(x), str(y), str(duration_ms))


def evidence(command: str, contract_path: Path, **details: Any) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "command": command,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "contract": str(contract_path),
        **details,
    }


def package_process(adb: Adb, package: str) -> str | None:
    result = adb.run("shell", "pidof", package, check=False)
    return result.stdout.strip() or None


def preflight(adb: Adb, contract: dict[str, Any], contract_path: Path) -> dict[str, Any]:
    package = contract["package"]
    state = adb.run("get-state", check=False)
    package_path = adb.run("shell", "pm", "path", package, check=False)
    package_dump = adb.run("shell", "dumpsys", "package", package, check=False).stdout
    version_name = re.search(r"\bversionName=([^\s]+)", package_dump)
    version_code = re.search(r"\bversionCode=(\d+)", package_dump)
    checks = {
        "device_ready": state.returncode == 0 and state.stdout.strip() == "device",
        "package_installed": package_path.returncode == 0 and package_path.stdout.startswith("package:"),
        "version_name_matches": bool(version_name and version_name.group(1) == contract["base_version_name"]),
        "version_code_matches": bool(version_code and int(version_code.group(1)) == contract["base_version_code"]),
    }
    return evidence(
        "preflight",
        contract_path,
        ok=all(checks.values()),
        checks=checks,
        observed={
            "device_state": state.stdout.strip(),
            "version_name": version_name.group(1) if version_name else None,
            "version_code": int(version_code.group(1)) if version_code else None,
        },
    )


def startup(adb: Adb, contract: dict[str, Any], contract_path: Path, wait: float, artifact_dir: Path) -> dict[str, Any]:
    package = contract["package"]
    adb.run("logcat", "-c")
    adb.run("shell", "am", "force-stop", package)
    start = adb.run("shell", "am", "start", "-W", "-n", contract["startup"]["component"], timeout=45)
    time.sleep(wait)
    pid = package_process(adb, package)
    logcat = adb.run("logcat", "-d", "-v", "threadtime", timeout=30).stdout
    fatal_lines = fatal_log_lines(logcat)
    ui_error = None
    matched_anchors = None
    try:
        nodes = dump_ui(adb, artifact_dir / "startup.xml")
        matched_anchors = accepted_startup_anchor_set(nodes, contract["startup"]["logged_out_accepted_anchor_sets"])
    except (AdbError, ET.ParseError, OSError, ValueError) as exc:
        ui_error = str(exc)
    checks = {"process_alive": pid is not None, "no_android_runtime_fatal": not fatal_lines}
    return evidence(
        "startup",
        contract_path,
        ok=all(checks[name] for name in contract["startup"]["required"]),
        checks=checks,
        process_id=pid,
        accepted_logged_out_anchor_set=matched_anchors,
        ui_capture_error=ui_error,
        fatal_logcat=fatal_lines,
        am_start=start.stdout.strip(),
    )


def ui_dump_evidence(adb: Adb, contract_path: Path, output: Path) -> dict[str, Any]:
    nodes = dump_ui(adb, output)
    return evidence("dump-ui", contract_path, ok=True, output=str(output), node_count=len(nodes))


def node_lookup_evidence(adb: Adb, contract_path: Path, args: argparse.Namespace, output: Path) -> dict[str, Any]:
    nodes = dump_ui(adb, output)
    criteria = {
        "text": args.text,
        "content_desc": args.content_desc,
        "resource_id": args.resource_id,
        "class_name": args.class_name,
        "clickable": args.clickable,
        "long_clickable": args.long_clickable,
        "checked": args.checked,
    }
    matches = find_nodes(nodes, **criteria)
    return evidence(
        "find-node",
        contract_path,
        ok=bool(matches),
        criteria={key: value for key, value in criteria.items() if value is not None},
        matches=[node.as_dict() for node in matches],
        hierarchy=str(output),
    )


def _fresh_nodes(adb: Adb, artifact_dir: Path, name: str) -> list[UiNode]:
    return dump_ui(adb, artifact_dir / name)


def enter_settings(
    adb: Adb,
    contract: dict[str, Any],
    contract_path: Path,
    artifact_dir: Path,
    attempts: int,
    poll_interval: float,
) -> dict[str, Any]:
    package = contract["package"]
    profile_id = f"{package}:id/profile_tab"
    nodes = _fresh_nodes(adb, artifact_dir, "settings-entry-00.xml")
    profiles = find_nodes(nodes, resource_id=profile_id)
    if not profiles:
        return evidence("enter-settings", contract_path, ok=False, error="Profile tab not found", steps=[])
    steps = ["tap_profile"]
    tap(adb, profiles[0])
    time.sleep(poll_interval)

    for attempt in range(1, attempts + 1):
        nodes = _fresh_nodes(adb, artifact_dir, f"settings-entry-{attempt:02d}.xml")
        options = find_nodes(nodes, content_desc="Options", long_clickable=True)
        if options:
            long_press(adb, options[0])
            steps.append("long_press_options")
            time.sleep(poll_interval)
            final_nodes = _fresh_nodes(adb, artifact_dir, "settings-entry-final.xml")
            title_found = bool(find_nodes(final_nodes, text="Distraction Free settings"))
            return evidence(
                "enter-settings",
                contract_path,
                ok=title_found,
                checks={"semantic_options_found": True, "settings_title_found": title_found},
                attempts=attempt,
                steps=steps,
            )

        if attempt == 1:
            home = find_nodes(nodes, content_desc="Home") or find_nodes(nodes, text="Home")
            if home:
                tap(adb, home[0])
                steps.append("tap_home")
                time.sleep(poll_interval)
                nodes = _fresh_nodes(adb, artifact_dir, "settings-entry-home.xml")
                profiles = find_nodes(nodes, resource_id=profile_id)
                if profiles:
                    tap(adb, profiles[0])
                    steps.append("tap_profile_after_home")
                    time.sleep(poll_interval)

        valid_bounds = [bounds_center(node.bounds) for node in nodes if BOUNDS_RE.fullmatch(node.bounds)]
        width = max((x for x, _ in valid_bounds), default=540) * 2
        height = max((y for _, y in valid_bounds), default=1200) * 2
        x = width // 2
        adb.run("shell", "input", "swipe", str(x), str(height * 2 // 5), str(x), str(height * 3 // 5), "300")
        steps.append("swipe_down_toward_header")
        time.sleep(poll_interval)

    return evidence(
        "enter-settings",
        contract_path,
        ok=False,
        checks={"semantic_options_found": False, "settings_title_found": False},
        attempts=attempts,
        steps=steps,
        error="Long-clickable Options node did not appear",
    )


def reels_capture(adb: Adb, contract: dict[str, Any], contract_path: Path, artifact_dir: Path) -> dict[str, Any]:
    package = contract["package"]
    hierarchy = artifact_dir / "reels.xml"
    screenshot = artifact_dir / "reels.png"
    hierarchy_error = None
    node_count = None
    try:
        node_count = len(dump_ui(adb, hierarchy))
    except (AdbError, ET.ParseError, OSError, ValueError) as exc:
        hierarchy_error = str(exc)
        remote = "/data/local/tmp/dfinsta-reels.png"
        adb.run("shell", "screencap", "-p", remote)
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        adb.run("pull", remote, str(screenshot))
    pid = package_process(adb, package)
    return evidence(
        "reels-capture",
        contract_path,
        ok=pid is not None and (node_count is not None or screenshot.exists()),
        process_alive=pid is not None,
        process_id=pid,
        capture_mode="hierarchy" if node_count is not None else "screenshot_process_fallback",
        hierarchy=str(hierarchy) if node_count is not None else None,
        node_count=node_count,
        screenshot=str(screenshot) if screenshot.exists() else None,
        hierarchy_error=hierarchy_error,
    )


def _not_evaluated_assertions(assertions: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": assertion.get("name"),
            "kind": assertion.get("kind"),
            "anchors": assertion.get("anchors"),
            "match": assertion.get("match", "all"),
            "severity": assertion.get("severity", "required"),
            "passed": None,
            "anchor_visible": None,
        }
        for assertion in assertions
    ]


def feature_state(
    adb: Adb,
    contract: dict[str, Any],
    contract_path: Path,
    artifact_dir: Path,
    target_names: Sequence[str] | None,
    leave_settings: bool,
) -> dict[str, Any]:
    package = contract["package"]
    config = contract["device_validation"]["feature_state"]
    targets = config["targets"]
    if target_names:
        requested = set(target_names)
        unknown = requested - {target["name"] for target in targets}
        if unknown:
            raise ValueError(f"Unknown feature-state targets: {', '.join(sorted(unknown))}")
        targets = [target for target in targets if target["name"] in requested]

    artifact_dir.mkdir(parents=True, exist_ok=True)
    steps: list[str] = []
    current_nodes: list[UiNode] | None = None
    initial_error = None
    try:
        current_nodes = dump_ui(adb, artifact_dir / "feature-state-initial.xml")
    except (AdbError, ET.ParseError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        initial_error = str(exc)

    settings_exit = {"requested": leave_settings, "pressed_back": False}
    if leave_settings and current_nodes is not None:
        settings_selector = config["settings_screen_selector"]
        if find_selector_nodes(current_nodes, settings_selector):
            adb.run("shell", "input", "keyevent", "KEYCODE_BACK")
            settings_exit["pressed_back"] = True
            steps.append("press_back_from_settings")
            time.sleep(config["poll_interval_seconds"])
            current_nodes = None

    target_results = []
    for target in targets:
        name = target["name"]
        navigation_error = None
        nav_node = None
        for attempt in range(1, config["poll_attempts"] + 1):
            if current_nodes is None:
                try:
                    current_nodes = dump_ui(adb, artifact_dir / f"{name}-navigation.xml")
                except (AdbError, ET.ParseError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
                    navigation_error = str(exc)
                    current_nodes = None
            if current_nodes is not None:
                matches = find_selector_nodes(current_nodes, target["selector"])
                if matches:
                    nav_node = matches[0]
                    break
            if attempt < config["poll_attempts"]:
                time.sleep(config["poll_interval_seconds"])

        if nav_node is not None:
            tap(adb, nav_node)
            steps.append(f"tap_{name}_navigation")
            time.sleep(config["poll_interval_seconds"])

        screenshot = artifact_dir / f"{name}.png"
        capture_screenshot(adb, screenshot, f"feature-state-{name}")
        hierarchy = artifact_dir / f"{name}.xml"
        hierarchy_error = None
        captured_nodes = None
        try:
            captured_nodes = dump_ui(adb, hierarchy)
        except (AdbError, ET.ParseError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
            hierarchy_error = str(exc)

        assertions = (
            evaluate_text_assertions(captured_nodes, target["disabled_state_assertions"])
            if captured_nodes is not None
            else _not_evaluated_assertions(target["disabled_state_assertions"])
        )
        required_assertions_ok = all(
            assertion["passed"] is not False
            for assertion in assertions
            if assertion["severity"] == "required"
        )
        pid = package_process(adb, package)
        fallback_allowed = target.get("allow_screenshot_process_fallback", False)
        hierarchy_required = not fallback_allowed
        capture_ok = screenshot.exists() and (captured_nodes is not None or not hierarchy_required)
        navigation_ok = nav_node is not None
        target_results.append(
            {
                "name": name,
                "features": target["features"],
                "ok": pid is not None and navigation_ok and capture_ok and required_assertions_ok,
                "navigation": {
                    "selector": target["selector"],
                    "found": nav_node is not None,
                    "verified": navigation_ok,
                    "error": navigation_error,
                },
                "process_alive": pid is not None,
                "process_id": pid,
                "capture_mode": "hierarchy_and_screenshot" if captured_nodes is not None else "screenshot_process_fallback",
                "hierarchy": str(hierarchy) if captured_nodes is not None else None,
                "screenshot": str(screenshot) if screenshot.exists() else None,
                "node_count": len(captured_nodes) if captured_nodes is not None else None,
                "hierarchy_error": hierarchy_error,
                "disabled_state_assertions": assertions,
            }
        )
        current_nodes = captured_nodes

    final_pid = package_process(adb, package)
    logcat_args = ["logcat", "-d", "-v", "threadtime"]
    if final_pid:
        logcat_args.extend(["--pid", final_pid.split()[0]])
    logcat = adb.run(*logcat_args, timeout=30, check=False).stdout
    fatal_lines = fatal_log_lines(logcat)
    checks = {
        "process_alive": final_pid is not None,
        "no_android_runtime_fatal": not fatal_lines,
        "targets_ok": all(target["ok"] for target in target_results),
    }
    return evidence(
        "feature-state",
        contract_path,
        ok=all(checks.values()),
        checks=checks,
        process_id=final_pid,
        settings_exit=settings_exit,
        initial_hierarchy_error=initial_error,
        steps=steps,
        targets=target_results,
        fatal_logcat=fatal_lines,
    )


def boolean_argument(value: str) -> bool:
    if value.lower() in {"true", "1", "yes"}:
        return True
    if value.lower() in {"false", "0", "no"}:
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb", default="adb", help="ADB executable path")
    parser.add_argument("--serial", help="ADB device serial")
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--artifact-dir", type=Path, help="directory for pulled evidence")
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("preflight")
    startup_parser = subparsers.add_parser("startup")
    startup_parser.add_argument("--wait", type=float, default=10)
    dump_parser = subparsers.add_parser("dump-ui")
    dump_parser.add_argument("--output", type=Path)
    find_parser = subparsers.add_parser("find-node")
    find_parser.add_argument("--text")
    find_parser.add_argument("--content-desc")
    find_parser.add_argument("--resource-id")
    find_parser.add_argument("--class-name")
    find_parser.add_argument("--clickable", type=boolean_argument)
    find_parser.add_argument("--long-clickable", type=boolean_argument)
    find_parser.add_argument("--checked", type=boolean_argument)
    find_parser.add_argument("--output", type=Path)
    settings_parser = subparsers.add_parser("enter-settings")
    settings_parser.add_argument("--attempts", type=int, default=6)
    settings_parser.add_argument("--poll-interval", type=float, default=1)
    subparsers.add_parser("reels-capture")
    feature_parser = subparsers.add_parser("feature-state")
    feature_parser.add_argument("--target", action="append", dest="targets")
    feature_parser.add_argument(
        "--leave-settings",
        action="store_true",
        help="press Back only when the contract settings-screen selector is visible",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    contract_path = args.contract.resolve()
    artifact_dir = args.artifact_dir or Path(tempfile.mkdtemp(prefix="dfinsta-device-validation-"))
    adb = Adb(args.adb, args.serial)
    try:
        contract = load_contract(contract_path)
        if args.action == "preflight":
            result = preflight(adb, contract, contract_path)
        elif args.action == "startup":
            result = startup(adb, contract, contract_path, args.wait, artifact_dir)
        elif args.action == "dump-ui":
            result = ui_dump_evidence(adb, contract_path, args.output or artifact_dir / "window.xml")
        elif args.action == "find-node":
            result = node_lookup_evidence(adb, contract_path, args, args.output or artifact_dir / "window.xml")
        elif args.action == "enter-settings":
            result = enter_settings(adb, contract, contract_path, artifact_dir, args.attempts, args.poll_interval)
        elif args.action == "reels-capture":
            result = reels_capture(adb, contract, contract_path, artifact_dir)
        else:
            result = feature_state(adb, contract, contract_path, artifact_dir, args.targets, args.leave_settings)
    except (AdbError, ET.ParseError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        result = evidence(args.action, contract_path, ok=False, error=str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Safe host-side ADB validation for the DFInsta behavior contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
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


def selector_criteria(selector: dict[str, Any]) -> dict[str, Any]:
    string_keys = {"text", "resource_id", "content_desc", "class_name"}
    boolean_keys = {"clickable", "long_clickable", "checked", "selected"}
    allowed = string_keys | boolean_keys
    unsupported = set(selector) - allowed
    if unsupported:
        raise ValueError(f"Unsupported selector keys: {', '.join(sorted(unsupported))}")
    criteria = {
        key: value
        for key, value in selector.items()
        if (key in string_keys and isinstance(value, str) and value)
        or (key in boolean_keys and isinstance(value, bool))
    }
    if not criteria:
        raise ValueError("Selector requires at least one valid criterion")
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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_device_context(adb: Adb) -> dict[str, Any]:
    def shell_value(*args: str) -> str | None:
        result = adb.run("shell", *args, check=False)
        value = result.stdout.strip()
        return value or None

    size_output = shell_value("wm", "size")
    density_output = shell_value("wm", "density")
    display = None
    if size_output:
        try:
            width, height = physical_display_size(size_output)
            density_match = re.search(r"Physical density:\s*(\d+)", density_output or "")
            display = {
                "width_px": width,
                "height_px": height,
                "density_dpi": int(density_match.group(1)) if density_match else None,
            }
        except ValueError:
            pass
    locale = shell_value("getprop", "persist.sys.locale") or shell_value(
        "getprop", "ro.product.locale"
    )
    api_level = shell_value("getprop", "ro.build.version.sdk")
    return {
        "serial": adb.serial,
        "model": shell_value("getprop", "ro.product.model"),
        "fingerprint": shell_value("getprop", "ro.build.fingerprint"),
        "api_level": int(api_level) if api_level and api_level.isdigit() else None,
        "locale": locale,
        "display": display,
    }


def artifact_context(args: argparse.Namespace) -> dict[str, Any]:
    apk = args.artifact_apk.resolve()
    reports = [path.resolve() for path in args.build_report]
    return {
        "path": str(apk),
        "sha256": sha256_file(apk),
        "commit": args.artifact_commit,
        "build_reports": [
            {"path": str(path), "sha256": sha256_file(path)} for path in reports
        ],
    }


def package_process(adb: Adb, package: str) -> str | None:
    result = adb.run("shell", "pidof", package, check=False)
    return result.stdout.strip() or None


def startup_intent_arguments(config: dict[str, Any]) -> list[str]:
    arguments: list[str] = []
    action = config.get("action")
    if action:
        arguments.extend(["-a", action])
    for category in config.get("categories", []):
        arguments.extend(["-c", category])
    arguments.extend(["-n", config["component"]])
    return arguments


def resumed_activity(output: str) -> str | None:
    match = re.search(
        r"(?:topResumedActivity=|mResumedActivity:|ResumedActivity:).*?\su\d+\s+([^\s}]+)",
        output,
    )
    return match.group(1) if match else None


def physical_display_size(output: str) -> tuple[int, int]:
    match = re.search(r"Physical size:\s*(\d+)x(\d+)", output)
    if not match:
        raise ValueError("Unable to parse physical display size")
    return int(match.group(1)), int(match.group(2))


def foreground_state_valid(
    config: dict[str, Any], foreground: str | None, matched_anchors: list[str] | None
) -> bool:
    states = config.get("foreground_states")
    if states is None:
        expected = config.get("foreground_activity")
        return expected is None or foreground == expected
    for state in states:
        if foreground != state["activity"]:
            continue
        if state.get("requires_logged_out_anchor_set", False) and matched_anchors is None:
            continue
        return True
    return False


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


def startup(
    adb: Adb,
    contract: dict[str, Any],
    contract_path: Path,
    wait: float,
    artifact_dir: Path,
    launch_strategy: str | None,
) -> dict[str, Any]:
    package = contract["package"]
    startup_config = contract["startup"]
    strategy = launch_strategy or startup_config.get("launch_strategy", "explicit_component")
    adb.run("logcat", "-c")
    adb.run("shell", "am", "force-stop", package)
    if strategy == "explicit_component":
        start = adb.run(
            "shell",
            "am",
            "start",
            "-W",
            *startup_intent_arguments(startup_config),
            timeout=45,
        )
    elif strategy == "package_launcher":
        start = adb.run(
            "shell",
            "monkey",
            "-p",
            package,
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
            timeout=45,
        )
    else:
        raise ValueError(f"Unknown launch strategy: {strategy}")
    time.sleep(wait)
    activities = adb.run("shell", "dumpsys", "activity", "activities", check=False).stdout
    foreground = resumed_activity(activities)
    pid = package_process(adb, package)
    logcat = adb.run("logcat", "-d", "-v", "threadtime", timeout=30).stdout
    fatal_lines = fatal_log_lines(logcat)
    ui_error = None
    matched_anchors = None
    try:
        nodes = dump_ui(adb, artifact_dir / "startup.xml")
        matched_anchors = accepted_startup_anchor_set(
            nodes, startup_config["logged_out_accepted_anchor_sets"]
        )
    except (AdbError, ET.ParseError, OSError, ValueError) as exc:
        ui_error = str(exc)
    checks = {
        "process_alive": pid is not None,
        "no_android_runtime_fatal": not fatal_lines,
        "foreground_state": foreground_state_valid(startup_config, foreground, matched_anchors),
    }
    return evidence(
        "startup",
        contract_path,
        ok=all(checks[name] for name in startup_config["required"]),
        checks=checks,
        process_id=pid,
        foreground_activity=foreground,
        foreground_states=startup_config.get("foreground_states"),
        launch_strategy=strategy,
        accepted_logged_out_anchor_set=matched_anchors,
        ui_capture_error=ui_error,
        fatal_logcat=fatal_lines,
        launch_output=start.stdout.strip(),
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
    settings_config = contract["settings"]
    selectors = settings_config["entry_selectors"]
    recovery_actions = settings_config.get("recovery_actions", [])
    nodes = _fresh_nodes(adb, artifact_dir, "settings-entry-00.xml")
    profiles = find_selector_nodes(nodes, selectors["profile"])
    if not profiles:
        return evidence("enter-settings", contract_path, ok=False, error="Profile tab not found", steps=[])
    steps = ["tap_profile"]
    tap(adb, profiles[0])
    time.sleep(poll_interval)

    for attempt in range(1, attempts + 1):
        nodes = _fresh_nodes(adb, artifact_dir, f"settings-entry-{attempt:02d}.xml")
        options = find_selector_nodes(nodes, selectors["options"])
        if options:
            long_press(adb, options[0])
            steps.append("long_press_options")
            time.sleep(poll_interval)
            final_nodes = _fresh_nodes(adb, artifact_dir, "settings-entry-final.xml")
            title_found = bool(find_nodes(final_nodes, text=contract["settings"]["title"]))
            return evidence(
                "enter-settings",
                contract_path,
                ok=title_found,
                checks={"semantic_options_found": True, "settings_title_found": title_found},
                attempts=attempt,
                steps=steps,
            )

        if attempt == 1 and "retry_profile" in recovery_actions:
            profiles = find_selector_nodes(nodes, selectors["profile"])
            if profiles:
                tap(adb, profiles[0])
                steps.append("retry_profile")
                time.sleep(poll_interval)

        if attempt == 1 and "home_then_profile" in recovery_actions:
            home = find_selector_nodes(nodes, selectors["home"])
            if home:
                tap(adb, home[0])
                steps.append("tap_home")
                time.sleep(poll_interval)
                nodes = _fresh_nodes(adb, artifact_dir, "settings-entry-home.xml")
                profiles = find_selector_nodes(nodes, selectors["profile"])
                if profiles:
                    tap(adb, profiles[0])
                    steps.append("tap_profile_after_home")
                    time.sleep(poll_interval)

        if "swipe_down_toward_header" in recovery_actions:
            display = adb.run("shell", "wm", "size").stdout
            width, height = physical_display_size(display)
            x = width // 2
            adb.run(
                "shell",
                "input",
                "swipe",
                str(x),
                str(height * 2 // 5),
                str(x),
                str(height * 3 // 5),
                "300",
            )
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
        settings_selector = config.get(
            "settings_screen_selector", {"text": contract["settings"]["title"]}
        )
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
    parser.add_argument("--artifact-dir", required=True, type=Path, help="fresh evidence directory")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--artifact-apk", required=True, type=Path)
    parser.add_argument("--artifact-commit", required=True)
    parser.add_argument("--build-report", action="append", default=[], type=Path)
    parser.add_argument(
        "--install-state",
        required=True,
        choices=("clean_install", "in_place_update", "preexisting", "unknown"),
    )
    parser.add_argument(
        "--data-state",
        required=True,
        choices=("fresh", "preserved", "cleared", "unknown"),
    )
    parser.add_argument(
        "--account-state",
        required=True,
        choices=("logged_in", "logged_out", "unknown"),
    )
    parser.add_argument(
        "--cache-state",
        required=True,
        choices=("cleared", "preserved", "exhausted", "unknown"),
    )
    parser.add_argument("--state-note")
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("preflight")
    startup_parser = subparsers.add_parser("startup")
    startup_parser.add_argument("--wait", type=float, default=10)
    startup_parser.add_argument(
        "--launch-strategy",
        choices=("explicit_component", "package_launcher"),
        help="override the contract launch strategy for a separate diagnostic run",
    )
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
    feature_parser.add_argument(
        "--feature-state", choices=("enabled", "disabled", "unknown"), default="unknown"
    )
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
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", args.run_id):
        raise SystemExit("Invalid --run-id")
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", args.artifact_commit):
        raise SystemExit("Invalid --artifact-commit")
    input_paths = [args.artifact_apk, *args.build_report]
    missing_paths = [path for path in input_paths if not path.is_file()]
    if missing_paths:
        raise SystemExit(f"Missing provenance input: {missing_paths[0]}")
    artifact_dir = args.artifact_dir.resolve()
    result_path = artifact_dir / "evidence.json"
    if result_path.exists():
        raise SystemExit(f"Refusing to overwrite {result_path}")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    adb = Adb(args.adb, args.serial)
    artifact = artifact_context(args)
    try:
        contract = load_contract(contract_path)
        if args.action == "preflight":
            result = preflight(adb, contract, contract_path)
        elif args.action == "startup":
            result = startup(
                adb,
                contract,
                contract_path,
                args.wait,
                artifact_dir,
                args.launch_strategy,
            )
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
    result["schema_version"] = 2
    result["run_id"] = args.run_id
    result["artifact"] = artifact
    runner_path = Path(__file__).resolve()
    result["validation"] = {
        "runner_path": str(runner_path),
        "runner_sha256": sha256_file(runner_path),
        "contract_sha256": sha256_file(contract_path),
    }
    result["device"] = collect_device_context(adb)
    result["declared_state"] = {
        "install": args.install_state,
        "app_data": args.data_state,
        "account": args.account_state,
        "cache": args.cache_state,
        "feature": args.feature_state if args.action == "feature-state" else None,
        "note": args.state_note,
    }
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    result_path.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
